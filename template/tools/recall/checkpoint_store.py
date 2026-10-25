"""Immutable canonical frontier bodies, compacted without changing their values."""
import hashlib
import json
from pathlib import Path
import re

import storage_codec as codec
import transactions as tx

PURPOSE = 'frontier-input-manifest'
FIELDS = {'items', 'input_manifest'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def compact(value):
    """Intern containers by full content, including unresolved diagnostics.

    Child references make the cost proportional to distinct structures. List
    ordering and duplicate occurrences are retained. Semantic freshness hashes
    are ordinary data and are never used as storage identities.
    """
    nodes = {}
    def visit(item):
        if isinstance(item, dict):
            # Retain iteration order too: frontier uses prior-item order when
            # assembling equally ranked retained matches.
            node = ['dict', [[key, visit(child)] for key, child in item.items()]]
        elif isinstance(item, list):
            node = ['list', [visit(child) for child in item]]
        else:
            return ['value', item]
        key = codec.digest(canonical(node))
        nodes.setdefault(key, node)
        return ['ref', key]
    root = visit(value)
    return dict(schema_version=1, root=root, nodes=nodes)


def expand(packed):
    if not isinstance(packed, dict) or set(packed) != {'schema_version', 'root', 'nodes'} or packed['schema_version'] != 1:
        raise ValueError('Invalid checkpoint body encoding')
    nodes, memo, visiting = packed['nodes'], {}, set()
    if not isinstance(nodes, dict):
        raise ValueError('Invalid checkpoint node table')
    def visit(ref):
        if not isinstance(ref, list) or len(ref) != 2:
            raise ValueError('Invalid checkpoint node reference')
        tag, value = ref
        if tag == 'value' and not isinstance(value, (dict, list)):
            return value
        if tag != 'ref' or not isinstance(value, str) or value not in nodes:
            raise ValueError('Missing checkpoint node')
        if value in visiting:
            raise ValueError('Cyclic checkpoint body')
        if value in memo:
            return memo[value]
        node = nodes[value]
        if codec.digest(canonical(node)) != value or not isinstance(node, list) or len(node) != 2:
            raise ValueError('Checkpoint node digest/shape mismatch')
        visiting.add(value)
        if node[0] == 'dict':
            pairs = node[1]
            if (not isinstance(pairs, list) or any(not isinstance(p, list) or len(p) != 2 or not isinstance(p[0], str) for p in pairs)
                    or len({p[0] for p in pairs}) != len(pairs)):
                raise ValueError('Invalid checkpoint dictionary')
            result = {key: visit(child) for key, child in pairs}
        elif node[0] == 'list' and isinstance(node[1], list):
            result = [visit(child) for child in node[1]]
        else:
            raise ValueError('Invalid checkpoint node kind')
        visiting.remove(value)
        memo[value] = result
        return result
    return visit(packed['root'])


def prefix(owner):
    if not isinstance(owner, str) or not re.fullmatch('[a-z0-9][a-z0-9-]*', owner):
        raise ValueError('Invalid checkpoint owner')
    return ('catalog/' if owner == 'hub' else f'projects/{owner}/catalog/') + 'frontier/'


def is_body_path(path):
    return bool(re.fullmatch(r'(?:projects/[a-z0-9][a-z0-9-]*/)?catalog/frontier/[a-f0-9]{64}\.(?:zst|xz)', path))


def descriptor(entry, owner):
    body = entry['body']
    if entry.get('purpose') != PURPOSE or FIELDS & entry.keys():
        raise ValueError('Checkpoint must contain either inline body or body reference')
    if not isinstance(body, dict) or set(body) != {'schema_version', 'path', 'payload', 'expanded_sha256'} or body['schema_version'] != 1:
        raise ValueError('Invalid checkpoint body descriptor')
    codec.validate_reference(body['payload'])
    if body['path'] != prefix(owner) + codec.filename(body['payload']):
        raise ValueError('Checkpoint body path does not match its owner/digest')
    if not isinstance(body['expanded_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', body['expanded_sha256']):
        raise ValueError('Invalid expanded checkpoint digest')
    return body


def body_bytes(hub, entry, owner, overrides=None):
    body = descriptor(entry, owner)
    path = body['path']
    raw = (overrides or {}).get(path) if path in (overrides or {}) else tx.read_bytes(tx.safe_path(hub, path))
    if isinstance(raw, str):
        raw = raw.encode()
    if raw is None:
        raise ValueError(f'Missing canonical checkpoint body: {path}')
    ref = body['payload']
    if len(raw) != ref['stored_bytes'] or codec.digest(raw) != ref['stored_sha256']:
        raise ValueError(f'Canonical checkpoint body checksum/length mismatch: {path}')
    return raw


def hydrate(hub, owner, entry, overrides=None):
    if 'body' not in entry:
        return entry
    body = descriptor(entry, owner)
    value = expand(json.loads(codec.unpack(body_bytes(hub, entry, owner, overrides), body['payload'])))
    if not isinstance(value, dict) or set(value) != FIELDS or not all(isinstance(value[k], dict) for k in FIELDS):
        raise ValueError('Invalid expanded checkpoint body')
    if codec.digest(canonical(value)) != body['expanded_sha256']:
        raise ValueError('Expanded checkpoint checksum mismatch')
    return dict({k: v for k, v in entry.items() if k != 'body'}, **value)


def prepare(hub, owner, entry):
    """Return compact metadata plus immutable file additions for a normal batch."""
    if entry.get('purpose') != PURPOSE or 'body' in entry:
        return entry, {}
    if not FIELDS <= entry.keys():
        raise ValueError('Frontier checkpoint is missing its body')
    value = {key: entry[key] for key in ('items', 'input_manifest')}
    ref, raw = codec.pack(canonical(compact(value)))
    relative = prefix(owner) + codec.filename(ref)
    previous = tx.read_bytes(tx.safe_path(hub, relative))
    if previous is not None:
        # Compressor versions may encode identical content differently.
        ref = dict(ref, stored_bytes=len(previous), stored_sha256=codec.digest(previous))
        codec.unpack(previous, ref)
        raw = previous
    result = {key: value for key, value in entry.items() if key not in FIELDS}
    result['body'] = dict(schema_version=1, path=relative, payload=ref,
                          expanded_sha256=codec.digest(canonical(value)))
    overrides = {relative: raw}
    if hydrate(hub, owner, result, overrides) != entry:
        raise ValueError('Checkpoint conversion was not lossless')
    return result, {relative: (raw, tx.digest(previous))}


def validate_catalog(catalog, hub, overrides=None, full=False, owners=None):
    errors, seen = [], set()
    for owner, envelope in catalog.knowledge.items():
        if owners is not None and owner not in owners:
            continue
        for entry in envelope.get('checkpoints', []):
            if not isinstance(entry, dict) or 'body' not in entry:
                continue
            try:
                ref = descriptor(entry, owner)
                # A reference's expanded digest must also be checked even when
                # another checkpoint points to the same compressed object.
                key = (ref['path'], ref['expanded_sha256'])
                if key not in seen:
                    if full:
                        hydrate(hub, owner, entry, overrides)
                    else:
                        body_bytes(hub, entry, owner, overrides)
                    seen.add(key)
                catalog.file_hashes[ref['path']] = ref['payload']['stored_sha256']
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                errors.append(f'{entry.get("id", owner)}: {exc}')
    return errors
