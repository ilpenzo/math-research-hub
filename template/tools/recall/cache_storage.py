"""Bounded disposable storage; generation readers pin files against collection."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil

import storage_policy
import transactions as tx


def size(path):
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())


def current(root):
    try:
        data = json.loads((root / 'CURRENT.json').read_bytes())
    except FileNotFoundError:
        return None
    value = data.get('generation')
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('Invalid recall generation pointer')
    return value


@contextmanager
def reader(hub):
    root = Path(hub).resolve() / '.hub/recall'
    if not (root / 'CURRENT.json').exists():
        yield None
        return
    lock = root / 'readers.lock'
    # New refresh/maintenance creates this permanent file. A first legacy query
    # can bootstrap it too; a read-only legacy copy falls back to live retrieval.
    try:
        stream = lock.open('rb') if lock.exists() else lock.open('a+b')
    except PermissionError:
        yield None
        return
    with stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            name = current(root)
            path = root / 'generations' / name if name else None
            if path is not None and (path.is_symlink() or not path.is_dir()):
                raise ValueError('Missing/unsafe current recall generation')
            yield path
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def collect_generations(hub):
    """Caller owns refresh.lock; readers hold a shared lease for the whole query."""
    root = Path(hub).resolve() / '.hub/recall'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'readers.lock').open('a+b') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(reclaimed_bytes=0, reader_pinned=True, removed=[])
        try:
            selected = current(root)
            candidates = [p for p in (root / 'generations').glob('*')
                          if p.is_dir() and not p.is_symlink() and re.fullmatch('[a-f0-9]{32}', p.name)]
            completed = []
            for path in candidates:
                try:
                    state = json.loads((path / 'status.json').read_bytes())
                    if state.get('ready'):
                        completed.append((state.get('created_at', ''), path.name))
                except (OSError, ValueError):
                    pass
            pointer = json.loads((root / 'CURRENT.json').read_bytes()) if selected else {}
            previous = pointer.get('previous_generation')
            if selected and 'previous_generation' not in pointer:
                selected_time = next((stamp for stamp, name in completed if name == selected), '')
                previous = next((name for stamp, name in sorted(completed, reverse=True)
                                 if name != selected and stamp <= selected_time), None)
            keep = {selected, previous}
            removed, reclaimed = [], 0
            for path in candidates:
                if path.name not in keep:
                    reclaimed += size(path)
                    shutil.rmtree(path)
                    removed.append(path.name)
            return dict(reclaimed_bytes=reclaimed, reader_pinned=False, removed=removed)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Budget:
    """One refresh owns cache mutation and accounts for in-progress output too."""
    def __init__(self, hub):
        self.hub = Path(hub).resolve()
        self.root = self.hub / '.hub/recall'
        self.policy = storage_policy.load(hub)
        self.collection = collect_generations(hub)
        self.cache = {}
        for path in (self.root / 'cache').rglob('*'):
            if path.is_file() and not path.is_symlink():
                stat = path.stat()
                self.cache[path] = (stat.st_size, stat.st_mtime_ns)
        self.cache_bytes = sum(info[0] for info in self.cache.values())
        self.directory = None
        self.trim(0)

    def trim(self, incoming, exclude=None):
        limit = self.policy['cache_budget_bytes']
        for path in sorted(self.cache, key=lambda p: self.cache[p][1]):
            if self.cache_bytes + incoming <= limit:
                break
            if path == exclude:
                continue
            self.cache_bytes -= self.cache.pop(path)[0]
            path.unlink(missing_ok=True)
        return self.cache_bytes + incoming <= limit

    def touch(self, path):
        if path in self.cache:
            os.utime(path, None)
            self.cache[path] = (self.cache[path][0], path.stat().st_mtime_ns)

    def write_cache(self, path, raw):
        # Incoming bytes include an atomic-write temporary, even on replacement.
        if len(raw) > self.policy['cache_budget_bytes'] or not self.trim(len(raw), path):
            return False
        storage_policy.require_space(self.hub, len(raw) + 65536)
        tx._replace(path, raw)
        self.cache_bytes -= self.cache.get(path, (0, 0))[0]
        self.cache[path] = (len(raw), path.stat().st_mtime_ns)
        self.cache_bytes += len(raw)
        return True

    def cap_sqlite(self, connection, directory, metadata_bytes):
        self.directory = directory
        total = size(self.root / 'generations')
        database = directory / 'index.sqlite'
        remaining = self.policy['generation_budget_bytes'] - total + database.stat().st_size - metadata_bytes
        page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        minimum = connection.execute('PRAGMA page_count').fetchone()[0]
        pages = remaining // page_size
        if pages <= minimum:
            raise ValueError('Recall generation budget exhausted; current index retained (active readers may pin old generations)')
        storage_policy.require_space(self.hub, remaining + metadata_bytes + 65536)
        # This generation is disposable until CURRENT is replaced. Disabling its
        # rollback journal avoids an unaccounted second database during a build.
        connection.execute('PRAGMA journal_mode=OFF')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute(f'PRAGMA max_page_count={pages}')

    def write_generation(self, path, raw):
        if size(self.root / 'generations') + len(raw) > self.policy['generation_budget_bytes']:
            raise ValueError('Recall generation budget exhausted; current index retained')
        storage_policy.require_space(self.hub, len(raw) + 65536)
        tx._replace(path, raw)

    @contextmanager
    def build_scope(self):
        try:
            yield
        finally:
            if self.directory is not None and current(self.root) != self.directory.name:
                shutil.rmtree(self.directory, ignore_errors=True)
            self.collection = collect_generations(self.hub)
