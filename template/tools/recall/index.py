"""Resumable parse/embedding caches and atomic SQLite FTS5 generations."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import unicodedata
import uuid

from . import corpus, providers, cache_storage

SCHEMA_VERSION = 1
NORMALIZER_VERSION = 'math-v2'
ALIASES = (
    ('castelnuovo mumford', 'castelnuovo-mumford', 'cm regularity'),
    ('borel fixed', 'borel-fixed', 'strongly stable'),
    ('projective space', 'projectivespace'),
    ('first chern', 'chern 1', 'c1', 'c_1'),
    ('second chern', 'chern 2', 'c2', 'c_2'),
    ('symmetric power', 'symmetric powers', 'sym'),
    ('exterior power', 'exterior powers', 'wedge'),
)
GREEK = {'α': 'alpha', 'β': 'beta', 'γ': 'gamma', 'δ': 'delta', 'ε': 'epsilon',
         'λ': 'lambda', 'μ': 'mu', 'ν': 'nu', 'π': 'pi', 'ρ': 'rho', 'σ': 'sigma',
         'τ': 'tau', 'φ': 'phi', 'ψ': 'psi', 'ω': 'omega', 'χ': 'chi'}


def normalize(text, *, expanded=True):
    """Ranking-only normal form; mathematical source text is never rewritten."""
    text = str(text)
    text = re.sub(r'[⁰¹²³⁴⁵⁶⁷⁸⁹]+', lambda m: '^' + unicodedata.normalize('NFKC', m[0]), text)
    text = re.sub(r'[₀₁₂₃₄₅₆₇₈₉]+', lambda m: '_' + unicodedata.normalize('NFKC', m[0]), text)
    text = unicodedata.normalize('NFKC', text).casefold()
    for char, name in GREEK.items():
        text = text.replace(char, ' ' + name + ' ')
    text = re.sub(r'\\(?:mathbb|mathbf|mathrm|mathcal|operatorname)\s*\{([^{}]+)\}', r'\1', text)
    text = re.sub(r'\\([a-z]+)', r' \1 ', text)
    text = re.sub(r'\{\s*([a-z0-9]+)\s*\}', r'\1', text)
    # Retain exponent/subscript tokens as well as natural token forms.
    compact = re.findall(r'([a-z]+)\s*([_^])\s*([0-9]+)', text)
    twists = re.findall(r'\b([a-z]+)\s*\(\s*([+-]?)\s*([0-9]+)\s*\)', text)
    text = re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).replace('_', ' ')
    text = ' '.join(text.split())
    extras = [f'{base}{"pow" if op == "^" else "sub"}{n} {base}{n}' for base, op, n in compact]
    extras += [f'{base}twist{"neg" if sign == "-" else "pos"}{number}' for base,sign,number in twists]
    return ' '.join([text] + extras).strip() if expanded else text


def alias_text(text):
    normalized = normalize(text)
    extras = []
    for group in ALIASES:
        if any(re.search(r'(?<!\w)' + re.escape(normalize(term)) + r'(?!\w)', normalized) for term in group):
            extras.extend(normalize(term) for term in group)
    return normalized + (' ' + ' '.join(extras) if extras else '')


def semantic_body(chunk):
    """Rank verbatim mathematical fields before bulky evidence/provenance JSON.

    Full original objects remain in SQLite. This deterministic projection adds
    no interpretation and gives the finite embedding context useful statements.
    """
    original = chunk.get('metadata', {}).get('original')
    if chunk.get('tier') not in {'canonical','knowledge'} or not isinstance(original,dict):
        return chunk['text']
    def plain(value):
        if isinstance(value,str):
            return value
        if isinstance(value,list):
            return '\n'.join(plain(item) for item in value)
        if isinstance(value,dict):
            if isinstance(value.get('text'),str):
                return value['text']
            return '\n'.join(key+': '+plain(item) for key,item in value.items()
                              if key not in {'provenance','assessment','sha256','inspected_input_fingerprint'})
        return str(value)
    fields = ('statement','hypotheses','limitations','reopening_condition','checked_statement',
              'checked_scope','payload','citation','use','locator','title','author','authors','year','library')
    return '\n'.join(field+': '+plain(original[field]) for field in fields if field in original) or chunk['text']


def state_dir(hub):
    return Path(hub) / '.hub/recall'


def _write_json(path, value, budget=None):
    if budget is not None:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        if path.is_relative_to(budget.root / "cache"):
            return budget.write_cache(path, raw)
        return budget.write_generation(path, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def current_generation(hub):
    current = state_dir(hub) / 'CURRENT.json'
    if not current.exists():
        return None
    data = json.loads(current.read_text())
    generation = data.get('generation', '')
    if not re.fullmatch(r'[0-9a-f]{32}', generation):
        raise ValueError('Invalid recall generation pointer')
    path = state_dir(hub) / 'generations' / generation
    if not (path / 'index.sqlite').is_file() or not (path / 'status.json').is_file():
        raise ValueError('Recall generation incomplete; previous generation files are retained')
    return path


def status(hub, generation=None, *, pinned=False, storage=True):
    if not pinned:
        with cache_storage.reader(hub) as selected:
            return status(hub, selected, pinned=True, storage=storage)
    if generation is None:
        return dict(ready=False, coverage={'indexed_chunks': 0}, semantic={'state': 'unconfigured'},
                    warnings=['Recall has not been refreshed.' if not (state_dir(hub) / 'CURRENT.json').exists()
                              else 'Recall has no available leased generation; live canonical retrieval remains available.'])
    result = json.loads((generation / 'status.json').read_text())
    if storage:
        result['storage_usage'] = dict(generation_bytes=cache_storage.size(state_dir(hub) / 'generations'),
                                      cache_bytes=cache_storage.size(state_dir(hub) / 'cache'))
    return result


@contextmanager
def _refresh_lock(hub):
    directory = state_dir(hub)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'refresh.lock').open('a+') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Recall refresh busy; retry after the active refresh') from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _connection(path):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=OFF')
    connection.executescript('''
      CREATE TABLE chunks (id TEXT PRIMARY KEY, owner TEXT, tier TEXT, source_id TEXT,
        root TEXT, path TEXT, locator TEXT, title TEXT, text TEXT, sha256 TEXT,
        metadata TEXT, vector BLOB);
      CREATE INDEX chunk_owner ON chunks(owner);
      CREATE INDEX chunk_source ON chunks(source_id);
      CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED, title, text, aliases, identity,
        tokenize='unicode61 remove_diacritics 2');
    ''')
    return connection


def _parse_cached(hub, manifest, descriptor, budget=None):
    key = corpus.digest([corpus.PARSER_VERSION, descriptor])
    path = state_dir(hub) / 'cache/parsed' / (key + '.json')
    if path.exists():
        chunks = json.loads(path.read_text())
        # A frozen descriptor remains authoritative; detect changed files on resume.
        actual = corpus.resolve(hub, manifest['configuration'], descriptor['root'], descriptor['path'])
        if corpus.digest(actual.read_bytes()) != descriptor['sha256']:
            raise ValueError(f'File changed after manifest freeze: {descriptor["path"]}')
        if budget is not None:
            budget.touch(path)
        return chunks, True
    chunks = corpus.parse_file(hub, manifest, descriptor)
    _write_json(path, chunks, budget)
    return chunks, False


def _refresh(hub, project=None, manifest=None, semantic=False, budget=None):
    """Build a complete eligible generation; failure leaves the previous pointer.

    Project is a reporting scope only: shared generation refresh never drops other
    projects. A supplied manifest freezes selection; absent one reuses selection
    from the last successful generation and discovers current registered files.
    """
    hub = Path(hub).resolve()
    with budget.build_scope():
        previous = current_generation(hub)
        if manifest is None:
            selection = None
            if previous:
                selection = json.loads((previous / 'manifest.json').read_text()).get('selection')
            manifest = corpus.freeze_manifest(hub, selection)
        if project is not None and project not in manifest['configuration']['projects']:
            raise ValueError(f'Unknown project {project}')
        if corpus.digest({k: v for k, v in manifest.items() if k not in {'created_at', 'id'}}) != manifest.get('id'):
            raise ValueError('Frozen corpus manifest hash mismatch')
        if manifest.get('parser_version') != corpus.PARSER_VERSION:
            raise ValueError('Manifest parser version is not supported; freeze a new manifest')
        generation_id = uuid.uuid4().hex
        directory = state_dir(hub) / 'generations' / generation_id
        directory.mkdir(parents=True)
        budget.directory = directory
        gaps = []
        chunks, parsed_hits, parsed_files = list(manifest['objects']), 0, 0
        for descriptor in manifest['files']:
            try:
                parsed, cached = _parse_cached(hub, manifest, descriptor, budget)
                chunks.extend(parsed)
                parsed_hits += int(cached)
                parsed_files += 1
            except (OSError, ValueError) as exc:
                gaps.append(dict(root=descriptor['root'], path=descriptor['path'],
                                 tier=descriptor['tier'], reason=str(exc)))
        semantic_state = dict(state='disabled', indexed_vectors=0, eligible_chunks=len(chunks))
        vectors, vector_hits = {}, 0
        if semantic:
            semantic_state['state'] = 'unavailable'
            try:
                provider = providers.configured(hub)
                if provider is None:
                    raise providers.ProviderUnavailable('No local embedding provider configured')
                pin = provider.identity(timeout=3)
                # Group before requesting anything: repeated chunks in this very
                # refresh share one call and one cache entry, including on resume.
                grouped, pending = {}, []
                cache_root = state_dir(hub) / 'cache/embeddings'
                for chunk in chunks:
                    text = (chunk['title'] + '\n' + semantic_body(chunk))[:providers.MAX_INPUT_CHARS]
                    key = corpus.digest([pin, text])
                    group = grouped.setdefault(key, dict(text=text, ids=[], path=cache_root / (key + '.json')))
                    group['ids'].append(chunk['id'])
                for group in grouped.values():
                    if group['path'].exists():
                        vector = json.loads(group['path'].read_text())
                        budget.touch(group['path'])
                        vectors.update((key, vector) for key in group['ids'])
                        vector_hits += len(group['ids'])
                    else:
                        pending.append(group)
                semantic_state.update(unique_inputs=len(grouped), unique_cached_inputs=len(grouped) - len(pending),
                                      deduplicated_inputs=len(chunks) - len(grouped),
                                      projection='verbatim statement/hypotheses/limits or aid clause text; document chunks unchanged')
                for start in range(0, len(pending), 16):
                    batch = pending[start:start + 16]
                    output = provider.embed([group['text'] for group in batch])
                    for group, vector in zip(batch, output):
                        vectors.update((key, vector) for key in group['ids'])
                        _write_json(group['path'], vector, budget)
                dimensions = {len(v) for v in vectors.values()}
                if len(dimensions) > 1:
                    raise providers.ProviderUnavailable('Embedding dimensions changed; cached vectors not mixed')
                pin['dimensions'] = next(iter(dimensions), 0)
                # Prevent a model replacement during refresh from certifying a mixed generation.
                final_pin = provider.identity(timeout=3)
                if final_pin['digest'] != pin['digest']:
                    raise providers.ProviderUnavailable('Model artifact changed during refresh')
                semantic_state.update(state='ready', indexed_vectors=len(vectors), pin=pin,
                    cache_hits=vector_hits, vector_storage_bytes=sum(len(v) * 4 for v in vectors.values()), truncated_inputs=sum(len(c['title'] + '\n' + semantic_body(c)) > providers.MAX_INPUT_CHARS for c in chunks))
            except (OSError, ValueError) as exc:
                # Cached successes survive; this generation publishes lexical fallback only.
                vectors = {}
                semantic_state.update(state='unavailable', reason=str(exc), cache_hits=vector_hits)
        if cache_storage.size(state_dir(hub) / 'generations') + 65536 > budget.policy['generation_budget_bytes']:
            raise ValueError('Recall generation budget exhausted; current index retained')
        connection = _connection(directory / 'index.sqlite')
        try:
            budget.cap_sqlite(connection, directory, len(json.dumps(manifest, sort_keys=True).encode()) + 131072)
            for chunk in chunks:
                vector = vectors.get(chunk['id'])
                blob = struct.pack('<' + 'f' * len(vector), *vector) if vector else None
                connection.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    tuple(chunk[k] for k in ('id', 'owner', 'tier', 'source_id', 'root', 'path', 'locator', 'title', 'text', 'sha256')) +
                    (json.dumps(chunk['metadata'], ensure_ascii=False), blob))
                original = chunk['metadata'].get('original', {})
                aliases = original.get('library', {}).get('aliases', [])
                connection.execute('INSERT INTO search VALUES (?,?,?,?,?)',
                    (chunk['id'], normalize(chunk['title']), normalize(chunk['text']),
                     alias_text(chunk['title'] + ' ' + chunk['text'] + ' ' + ' '.join(aliases)),
                     normalize(chunk['source_id'] + ' ' + chunk['path'])))
            connection.commit()
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Recall SQLite integrity check failed')
        finally:
            connection.close()
        coverage = dict(eligible=manifest['eligible'], indexed_chunks=len(chunks),
            chunks_by_tier=dict(Counter(c['tier'] for c in chunks)), indexed_files=parsed_files,
            eligible_files=len(manifest['files']), parser_gaps=gaps,
            acquisition_gaps=manifest.get('gaps', []),
            deferred=manifest.get('deferred', []), exclusions=manifest.get('exclusions', {}),
            selection=manifest.get('selection', {}), limits=manifest.get('limits', []))
        result = dict(ready=True, generation=generation_id, manifest_id=manifest['id'],
            schema_version=SCHEMA_VERSION, normalizer_version=NORMALIZER_VERSION,
            created_at=datetime.now(timezone.utc).isoformat(), project=project,
            coverage=coverage, semantic=semantic_state, catalog_hashes=manifest['catalog_hashes'],
            cache={'parsed_files_reused': parsed_hits, 'embedding_chunks_reused': vector_hits},
            storage={'sqlite_bytes': (directory / 'index.sqlite').stat().st_size,
                     'exact_text_bytes': sum(len(c['text'].encode('utf-8')) for c in chunks),
                     'unique_texts': len({corpus.digest([c['title'], c['text']]) for c in chunks})},
            warnings=['Some eligible files have explicit parser/acquisition gaps.']
                     if gaps or manifest.get('gaps') else [])
        _write_json(directory / 'manifest.json', manifest, budget)
        _write_json(directory / 'status.json', result, budget)
        with (directory / 'index.sqlite').open('rb') as database:
            os.fsync(database.fileno())
        _write_json(state_dir(hub) / 'CURRENT.json', {'generation': generation_id, 'previous_generation': previous.name if previous else None})
        return result


def refresh(hub, project=None, manifest=None, semantic=False):
    """Refresh within byte budgets; failed builds leave the selected index intact."""
    with _refresh_lock(Path(hub)):
        budget = cache_storage.Budget(hub)
        try:
            result = _refresh(hub, project, manifest, semantic, budget)
        except sqlite3.OperationalError as exc:
            raise ValueError('Recall build failed (possibly generation budget exhausted); current index retained: ' + str(exc)) from exc
        result['collection'] = budget.collection
        return result


def collect(hub):
    """Explicit maintenance, also performed on every refresh; canonical data excluded."""
    with _refresh_lock(Path(hub)):
        budget = cache_storage.Budget(hub)
        return dict(**budget.collection, generation_bytes=cache_storage.size(budget.root / 'generations'),
                    cache_bytes=budget.cache_bytes, limits=budget.policy)
