"""MathDesk hub data (builder C): records, sources, aids, documents, search, library.

Everything here is read-only. Nothing is written anywhere, no lock file is
created and no hub command function is called in-process.

- Catalog registries (``projects/<p>/catalog/{records,sources}.json``) are read
  directly under a non-blocking *shared* flock of ``.hub/locks/publication.lock``.
  The lock is taken only if the file already exists, and briefly retried. If the
  gate stays busy, the previous snapshot keeps serving. Snapshots are rechecked
  by file stat at most once every two seconds.
- Knowledge envelopes are parsed only up to their ``aids`` section, because the
  frontier checkpoints that follow it can make the file hundreds of megabytes.
- Full-text search reads the recall FTS generation read-only (``immutable=1``)
  for document hits; records, sources and aids are matched in memory.

The hub's ``hub.format_record`` is not reused. It depends on module globals that
are set by ``@command_snapshot`` and it rehashes the whole transitive evidence
closure. Record cards here use the same layout without hash checks.

Requires Python 3.11 or later (the server interpreter). Uses only the stdlib.
"""
from __future__ import annotations

import codecs
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import threading
import time


class HubBusy(RuntimeError):
    """The publication gate stayed busy and there is no earlier snapshot to serve."""


DOC_EXTENSIONS = ('.md', '.txt', '.tex')      # readable through read_document
LISTED_EXTENSIONS = ('.md', '.tex')           # listed by documents(), like hub.document_paths
MAX_DOCUMENT_BYTES = 3 * 1024 * 1024
CATALOG_CHECK_SECONDS = 2.0
DOCUMENT_TTL_SECONDS = 10.0
SEARCH_DEADLINE_SECONDS = 1.5
SEARCH_CACHE_SIZE = 128
LOCK_RETRY_PAUSES = (0.1, 0.25, 0.5)
ID_LETTERS = 'RCOQLK'
OUTGOING = ('depends_on', 'source_refs', 'subsumed_by')
# Mirrors hub.document_paths / recall.corpus. Indexes are added separately.
SKIP_DIRS = {'indexes', 'catalog', 'archives', 'migration-baseline', 'migration-stage',
             'staging', '__pycache__', 'consultations', 'node_modules'}
RAW_NAME = re.compile(r'(?:^|[_.-])(?:PROMPT|RAW|STDERR|STREAM|EXECUTION|RESPONSE)(?:[_.-]|$)', re.I)
STOP = {'a', 'an', 'the', 'of', 'to', 'for', 'and', 'or', 'in', 'on', 'with', 'from', 'is',
        'are', 'how', 'can', 'does', 'what'}
KIND_ORDER = {'hub': 0, 'project': 1, 'note': 2, 'goal': 3, 'proof': 4, 'investigation': 5,
              'index': 6, 'harness': 7, 'template': 8, 'document': 9}
ROOT_FORM = re.compile(r'([a-z][a-z0-9_-]*):(.+)', re.S)
ID_LIKE = re.compile(r'(?:[A-Z][A-Z0-9]*-)?[A-Z]\d*')
LIBRARY_PATH = re.compile(r'projects/[a-z0-9-]+/library/((?:[A-Z][A-Z0-9]*-)?[A-Z]\d{3,})/')
AID_NOTICE = ('Research aid or operational context; no theorem authority or automatic '
              'applicability certification.')


class _Config:
    """Derived view of hub.json."""

    def __init__(self, hub_root: str, cfg: dict):
        self.cfg = cfg
        self.projects = cfg.get('projects', {}) if isinstance(cfg.get('projects'), dict) else {}
        self.default_project = cfg.get('legacy_default_project', cfg.get('default_project'))
        self.layout = cfg.get('catalog_layout', 'federated')
        self.roots = {}
        for name, value in (cfg.get('roots') or {}).items():
            if isinstance(name, str) and isinstance(value, str) and name != 'hub':
                self.roots[name] = os.path.realpath(os.path.expanduser(value))
        users = defaultdict(set)
        for project, info in self.projects.items():
            for root in info.get('roots', []) if isinstance(info, dict) else []:
                users[root].add(project)
        self.root_owner = {root: next(iter(owners)) for root, owners in users.items() if len(owners) == 1}
        routes = []
        for route in cfg.get('infrastructure_routes', []) or []:
            if isinstance(route, str):
                routes.append((route.strip('/'), None))
        for project, info in self.projects.items():
            for route in (info.get('document_routes') if isinstance(info, dict) else None) or [f'projects/{project}']:
                if isinstance(route, str):
                    routes.append((route.strip('/'), project))
        # Longest route first; project routes win over infrastructure on ties.
        self.routes = sorted(routes, key=lambda item: (-len(item[0]), item[1] is None))
        prefixes = sorted({info.get('id_prefix', '') for info in self.projects.values()
                           if isinstance(info, dict) and isinstance(info.get('id_prefix'), str)} - {''},
                          key=lambda p: (-len(p), p))
        self.prefixes = [p for p in prefixes if re.fullmatch(r'[A-Z][A-Z0-9]*-', p)]

    def catalog_files(self) -> list:
        """(relative path, kind, owner) for every canonical registry and knowledge file."""
        files = []
        if self.layout == 'legacy':
            files += [('catalog/records.json', 'records', None), ('catalog/sources.json', 'sources', None)]
        else:
            for project in self.projects:
                if re.fullmatch(r'[a-z0-9][a-z0-9-]*', project):
                    files += [(f'projects/{project}/catalog/records.json', 'records', project),
                              (f'projects/{project}/catalog/sources.json', 'sources', project)]
        for project in self.projects:
            if re.fullmatch(r'[a-z0-9][a-z0-9-]*', project):
                files.append((f'projects/{project}/catalog/knowledge.json', 'knowledge', project))
        files.append(('catalog/knowledge.json', 'knowledge', 'hub'))
        return files


class _Snapshot:
    """One immutable catalog view. Entry dicts must not be mutated."""

    def __init__(self, signature=None, token=0):
        self.signature = signature
        self.token = token
        self.loaded_at = time.time()
        self.entries = {}          # id -> info dict (entry, category, project, file, ...)
        self.incoming = defaultdict(list)   # id -> ids whose supersedes contains it
        self.warnings = []
        self.files = {}            # rel -> (mtime_ns, size) as read


def _casefold(value) -> str:
    return ' '.join(str(value).split()).casefold()


def _plain(value) -> str:
    """Readable text of a JSON field (aid payload, review), as recall.index.semantic_body."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(_plain(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get('text'), str):
            return value['text']
        return '\n'.join(f'{key}: {_plain(item)}' for key, item in value.items()
                         if key not in {'provenance', 'assessment', 'sha256'})
    return str(value)


def _clip(text: str, n: int) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + '\u2026'


def _snippet(text: str, terms: list, width: int = 220) -> str:
    flat = ' '.join(str(text or '').split())
    if not flat:
        return ''
    low = flat.casefold()
    hits = [low.find(t) for t in terms if t and low.find(t) >= 0]
    start = max(0, min(hits) - 70) if hits else 0
    piece = flat[start:start + width]
    return ('\u2026' if start else '') + piece + ('\u2026' if start + width < len(flat) else '')


def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        return False


def _parse_aids(raw: str):
    """Decode only the leading keys of a knowledge envelope, up to 'aids'.

    Raises _NeedMore if the text ends before the aids array is complete.
    """
    decoder = json.JSONDecoder()
    ws = re.compile(r'\s*')
    try:
        i = ws.match(raw, 0).end()
        if raw[i] != '{':
            raise ValueError('knowledge envelope is not an object')
        i = ws.match(raw, i + 1).end()
        while True:
            if raw[i] == '}':
                return []
            key, i = decoder.raw_decode(raw, i)
            i = ws.match(raw, i).end()
            if raw[i] != ':':
                raise ValueError('malformed knowledge envelope')
            i = ws.match(raw, i + 1).end()
            value, i = decoder.raw_decode(raw, i)
            if key == 'aids':
                if not isinstance(value, list):
                    raise ValueError('knowledge aids must be a list')
                return [a for a in value if isinstance(a, dict) and isinstance(a.get('id'), str)]
            i = ws.match(raw, i).end()
            if raw[i] == ',':
                i = ws.match(raw, i + 1).end()
            elif raw[i] == '}':
                return []
            else:
                raise ValueError('malformed knowledge envelope')
    except (IndexError, json.JSONDecodeError) as exc:
        raise _NeedMore() from exc


class _NeedMore(Exception):
    pass


def _read_aids(path: str):
    """Read a knowledge file progressively until its aids section is decoded."""
    try:
        stream = open(path, 'rb')
    except FileNotFoundError:
        return []
    with stream:
        decoder = codecs.getincrementaldecoder('utf-8')()
        text, chunk = '', 1 << 20
        while True:
            data = stream.read(chunk)
            eof = not data
            text += decoder.decode(data, final=eof)
            try:
                return _parse_aids(text)
            except _NeedMore:
                if eof:
                    raise ValueError(f'truncated or malformed knowledge file {path}')
                chunk = min(chunk * 2, 64 << 20)


class Hub:
    """Read-only view of one research hub for the MathDesk server (thread-safe)."""

    def __init__(self, root: str):
        self.root = os.path.realpath(os.path.expanduser(str(root)))
        self._internal = os.path.join(self.root, '.hub')
        self._cfg_lock = threading.Lock()
        self._conf = None
        self._conf_sig = None
        self._conf_checked = float('-inf')
        self._reload_lock = threading.Lock()
        self._snap = None
        self._last_check = float('-inf')
        self._token = 0
        self._docs_lock = threading.Lock()
        self._docs = None
        self._docs_at = 0.0
        self._docs_refreshing = False
        self._titles = {}
        self._search_lock = threading.Lock()
        self._search_cache = OrderedDict()
        self._norm_lock = threading.Lock()
        self._norm = None

    # ------------------------------------------------------------------ config

    def _config(self) -> _Config:
        now = time.monotonic()
        with self._cfg_lock:
            if self._conf is not None and now - self._conf_checked < CATALOG_CHECK_SECONDS:
                return self._conf
            self._conf_checked = now
            path = os.path.join(self.root, 'hub.json')
            try:
                st = os.stat(path)
                sig = (st.st_mtime_ns, st.st_size)
                if self._conf is None or sig != self._conf_sig:
                    with open(path, 'rb') as stream:
                        cfg = json.loads(stream.read())
                    if isinstance(cfg, dict) and isinstance(cfg.get('projects'), dict):
                        self._conf, self._conf_sig = _Config(self.root, cfg), sig
            except (OSError, ValueError):
                pass
            if self._conf is None:
                self._conf = _Config(self.root, {'projects': {}, 'roots': {}})
            return self._conf

    def id_regex(self) -> str:
        """JS- and Python-compatible regex source for hub IDs.

        A prefixed ID (CP-R065) is matched whole; a bare ID must not follow a
        word character or a letter-hyphen pair, so the R in CP-R065, R^2, K0,
        L2-norm, R31 and xR031 are not matched. "KC-R008/R009" gives KC-R008
        and the bare R009, and "R031-R040" gives both. Three or more digits.
        """
        prefixes = self._config().prefixes
        prefix = '(?:(?:' + '|'.join(re.escape(p[:-1]) for p in prefixes) + ')-)?' if prefixes else ''
        return r'(?<!\w)(?<![A-Za-z]-)' + prefix + '[' + ID_LETTERS + r']\d{3,}(?!\w)'

    def projects(self) -> list:
        conf = self._config()
        out = []
        for name, info in conf.projects.items():
            info = info if isinstance(info, dict) else {}
            out.append(dict(name=name, title=info.get('title', name),
                            root=os.path.join(self.root, 'projects', name), path=f'projects/{name}',
                            id_prefix=info.get('id_prefix', ''), state=info.get('state')))
        return out

    def _project_of_rel(self, conf: _Config, rel: str):
        parts = rel.split('/')
        if len(parts) >= 2 and parts[0] == 'projects' and parts[1] in conf.projects:
            return parts[1]
        for route, owner in conf.routes:
            if route and (rel == route or rel.startswith(route + '/')):
                return owner
        return None

    def project_for_cwd(self, cwd: str):
        if not isinstance(cwd, str) or not cwd or '\x00' in cwd:
            return None
        conf = self._config()
        real = os.path.realpath(os.path.expanduser(cwd))
        if _inside(real, self.root) and real != self.root:
            rel = os.path.relpath(real, self.root).replace(os.sep, '/')
            owner = self._project_of_rel(conf, rel)
            if owner:
                return owner
        best = None
        for name, base in conf.roots.items():
            if _inside(real, base) and (best is None or len(base) > len(conf.roots[best])):
                best = name
        return conf.root_owner.get(best) if best else None

    # ------------------------------------------------------------------ catalog

    def _signature(self, conf: _Config):
        sig = []
        for rel in ['hub.json'] + [f for f, _, _ in conf.catalog_files()]:
            try:
                st = os.stat(os.path.join(self.root, rel))
                sig.append((rel, st.st_mtime_ns, st.st_size, st.st_ino))
            except OSError:
                sig.append((rel, None, None, None))
        return tuple(sig)

    def _acquire_gate(self):
        """Open and share-lock the publication gate; None when it does not exist."""
        path = os.path.join(self._internal, 'locks', 'publication.lock')
        try:
            stream = open(path, 'rb')       # never create the permanent lock file
        except OSError:
            return None
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise BlockingIOError('Hub is busy')
        return stream

    def _read_files(self, conf: _Config):
        data = {}
        for rel, kind, owner in conf.catalog_files():
            path = os.path.join(self.root, rel)
            if kind == 'knowledge':
                try:
                    data[rel] = ('aids', _read_aids(path))
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    data[rel] = ('error', str(exc))
            else:
                try:
                    with open(path, 'rb') as stream:
                        data[rel] = ('bytes', stream.read())
                except FileNotFoundError:
                    data[rel] = ('bytes', None)
        return data

    def _load(self, conf: _Config, signature, previous):
        warnings = []
        data = None
        for pause in (0.0,) + LOCK_RETRY_PAUSES:
            if pause:
                time.sleep(pause)
            try:
                gate = self._acquire_gate()
            except BlockingIOError:
                continue
            try:
                data = self._read_files(conf)
            finally:
                if gate is not None:
                    try:
                        fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
                    finally:
                        gate.close()
            break
        if data is None:
            if previous is not None:
                previous.warnings = previous.warnings[-4:] + ['Hub is busy (publication in progress); serving the previous catalog snapshot.']
                return previous
            raise HubBusy('Hub is busy (publication in progress); try again in a moment.')
        self._token += 1
        snap = _Snapshot(signature, self._token)
        snap.warnings = warnings
        snap.files = {s[0]: (s[1], s[2]) for s in signature}
        by_project_files = {rel: (kind, owner) for rel, kind, owner in conf.catalog_files()}
        for rel, (form, payload) in data.items():
            kind, owner = by_project_files[rel]
            if form == 'error':
                snap.warnings.append(f'Unreadable knowledge file {rel}: {payload}')
                items = [i['entry'] for i in previous.entries.values() if i['file'] == rel] if previous else []
                category = 'aid'
            elif form == 'aids':
                items, category = payload, 'aid'
            else:
                if payload is None:
                    snap.warnings.append(f'Missing registry {rel}')
                    continue
                try:
                    items = json.loads(payload)
                except ValueError as exc:
                    snap.warnings.append(f'Unreadable registry {rel}: {exc}')
                    if previous is not None:
                        # Keep the previous version of this file's entries.
                        items = [i['entry'] for i in previous.entries.values() if i['file'] == rel]
                    else:
                        continue
                if not isinstance(items, list):
                    snap.warnings.append(f'Registry {rel} is not a list')
                    continue
                category = 'record' if kind == 'records' else 'source'
            for entry in items:
                if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
                    continue
                key = entry['id']
                if key in snap.entries:
                    snap.warnings.append(f'Duplicate ID {key} in {rel}; first occurrence kept')
                    continue
                project = entry.get('project')
                if not isinstance(project, str) or not project:
                    project = owner if owner and owner != 'hub' else (None if owner == 'hub' else conf.default_project)
                snap.entries[key] = self._index_entry(entry, category, project, rel)
        for key, info in snap.entries.items():
            if info['category'] == 'aid':
                continue
            for target in info['entry'].get('supersedes', []) or []:
                if isinstance(target, str):
                    snap.incoming[target].append(key)
        return snap

    @staticmethod
    def _index_entry(entry, category, project, rel):
        title_extra = ''
        if category == 'record':
            title = str(entry.get('title') or entry['id'])
            status = entry.get('status')
            kind = entry.get('kind') or 'record'
            body = [entry.get('statement'), _plain(entry.get('hypotheses')), entry.get('limitations'),
                    ' '.join(entry.get('topics') or []), entry.get('reopening_condition')]
            snippet_source = entry.get('statement') or ''
        elif category == 'source':
            citation = str(entry.get('citation') or entry['id'])
            title = _clip(citation, 240)
            status = entry.get('check_state')
            kind = 'source'
            library = entry.get('library') if isinstance(entry.get('library'), dict) else {}
            body = [citation, entry.get('use'), entry.get('locator'), entry.get('version'),
                    entry.get('limitations'), ' '.join(entry.get('topics') or []),
                    ' '.join(library.get('aliases') or []), library.get('availability_note')]
            snippet_source = entry.get('use') or citation
            title_extra = ' '.join(library.get('aliases') or [])
        else:
            title = str(entry.get('title') or entry['id'])
            disposition = entry.get('disposition')
            assessment = entry.get('assessment') if isinstance(entry.get('assessment'), dict) else {}
            status = disposition if isinstance(disposition, str) else assessment.get('state')
            kind = entry.get('kind') or 'aid'
            body = [_plain(entry.get('payload'))]
            snippet_source = body[0]
        hay_title = _casefold(f"{entry['id']} {title} {title_extra}")
        return dict(entry=entry, category=category, project=project, file=rel, title=title,
                    status=status, kind=kind, hay_title=hay_title,
                    hay_body=_casefold(' '.join(str(b) for b in body if b)), snippet_source=snippet_source)

    def _snapshot(self) -> _Snapshot:
        snap = self._snap
        if snap is not None and time.monotonic() - self._last_check < CATALOG_CHECK_SECONDS:
            return snap
        if snap is None:
            with self._reload_lock:
                if self._snap is None:
                    self._refresh()
            return self._snap
        if self._reload_lock.acquire(blocking=False):
            try:
                if time.monotonic() - self._last_check >= CATALOG_CHECK_SECONDS:
                    self._refresh()
            finally:
                self._reload_lock.release()
        return self._snap

    def _refresh(self):
        """Caller holds _reload_lock."""
        conf = self._config()
        signature = self._signature(conf)
        previous = self._snap
        try:
            if previous is None or previous.signature != signature:
                self._snap = self._load(conf, signature, previous)
        except HubBusy:
            if previous is None:
                raise          # no consistent snapshot yet: callers report "busy" and retry
            previous.warnings = previous.warnings[-4:] + ['Hub is busy; serving the previous catalog snapshot.']
        except Exception as exc:  # keep serving; report through status()
            if previous is None:
                self._snap = _Snapshot(None, 0)
                self._snap.warnings.append(f'Catalog load failed: {exc}')
            else:
                previous.warnings = previous.warnings[-4:] + [f'Catalog reload failed: {exc}']
        self._last_check = time.monotonic()

    def status(self) -> dict:
        """Diagnostics for `mathdesk doctor` (extension, not in the frozen interface)."""
        snap = self._snapshot()
        counts = defaultdict(int)
        for info in snap.entries.values():
            counts[info['category']] += 1
        return dict(root=self.root, entries=dict(counts), loaded_at=snap.loaded_at,
                    warnings=list(snap.warnings), fts_index=self._fts_path())

    def known_ids(self) -> list:
        return sorted(self._snapshot().entries)

    # ------------------------------------------------------------------ paths

    def _locate(self, conf: _Config, root, rel):
        """(display path for read_document, absolute real path) or None."""
        if not isinstance(root, str) or not isinstance(rel, str) or not rel or '\x00' in rel:
            return None
        base = self.root if root == 'hub' else conf.roots.get(root)
        if base is None or os.path.isabs(rel):
            return None
        real = os.path.realpath(os.path.join(base, rel))
        if not _inside(real, base):
            return None
        if _inside(real, self.root):
            if _inside(real, self._internal):
                return None
            return os.path.relpath(real, self.root).replace(os.sep, '/'), real
        clean = os.path.normpath(rel).replace(os.sep, '/')
        return f'{root}:{clean}', real

    def _resolve_document(self, relpath):
        """(absolute real path, display path, owner) or (None, reason, None)."""
        if not isinstance(relpath, str) or not relpath or len(relpath) > 1024:
            return None, 'invalid path', None
        if '\x00' in relpath or '\\' in relpath:
            return None, 'invalid characters', None
        conf = self._config()
        base, rel, root_name = self.root, relpath, 'hub'
        match = ROOT_FORM.fullmatch(relpath)
        if match and match.group(1) == 'hub':
            rel = match.group(2)
        elif match and match.group(1) in conf.roots:
            root_name, rel = match.group(1), match.group(2)
            base = conf.roots[root_name]
        if rel.startswith(('/', '~')) or os.path.isabs(rel):
            return None, 'absolute paths are refused', None
        parts = rel.split('/')
        if any(p in ('', '.', '..') for p in parts):
            return None, 'path must be normalized and relative', None
        if any(p.startswith('.') for p in parts):
            return None, 'hidden paths are refused', None
        if os.path.splitext(rel)[1].lower() not in DOC_EXTENSIONS:
            return None, 'only .md, .txt and .tex documents are served', None
        real = os.path.realpath(os.path.join(base, rel))
        if not _inside(real, base) or real == base:
            return None, 'path escapes its root', None
        if _inside(real, self._internal):
            return None, '.hub is never served', None
        if os.path.splitext(real)[1].lower() not in DOC_EXTENSIONS:
            return None, 'link target is not a document', None
        inner = os.path.relpath(real, base).replace(os.sep, '/')
        if any(p.startswith('.') for p in inner.split('/')):
            return None, 'hidden paths are refused', None
        if root_name == 'hub':
            display = inner
            owner = self._project_of_rel(conf, inner)
        else:
            display = f'{root_name}:{rel}'
            owner = conf.root_owner.get(root_name)
            if _inside(real, self.root):
                owner = self._project_of_rel(conf, os.path.relpath(real, self.root).replace(os.sep, '/')) or owner
        return real, display, owner

    def document_error(self, relpath):
        """Why read_document(relpath) would refuse it, or None (extension)."""
        real, reason, _ = self._resolve_document(relpath)
        if real is None:
            return reason
        try:
            st = os.stat(real)
        except OSError:
            return 'not found'
        if not stat.S_ISREG(st.st_mode):
            return 'not a regular file'
        if st.st_size > MAX_DOCUMENT_BYTES:
            return 'document exceeds 3 MB'
        return None

    def read_document(self, relpath: str):
        real, display, owner = self._resolve_document(relpath)
        if real is None:
            return None
        try:
            with open(real, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_DOCUMENT_BYTES:
                    return None
                data = stream.read(MAX_DOCUMENT_BYTES + 1)
        except OSError:
            return None
        if len(data) > MAX_DOCUMENT_BYTES:
            return None
        return dict(path=display, text=data.decode('utf-8', errors='replace'), mtime=st.st_mtime,
                    project=owner, size=len(data))

    # ------------------------------------------------------------------ records

    def _lookup(self, ident):
        if not isinstance(ident, str):
            return None, None
        key = ident.strip().strip('.,;:()[]{}<>`*"\'')
        snap = self._snapshot()
        info = snap.entries.get(key) or snap.entries.get(key.upper())
        return snap, info

    def _context(self, snap: _Snapshot, seed: str):
        """Mirror of recall.retrieve._current_context over this snapshot."""
        queue, seen, corrections, prerequisites, missing = [seed], set(), {}, {}, set()
        while queue:
            key = queue.pop()
            if key in seen:
                continue
            seen.add(key)
            info = snap.entries.get(key)
            if info is None:
                missing.add(key)
                continue
            for correction in snap.incoming.get(key, []):
                corrections.setdefault(correction, set()).add(key)
                queue.append(correction)
            entry = info['entry']
            targets = []
            if info['category'] == 'aid':
                targets = [r.get('id') for r in entry.get('references', []) or [] if isinstance(r, dict)]
            else:
                for relation in OUTGOING:
                    targets += list(entry.get(relation, []) or [])
            for target in targets:
                if not isinstance(target, str):
                    continue
                found = snap.entries.get(target)
                if found is not None and found['category'] != 'aid' and target != seed:
                    prerequisites[target] = found
                queue.append(target)
        return corrections, prerequisites, sorted(missing)

    def _brief(self, key, info):
        return dict(id=key, title=info['title'], status=info['status'], project=info['project'],
                    kind=info['kind'], category=info['category'])

    def _paths_for(self, conf, info):
        entry, items = info['entry'], []
        for item in entry.get('evidence', []) or []:
            items.append(('evidence', item))
        for key, value in entry.items():
            if 'review' in key:
                for review in value if isinstance(value, list) else [value]:
                    if isinstance(review, dict) and isinstance(review.get('artifact'), dict):
                        items.append(('review', review['artifact']))
        library = entry.get('library') if isinstance(entry.get('library'), dict) else {}
        for copy_ in library.get('copies', []) or []:
            items.append(('library copy', copy_))
        for item in entry.get('provenance', []) or []:
            items.append(('provenance', item))
        out, seen = [], set()
        for label, item in items:
            if not isinstance(item, dict):
                continue
            located = self._locate(conf, item.get('root'), item.get('path'))
            if located is None:
                continue
            display, real = located
            if os.path.splitext(real)[1].lower() not in DOC_EXTENSIONS or display in seen:
                continue
            seen.add(display)
            out.append(dict(path=display, label=label, locator=item.get('locator'),
                            exists=os.path.isfile(real)))
        return out

    def record(self, ident: str):
        snap, info = self._lookup(ident)
        if info is None:
            return None
        conf = self._config()
        key = info['entry']['id']
        corrections, prerequisites, unresolved = self._context(snap, key)
        direct = set(snap.incoming.get(key, []))
        correction_list = []
        for cid, targets in corrections.items():
            cinfo = snap.entries.get(cid)
            if cinfo is None:
                continue
            item = self._brief(cid, cinfo)
            item.update(supersedes=sorted(targets), direct=cid in direct)
            correction_list.append(item)
        correction_list.sort(key=lambda c: (not c['direct'], c['id']))
        pdf = self.library_info(key) if info['category'] == 'source' or self._has_pdf_ref(info) else None
        if info['category'] == 'record':
            body = self._record_markdown(conf, snap, info, correction_list)
        elif info['category'] == 'source':
            body = self._source_markdown(conf, snap, info, correction_list, pdf)
        else:
            body = self._aid_markdown(conf, snap, info, correction_list)
        return dict(id=key, project=info['project'], category=info['category'], kind=info['kind'],
                    title=info['title'], status=info['status'], body_markdown=body,
                    fields=copy.deepcopy(info['entry']), corrections=correction_list,
                    context=dict(prerequisites=[self._brief(k, v) for k, v in sorted(prerequisites.items())][:200],
                                 unresolved=unresolved),
                    paths=self._paths_for(conf, info), pdf=bool(pdf and pdf.get('pdf')),
                    pdf_reason=(pdf or {}).get('reason'), file=info['file'])

    @staticmethod
    def _has_pdf_ref(info):
        entry = info['entry']
        return any(isinstance(e, dict) and str(e.get('path', '')).lower().endswith('.pdf')
                   for e in entry.get('evidence', []) or [])

    def _evidence_line(self, conf, item):
        if not isinstance(item, dict):
            return '- ' + str(item)
        root, rel = item.get('root'), item.get('path')
        located = self._locate(conf, root, rel)
        present = located is not None and os.path.exists(located[1])
        line = f'- `{root}:{rel}`'
        if item.get('locator'):
            line += ' \u2014 ' + str(item['locator'])
        if not present:
            line += ' (missing on disk)'
        return line

    def _review_lines(self, conf, entry):
        lines = []
        for key, value in entry.items():
            if 'review' not in key:
                continue
            for review in value if isinstance(value, list) else [value]:
                if isinstance(review, dict):
                    head = f"- {key}" + (f" ({review['date']})" if review.get('date') else '')
                    if review.get('scope'):
                        head += ': ' + str(review['scope'])
                    lines.append(head)
                    if isinstance(review.get('artifact'), dict):
                        lines.append('  ' + self._evidence_line(conf, review['artifact']))
                    rest = {k: v for k, v in review.items() if k not in {'date', 'scope', 'artifact'}}
                    if rest:
                        lines.append('  - ' + _clip(_plain(rest), 600))
                elif review:
                    lines.append(f'- {key}: {_clip(_plain(review), 600)}')
        return lines

    def _evidence_summary(self, conf, entry):
        items = [e for e in entry.get('evidence', []) or [] if isinstance(e, dict)]
        present = 0
        for item in items:
            located = self._locate(conf, item.get('root'), item.get('path'))
            present += bool(located and os.path.exists(located[1]))
        return f'{present}/{len(items)} evidence files present; hashes not rechecked here (hub.py show checks freshness)'

    @staticmethod
    def _correction_lines(corrections):
        if not corrections:
            return []
        direct = [c for c in corrections if c['direct']]
        other = [c for c in corrections if not c['direct']]
        lines = ['']
        if direct:
            lines.append('**Read the corrections:** ' + '; '.join(
                f"{c['id']} \u2014 {c['title']} ({c['status']})" for c in direct))
        if other:
            lines.append('')
            lines.append('**Corrections to prerequisites:** ' + '; '.join(
                f"{c['id']} \u2014 {c['title']} (corrects {', '.join(c['supersedes'])})" for c in other))
        return lines

    def _record_markdown(self, conf, snap, info, corrections):
        r = info['entry']
        lines = [f"## {r['id']} \u2014 {info['title']}", '',
                 f"Owner: {info['project']}; {self._evidence_summary(conf, r)}", '',
                 f"{r.get('kind')} | status: {r.get('status')} | evidence: {r.get('evidence_level')} | novelty: {r.get('novelty')}",
                 f"Topics: {', '.join(r.get('topics') or [])}", '', str(r.get('statement') or ''), '',
                 '**Hypotheses/scope**', '']
        lines += ['- ' + str(x) for x in r.get('hypotheses') or []] or ['- (none recorded)']
        lines += ['', '**Limits**', '', str(r.get('limitations') or ''), '', '**Evidence**', '']
        lines += [self._evidence_line(conf, x) for x in r.get('evidence') or []] or ['- (none recorded)']
        reviews = self._review_lines(conf, r)
        if reviews:
            lines += ['', '**Review**', ''] + reviews
        for field in OUTGOING[:1] + ('supersedes', 'subsumed_by', 'source_refs'):
            if r.get(field):
                lines += ['', f"{field}: {', '.join(r[field])}"]
        lines += self._correction_lines(corrections)
        if r.get('reopening_condition'):
            lines += ['', 'Reopen only with: ' + str(r['reopening_condition'])]
        return '\n'.join(lines)

    def _source_markdown(self, conf, snap, info, corrections, pdf):
        s = info['entry']
        library = s.get('library') if isinstance(s.get('library'), dict) else {}
        lines = [f"## {s['id']} \u2014 {info['title']}", '',
                 f"Owner: {info['project']}; source; check state: {s.get('check_state')}; {self._evidence_summary(conf, s)}",
                 f"Topics: {', '.join(s.get('topics') or [])}", '',
                 f"**Citation:** {s.get('citation', '')}", '']
        for label, field in (('URL', 'url'), ('Version', 'version'), ('Locator', 'locator'),
                             ('Use', 'use'), ('Limits', 'limitations')):
            if s.get(field):
                value = f'<{s[field]}>' if field == 'url' and re.match(r'https?://\S+$', str(s[field])) else s[field]
                lines += [f'**{label}:** {value}', '']
        if library:
            lines += [f"**Library:** {library.get('kind', 'entry')}"
                      + (f"; aliases: {', '.join(library.get('aliases') or [])}" if library.get('aliases') else '')]
            if library.get('availability_note'):
                lines += ['', 'Availability: ' + str(library['availability_note'])]
            copies = library.get('copies') or []
            if copies:
                lines += ['', 'Copies:', '']
                for c in copies:
                    lines.append(self._evidence_line(conf, c) + (f" [{c.get('role')}]" if c.get('role') else ''))
            lines.append('')
        if pdf and pdf.get('pdf'):
            lines += [f"**Local PDF:** available (`{pdf.get('display')}`, {pdf.get('role')})", '']
        else:
            lines += [f"**Local PDF:** not available \u2014 {(pdf or {}).get('reason') or 'no retained PDF'}", '']
        lines += ['**Evidence**', '']
        lines += [self._evidence_line(conf, x) for x in s.get('evidence') or []] or ['- (none recorded)']
        reviews = self._review_lines(conf, s)
        if reviews:
            lines += ['', '**Review**', ''] + reviews
        lines += self._correction_lines(corrections)
        return '\n'.join(lines)

    def _aid_markdown(self, conf, snap, info, corrections):
        a = info['entry']
        assessment = a.get('assessment') if isinstance(a.get('assessment'), dict) else {}
        lines = [f"## {a['id']} \u2014 {info['title']}", '',
                 f"Owner: {info['project'] or 'hub'}; knowledge aid ({info['kind']}); "
                 f"disposition: {a.get('disposition') if isinstance(a.get('disposition'), str) else '-'}"
                 + (f"; revision {a['revision']}" if a.get('revision') is not None else ''), '',
                 f'*{AID_NOTICE}*', '']
        payload = a.get('payload')
        if isinstance(payload, dict):
            for name, value in payload.items():
                lines += [f'**{name.replace("_", " ")}:** {_clip(_plain(value), 4000)}', '']
        elif payload:
            lines += [_clip(_plain(payload), 4000), '']
        refs = [r for r in a.get('references', []) or [] if isinstance(r, dict)]
        if refs:
            lines += ['**References:** ' + '; '.join(
                f"{r.get('id')} ({r.get('relation', 'reference')})" for r in refs), '']
        if assessment:
            lines += ['**Assessment:** ' + ' \u2014 '.join(str(assessment[k]) for k in ('state', 'scope') if assessment.get(k)), '']
        prov = [p for p in a.get('provenance', []) or [] if isinstance(p, dict)]
        if prov:
            lines += [f'**Provenance** ({len(prov)} items)', '']
            lines += [self._evidence_line(conf, p) for p in prov[:12]]
            if len(prov) > 12:
                lines.append(f'- \u2026 {len(prov) - 12} more')
        lines += self._correction_lines(corrections)
        return '\n'.join(lines).rstrip()

    # ------------------------------------------------------------------ library

    def _pdf_ok(self, conf, item):
        """(display, real) for a readable PDF inside the hub or an archive root, else reason."""
        if not isinstance(item, dict):
            return None, 'invalid copy'
        located = self._locate(conf, item.get('root'), item.get('path'))
        label = f"{item.get('root')}:{item.get('path')}"
        if located is None:
            return None, f'{label} is outside the hub and registered archive roots'
        display, real = located
        if not real.lower().endswith('.pdf'):
            return None, f'{label} is not a .pdf'
        try:
            if not os.path.isfile(real):
                return None, f'{label} is missing on disk'
            with open(real, 'rb') as stream:
                if not stream.read(1024).lstrip().startswith(b'%PDF-'):
                    return None, f'{label} does not start with a PDF header'
        except OSError as exc:
            return None, f'{label} unreadable: {exc.strerror}'
        return (display, real), None

    def library_info(self, ident: str) -> dict:
        """{"id","pdf","display","role","reason"} for the retained PDF (extension)."""
        snap, info = self._lookup(ident)
        if info is None:
            return dict(id=ident, pdf=None, display=None, role=None, reason='unknown ID')
        conf = self._config()
        entry = info['entry']
        library = entry.get('library') if isinstance(entry.get('library'), dict) else {}
        copies = [c for c in library.get('copies', []) or [] if isinstance(c, dict)]
        primary = [c for c in copies if c.get('role') in {'full-text', 'primary', 'primary-candidate', 'full text'}]
        other = [c for c in copies if c not in primary]
        candidates = [(c, c.get('role')) for c in primary + other]
        candidates += [(e, 'evidence') for e in entry.get('evidence', []) or [] if isinstance(e, dict)]
        reasons = []
        for item, role in candidates:
            if not str(item.get('path', '')).lower().endswith('.pdf'):
                continue
            found, reason = self._pdf_ok(conf, item)
            if found:
                return dict(id=entry['id'], pdf=found[1], display=found[0], role=role or 'copy', reason=None)
            reasons.append(reason)
        if reasons:
            return dict(id=entry['id'], pdf=None, display=None, role=None, reason='; '.join(reasons))
        note = library.get('availability_note')
        return dict(id=entry['id'], pdf=None, display=None, role=None,
                    reason='no retained PDF copy or PDF evidence' + (f' ({_clip(note, 200)})' if note else ''))

    def library_pdf(self, ident: str):
        return self.library_info(ident).get('pdf')

    # ------------------------------------------------------------------ documents

    @staticmethod
    def _kind_of(rel: str) -> str:
        parts = rel.split('/')
        name = parts[-1].upper()
        if len(parts) == 1:
            return 'hub'
        if parts[0] == 'harness':
            return 'harness'
        if parts[0] == 'templates':
            return 'template'
        if 'indexes' in parts[:-1]:
            return 'index'
        if 'investigations' in parts[:-1]:
            if name.startswith('NOTE'):
                return 'note'
            if name.startswith('GOAL'):
                return 'goal'
            if name.startswith('PROOF'):
                return 'proof'
            return 'investigation'
        if parts[0] == 'projects' and len(parts) == 3:
            return 'project'
        return 'document'

    @staticmethod
    def _title_of(real: str, ext: str):
        try:
            with open(real, 'rb') as stream:
                head = stream.read(8192)
        except OSError:
            return None
        text = head.decode('utf-8', errors='replace').lstrip('\ufeff')
        if ext == '.tex':
            match = re.search(r'\\title(?:\[[^\]]*\])?\{((?:[^{}]|\{[^{}]*\})*)\}', text)
            return _clip(match.group(1), 200) if match else None
        lines = text.splitlines()
        if lines and lines[0].strip() == '---':
            for i in range(1, min(len(lines), 60)):
                line = lines[i]
                if line.strip() in ('---', '...'):
                    lines = lines[i + 1:]
                    break
                match = re.match(r'title:\s*["\']?(.+?)["\']?\s*$', line)
                if match:
                    return _clip(match.group(1), 200)
        fence = None
        for line in lines:
            stripped = line.strip()
            marker = re.match(r'(`{3,}|~{3,})', stripped)
            if marker:
                if fence is None:
                    fence = marker.group(1)[0]
                elif stripped.startswith(fence * 3):
                    fence = None
                continue
            if fence:
                continue
            match = re.match(r' {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', line)
            if match and match.group(1).strip('# '):
                return _clip(match.group(1).strip(), 200)
        return None

    def _walk_docs(self, conf: _Config):
        """[(real path, rel, owner)] for every listed document."""
        out, seen = [], set()

        def add(real, owner):
            if real in seen:
                return
            name = os.path.basename(real)
            if os.path.splitext(name)[1].lower() not in LISTED_EXTENSIONS or RAW_NAME.search(name):
                return
            rel = os.path.relpath(real, self.root).replace(os.sep, '/')
            if rel.startswith('../') or rel.startswith('.hub/') or any(p.startswith('.') for p in rel.split('/')):
                return
            seen.add(real)
            out.append((real, rel, owner))

        def top_level(directory, owner):
            try:
                with os.scandir(directory) as it:
                    for entry in it:
                        if entry.is_file(follow_symlinks=False):
                            add(entry.path, owner)
            except OSError:
                pass

        def walk(directory, owner):
            stack = [directory]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        entries = list(it)
                except OSError:
                    continue
                for entry in entries:
                    if entry.name.startswith('.'):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in SKIP_DIRS:
                            stack.append(entry.path)
                        elif entry.name == 'indexes':
                            top_level(entry.path, owner)
                    elif entry.is_file(follow_symlinks=False):
                        add(entry.path, owner)

        top_level(self.root, None)
        top_level(os.path.join(self.root, 'indexes'), None)
        top_level(os.path.join(self.root, 'harness'), None)
        top_level(os.path.join(self.root, 'templates'), None)
        routes = [(r, o) for r, o in conf.routes if r]
        routes += [(f'projects/{p}', p) for p in conf.projects if (f'projects/{p}', p) not in routes]
        # Longest first, so that a nested route claims its files for its own owner.
        for route, owner in sorted(routes, key=lambda item: -len(item[0])):
            path = os.path.join(self.root, route)
            if os.path.islink(path) or not _inside(os.path.realpath(path), self.root):
                continue
            if os.path.isfile(path):
                add(path, owner)
            elif os.path.isdir(path):
                walk(path, owner)
        return out

    def _build_docs(self):
        conf = self._config()
        docs = []
        for real, rel, owner in self._walk_docs(conf):
            try:
                st = os.stat(real)
            except OSError:
                continue
            ext = os.path.splitext(real)[1].lower()
            key = (st.st_mtime_ns, st.st_size)
            cached = self._titles.get(real)
            if cached and cached[0] == key:
                title = cached[1]
            else:
                title = self._title_of(real, ext)
                self._titles[real] = (key, title)
            project = self._project_of_rel(conf, rel) if owner is None else owner
            if rel.split('/')[0] in ('harness', 'templates') or len(rel.split('/')) == 1:
                project = None
            docs.append(dict(path=rel, title=title or os.path.splitext(os.path.basename(rel))[0],
                             kind=self._kind_of(rel), project=project, mtime=st.st_mtime, size=st.st_size))
        docs.sort(key=lambda d: (d['project'] or '', KIND_ORDER.get(d['kind'], 99), d['path']))
        return docs

    def _refresh_docs_background(self):
        try:
            docs = self._build_docs()
            with self._docs_lock:
                self._docs, self._docs_at = docs, time.monotonic()
        finally:
            with self._docs_lock:
                self._docs_refreshing = False

    def _all_docs(self) -> list:
        with self._docs_lock:
            docs, age = self._docs, time.monotonic() - self._docs_at
            stale = docs is not None and age >= DOCUMENT_TTL_SECONDS and not self._docs_refreshing
            if stale:
                self._docs_refreshing = True
        if docs is None:
            with self._docs_lock:
                if self._docs is None:
                    self._docs, self._docs_at = self._build_docs(), time.monotonic()
                return self._docs
        if stale:   # stale-while-revalidate: answer now, rebuild in the background
            threading.Thread(target=self._refresh_docs_background, name='mathdesk-docs', daemon=True).start()
        return docs

    def documents(self, project=None) -> list:
        docs = self._all_docs()
        if project in (None, '', 'all'):
            selected = docs
        elif project in ('hub', 'infrastructure'):
            selected = [d for d in docs if d['project'] is None]
        else:
            selected = [d for d in docs if d['project'] == project]
        return [dict(d) for d in selected]

    # ------------------------------------------------------------------ search

    def _normalizers(self):
        with self._norm_lock:
            if self._norm is None:
                previous = sys.dont_write_bytecode
                try:
                    # The hub's own query normalizer, so FTS tokens match the index.
                    # No .pyc may be written into the hub's tools directory.
                    sys.dont_write_bytecode = True
                    tools = os.path.join(self.root, 'tools')
                    if tools not in sys.path:
                        sys.path.append(tools)
                    from recall import index as recall_index
                    self._norm = (lambda s: recall_index.normalize(s, expanded=False), recall_index.alias_text)
                except Exception:
                    simple = lambda s: ' '.join(re.findall(r'\w+', str(s).casefold()))
                    self._norm = (simple, simple)
                finally:
                    sys.dont_write_bytecode = previous
            return self._norm

    def _fts_path(self):
        current = os.path.join(self._internal, 'recall', 'CURRENT.json')
        try:
            with open(current, 'rb') as stream:
                generation = json.loads(stream.read()).get('generation', '')
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(generation, str) or not re.fullmatch(r'[0-9a-f]{32}', generation):
            return None
        path = os.path.join(self._internal, 'recall', 'generations', generation, 'index.sqlite')
        return path if os.path.isfile(path) else None

    def _fts_hits(self, conf, query, project, limit, terms):
        with self._fts_lease() as path:
            return self._fts_hits_at(path, conf, query, project, limit, terms)

    @contextmanager
    def _fts_lease(self):
        directory = os.path.join(self._internal, 'recall')
        lock = os.path.join(directory, 'readers.lock')
        if not os.path.isfile(lock):
            # Legacy indexes have only the refresh lock. Both refresh and GC
            # own it, so it protects readers during a storage upgrade too.
            lock = os.path.join(directory, 'refresh.lock')
        try:
            stream = open(lock, 'rb')
        except OSError:
            yield None
            return
        with stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            try:
                yield self._fts_path()
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _fts_hits_at(self, path, conf, query, project, limit, terms):
        if not path:
            return []
        plain, alias = self._normalizers()
        words = [w for w in dict.fromkeys(plain(query).split()) if w not in STOP] or plain(query).split()
        if not words:
            return []
        quote = lambda w: '"' + w.replace('"', '""') + '"'
        expressions = [' '.join(quote(w) for w in words)]
        extra = [w for w in dict.fromkeys(alias(query).split()) if w not in STOP]
        if len(extra) > 1:
            expressions.append(' OR '.join(quote(w) for w in extra))
        deadline = time.monotonic() + SEARCH_DEADLINE_SECONDS
        try:
            connection = sqlite3.connect(Path(path).as_uri() + '?mode=ro&immutable=1', uri=True)
        except sqlite3.Error:
            return []
        hits = []
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
            fetch = 600 if project else 300
            for expression in expressions:
                ranked = connection.execute(
                    'SELECT id, bm25(search, 0, 6, 1, 0.25, 8) AS rank FROM search WHERE search MATCH ? '
                    'ORDER BY rank LIMIT ?', (expression, fetch)).fetchall()
                if not ranked:
                    continue
                order = {row['id']: i for i, row in enumerate(ranked)}
                rows = []
                ids = list(order)
                for start in range(0, len(ids), 500):
                    batch = ids[start:start + 500]
                    rows += connection.execute(
                        'SELECT id, owner, tier, root, path, locator, title, text FROM chunks WHERE id IN ('
                        + ','.join('?' * len(batch)) + ") AND tier IN ('documents', 'archives')", batch).fetchall()
                rows.sort(key=lambda row: order[row['id']])
                seen = set()
                for row in rows:
                    if project and row['owner'] != project:
                        continue
                    located = self._locate(conf, row['root'], row['path'])
                    if located is None or os.path.splitext(located[1])[1].lower() not in DOC_EXTENSIONS:
                        continue
                    display = located[0]
                    if display in seen or not os.path.isfile(located[1]):
                        continue
                    seen.add(display)
                    hits.append(dict(type='doc', path=display, title=row['title'] or os.path.basename(display),
                                     snippet=_snippet(row['text'], terms), project=row['owner'],
                                     locator=row['locator'], kind=self._kind_of(display) if ':' not in display else 'archive',
                                     match='fulltext', score=100.0 - order[row['id']] * 0.05))
                    if len(hits) >= limit:
                        break
                if hits:
                    break
        except sqlite3.Error:
            pass       # interrupted at the deadline or index unreadable: keep what we have
        finally:
            connection.close()
        return hits

    def search(self, q: str, project=None, limit: int = 20) -> list:
        if not isinstance(q, str) or not q.strip():
            return []
        q = ' '.join(q.split())[:300]
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20
        project = project or None
        snap = self._snapshot()
        key = (q, project, limit, snap.token, self._fts_path())
        with self._search_lock:
            if key in self._search_cache:
                self._search_cache.move_to_end(key)
                return copy.deepcopy(self._search_cache[key])
        results = self._search(snap, q, project, limit)
        with self._search_lock:
            self._search_cache[key] = results
            while len(self._search_cache) > SEARCH_CACHE_SIZE:
                self._search_cache.popitem(last=False)
        return copy.deepcopy(results)

    def _search(self, snap, q, project, limit):
        conf = self._config()
        folded = q.casefold()
        words = re.findall(r'\w+', folded)
        terms = [w for w in words if w not in STOP] or words
        ident = q.strip().upper()
        scored = []
        id_query = bool(ID_LIKE.fullmatch(ident))
        for key, info in snap.entries.items():
            if project and info['project'] != project:
                continue
            score = 0.0
            match = None
            if key == ident:
                score, match = 1000.0, 'id'
            elif id_query and len(ident) >= 2 and key.startswith(ident):
                score, match = 600.0 - len(key), 'id-prefix'
            elif terms:
                title_hits = sum(t in info['hay_title'] for t in terms)
                if title_hits == len(terms):
                    score, match = 300.0 + (40 if folded in info['hay_title'] else 0), 'title'
                elif all(t in info['hay_title'] or t in info['hay_body'] for t in terms):
                    score, match = 150.0 + 10 * title_hits + (20 if folded in info['hay_body'] else 0), 'text'
            if not score:
                continue
            if info['category'] == 'record' and info['kind'] == 'correction':
                score += 1
            scored.append((score, key, info, match))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = []
        for score, key, info, match in scored[:limit]:
            results.append(dict(type='record', id=key, title=info['title'], project=info['project'],
                                snippet=_snippet(info['snippet_source'], terms), kind=info['kind'],
                                category=info['category'], status=info['status'], match=match, score=score))
        docs = []
        for doc in self._all_docs():
            if project and doc['project'] != project:
                continue
            hay = _casefold(f"{doc['title']} {doc['path']}")
            if terms and all(t in hay for t in terms):
                bonus = (40 if folded in _casefold(doc['title']) else 0) + (10 if doc['kind'] in ('note', 'project', 'hub') else 0)
                docs.append((doc['mtime'], dict(type='doc', path=doc['path'], title=doc['title'],
                                                 project=doc['project'], snippet=doc['path'], kind=doc['kind'],
                                                 match='title', score=250.0 + bonus)))
        docs = [d for _, d in sorted(docs, key=lambda item: (-item[1]['score'], -item[0]))][:limit]
        results += docs
        if terms:
            have = {d['path'] for d in docs}
            results += [h for h in self._fts_hits(conf, q, project, limit, terms) if h['path'] not in have]
        for hit in results:
            if hit['type'] == 'doc':
                match = LIBRARY_PATH.match(hit['path'])
                source = snap.entries.get(match.group(1)) if match else None
                if source is not None:
                    hit.update(id=source['entry']['id'],
                               title=f"{source['entry']['id']} library text — {_clip(source['title'], 120)}")
        results.sort(key=lambda item: -item['score'])
        top = results[:limit]
        # Keep a quarter of the slots for documents when records would fill them all.
        reserve = max(1, limit // 4)
        shown = [r for r in top if r['type'] == 'doc']
        if len(shown) < reserve:
            extra = [r for r in results[limit:] if r['type'] == 'doc'][:reserve - len(shown)]
            if extra:
                records = [r for r in top if r['type'] == 'record'][:limit - len(shown) - len(extra)]
                top = sorted(records + shown + extra, key=lambda item: -item['score'])
        return top
