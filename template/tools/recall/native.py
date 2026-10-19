"""Bounded read-only adapters for local conversation metadata.

Private formats are schema-checked. No credential/config file is opened, no
service process is launched, and excerpts never constitute canonical results.
Ordinary session recovery uses only an exact saved path's stat, not discovery.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

SERVICES = ('codex', 'claude-code')
MAX_TAIL = 65536


def service_home(service):
    if service not in SERVICES:
        raise ValueError('service must be codex or claude-code')
    variable, default = ('CODEX_HOME', '~/.codex') if service == 'codex' else ('CLAUDE_CONFIG_DIR', '~/.claude')
    return Path(os.environ.get(variable, default)).expanduser().resolve()


def storage_instance(service):
    return hashlib.sha256(f'{service}:{service_home(service)}'.encode()).hexdigest()[:24]


def runtime_thread(service):
    # Claude has no consistently exposed native-session environment contract.
    return os.environ.get('CODEX_THREAD_ID') if service == 'codex' else None


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _time():
    return datetime.now(timezone.utc).isoformat()


def _stat(path):
    info = Path(path).stat()
    return dict(native_size=info.st_size, native_mtime_ns=info.st_mtime_ns)


def _allowed_history(path, service):
    """Only the adapter's transcript directories, never arbitrary supplied files."""
    home = service_home(service)
    allowed = [home / 'sessions', home / 'archived_sessions'] if service == 'codex' else [home / 'projects']
    return Path(path).suffix == '.jsonl' and any(_inside(path, root) for root in allowed)


def freshness(service, metadata, captured):
    path = metadata.get('path') if isinstance(metadata, dict) else None
    if not path or not _allowed_history(path, service):
        return dict(state='unknown', reason='No validated native transcript path; saved checkpoint remains usable.')
    try:
        current = _stat(path)
    except OSError as exc:
        return dict(state='unavailable', reason=f'Native history unavailable: {type(exc).__name__}')
    cursor = captured.get('cursor') if isinstance(captured, dict) else None
    if type(cursor) is not int or cursor < 0:
        return dict(state='unknown', reason='Checkpoint has no captured byte cursor.', observed=current)
    if cursor > current['native_size']:
        return dict(state='stale', reason='Native history was truncated/replaced after capture.', observed=current)
    if cursor < current['native_size']:
        return dict(state='catch-up-pending', uncaptured_bytes=current['native_size'] - cursor, observed=current)
    if captured.get('native_mtime_ns') != current['native_mtime_ns']:
        return dict(state='unknown', reason='End cursor matches, but captured file version is unknown or changed.', observed=current)
    if captured.get('scope') != 'parent-inspected-conversation':
        return dict(state='unknown', reason='File version matches, but selected excerpts or an unspecified capture scope do not establish full conversation capture.',
                    observed=current, capture_scope=captured.get('scope', 'unspecified'))
    return dict(state='current', reason='Captured byte cursor and observed file version match.', observed=current)


def _base(service):
    return dict(service=service, storage_instance=storage_instance(service), adapter_version=1,
                observed_at=_time(), candidates=[], warnings=[], coverage={})


def _codex(hub, limit, thread):
    result = _base('codex')
    database = service_home('codex') / 'state_5.sqlite'
    result['coverage'] = dict(source=str(database), scope='exact resolved workspace and its project directories',
                              limit=limit, private_format=True)
    if not database.is_file():
        result.update(state='unavailable')
        result['warnings'].append('Expected Codex state_5.sqlite is absent; no alternate history sweep attempted.')
        return result
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.15)) as db:
            db.row_factory = sqlite3.Row
            deadline = time.monotonic() + .2
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute('PRAGMA query_only=ON')
            columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            required = {'id', 'cwd', 'rollout_path', 'updated_at', 'title'}
            if not required <= columns:
                result.update(state='schema-unsupported')
                result['warnings'].append('Codex thread schema is missing required columns: ' + ', '.join(sorted(required - columns)))
                return result
            optional = ['cli_version', 'thread_source', 'forked_from_id', 'parent_thread_id']
            selected = sorted(required) + [c for c in optional if c in columns]
            # Match only scoped paths before projecting title/history metadata.
            root = str(Path(hub).resolve())
            escaped = root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            where, args = "(cwd = ? OR cwd LIKE ? ESCAPE '\\')", [root, escaped + '/%']
            if thread:
                where += ' AND id = ?'
                args.append(thread)
            rows = db.execute('SELECT ' + ','.join(selected) + ' FROM threads WHERE ' + where + ' ORDER BY updated_at DESC LIMIT ?', args + [limit + 1]).fetchall()
            result['coverage']['has_more'] = len(rows) > limit
            for raw in rows[:limit]:
                row = dict(raw)
                if not _inside(row['cwd'], hub):
                    continue
                item = dict(native_thread=row['id'], cwd=row['cwd'], title=row['title'],
                            updated_at=row['updated_at'], version=row.get('cli_version'),
                            path=row['rollout_path'], workspace_provenance='threads.cwd')
                if not _allowed_history(item['path'], 'codex'):
                    item.pop('path')
                    item['history_state'] = 'unavailable-path'
                else:
                    try:
                        item.update(_stat(item['path']))
                    except OSError:
                        item['history_state'] = 'unavailable'
                item['fork_parent'] = row.get('forked_from_id') or row.get('parent_thread_id')
                result['candidates'].append(item)
            result.update(state='available')
            result['coverage']['schema'] = 'state_5.sqlite:threads required columns verified'
    except (OSError, sqlite3.Error) as exc:
        result.update(state='unavailable')
        result['warnings'].append(f'Codex metadata inaccessible: {type(exc).__name__}: {exc}')
    return result


def _claude_key(path):
    return re.sub(r'[^a-zA-Z0-9]', '-', str(Path(path).resolve()))


def _header(path, maximum=16384):
    with Path(path).open('rb') as stream:
        data = stream.read(maximum)
    found = {}
    malformed = 0
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            continue
        if isinstance(row, dict):
            for key in ('sessionId', 'cwd', 'version', 'timestamp', 'parentSessionId', 'forkedFromSessionId'):
                if key in row and key not in found:
                    found[key] = row[key]
        if {'sessionId', 'cwd'} <= found.keys():
            break
    return found, malformed


def _claude(hub, projects, limit, thread):
    result = _base('claude-code')
    home = service_home('claude-code') / 'projects'
    roots = [Path(hub).resolve()] + [Path(hub).resolve() / 'projects' / p for p in projects]
    directories = [home / _claude_key(root) for root in roots]
    coverage = dict(scope='exact hub and manifest project encoded directories only', private_format=True,
                    limit=limit, directory_entry_limit=512, header_byte_limit=16384, scanned_files=0,
                    skipped_unverified=0, malformed_headers=0, has_more=False, paths=[str(p) for p in directories])
    result['coverage'] = coverage
    deadline = time.monotonic() + 1.0
    files = []
    try:
        for directory in directories:
            if not directory.is_dir():
                continue
            with os.scandir(directory) as iterator:
                for count, item in enumerate(iterator):
                    if count >= 512 or time.monotonic() > deadline:
                        coverage['has_more'] = True
                        break
                    if item.name.endswith('.jsonl') and (not thread or item.name == thread + '.jsonl'):
                        files.append((item.stat().st_mtime_ns, Path(item.path)))
        for _, path in sorted(files, reverse=True)[:limit]:
            if time.monotonic() > deadline:
                coverage['has_more'] = True
                break
            if not _allowed_history(path, 'claude-code'):
                continue
            metadata, malformed = _header(path)
            coverage['scanned_files'] += 1
            coverage['malformed_headers'] += malformed
            if not isinstance(metadata.get('cwd'), str) or not _inside(metadata['cwd'], hub) or metadata.get('sessionId') != path.stem:
                coverage['skipped_unverified'] += 1
                continue
            result['candidates'].append(dict(native_thread=metadata['sessionId'], cwd=metadata['cwd'],
                title=None, updated_at=metadata.get('timestamp'), version=metadata.get('version'), path=str(path),
                workspace_provenance='bounded transcript header cwd/sessionId',
                fork_parent=metadata.get('forkedFromSessionId') or metadata.get('parentSessionId'), **_stat(path)))
        coverage['has_more'] |= len(files) > limit
        result['state'] = 'available' if files else 'unavailable'
        if not files:
            result['warnings'].append('No matching Claude Code local transcripts in the exact scoped directories.')
        if coverage['skipped_unverified']:
            result['warnings'].append('Some headers lacked a recognized matching workspace/session schema; not adopted.')
    except OSError as exc:
        result['state'] = 'unavailable'
        result['warnings'].append(f'Claude metadata inaccessible: {type(exc).__name__}: {exc}')
    return result


def discover(hub, service, projects=(), limit=40, thread=None):
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError('native discovery limit must be between 1 and 200')
    if service == 'codex':
        return _codex(hub, limit, thread)
    if service == 'claude-code':
        return _claude(hub, projects, limit, thread)
    raise ValueError('service must be codex or claude-code')


def tail(service, metadata, maximum=MAX_TAIL, after=None):
    """Read only a selected validated candidate. Omitted bytes remain explicit."""
    path = metadata.get('path')
    if not path or not _allowed_history(path, service):
        return dict(state='unavailable', reason='No validated selected transcript path.')
    if not 1 <= maximum <= MAX_TAIL:
        raise ValueError(f'tail maximum must be between 1 and {MAX_TAIL}')
    start_time = time.monotonic()
    try:
        before = _stat(path)
        size = before['native_size']
        start = max(0, size - maximum, after if type(after) is int and 0 <= after <= size else 0)
        with Path(path).open('rb') as stream:
            stream.seek(max(0, start - 1))
            prior_byte = stream.read(1) if start else b'\n'
            stream.seek(start)
            data = stream.read(maximum)
        after_stat = _stat(path)
    except OSError as exc:
        return dict(state='unavailable', reason=type(exc).__name__)
    lines = data.splitlines()
    if start and prior_byte != b'\n':
        # A byte cursor need not be a line boundary; conservatively omit first.
        lines = lines[1:]
    messages, malformed, recognized, timed_out = [], 0, 0, False
    for line in lines:
        if time.monotonic() - start_time > .2:
            timed_out = True
            break
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(row, dict):
            continue
        body = row.get('payload', {}) if service == 'codex' else row.get('message', {})
        if not isinstance(body, dict):
            continue
        role = body.get('role') or (row.get('type') if service == 'claude-code' else None)
        if role not in ('user', 'assistant'):
            continue
        content = body.get('content', [])
        if isinstance(content, str):
            content = [dict(text=content)]
        if not isinstance(content, list):
            continue
        value = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict) and isinstance(part.get('text'), str))
        if value:
            recognized += 1
            messages.append(dict(role=role, text=value[:1400], truncated=len(value) > 1400,
                                 cursor=row.get('uuid') or row.get('timestamp')))
    return dict(state='available' if messages else 'schema-unrecognized-or-no-messages',
                excerpts=messages[-6:], recognized_messages=recognized, malformed_lines=malformed,
                omitted_prefix_bytes=start, read_bytes=len(data), file_changed=before != after_stat,
                parse_time_limit_reached=timed_out,
                suggested_capture=dict(cursor=size, **before, time=_time(), scope='selected-excerpts',
                                       omitted_prefix_bytes=start),
                scope='Selected bounded excerpts only; parent inspection required before checkpoint. Not canonical research results.')
