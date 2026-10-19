"""Retain exact computation inputs, source and outputs without running code/Git."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import catalog_store as cs
import transactions as tx


def verify_manifest(directory, manifest):
    errors = []
    if manifest.get('artifact_schema') != 1:
        return ['Unknown artifact manifest schema']
    for field in ['command', 'environment', 'domain', 'exhaustiveness', 'files']:
        if not manifest.get(field):
            errors.append(f'Missing artifact {field}')
    roles, seen = set(), set()
    for item in manifest.get('files', []):
        try:
            path = tx.safe_path(directory, item['path'])
            if item['path'] in seen:
                errors.append('Duplicate artifact path')
            seen.add(item['path'])
            roles.add(item['role'])
            if tx.digest(path.read_bytes()) != item['sha256']:
                errors.append(f'Artifact hash mismatch: {item["path"]}')
        except (OSError, KeyError, ValueError) as exc:
            errors.append(str(exc))
    if not {'source', 'input', 'output'} <= roles:
        errors.append('Retain source, input and output bytes (a source file may also be listed as an input)')
    return errors


def pack(hub, project, destination, specification):
    cat = cs.load_catalog(hub)
    if project not in cat.cfg['projects']:
        raise ValueError('Unknown project')
    prefix = f'projects/{project}/investigations/'
    if not destination.startswith(prefix):
        raise ValueError('Artifact destination must be inside this project investigation tree')
    tx.safe_path(hub, destination)
    manifest = {k: specification.get(k) for k in
                ['command', 'environment', 'domain', 'exhaustiveness', 'seed', 'code_revision', 'dirty_state']}
    for field in ['command', 'environment', 'domain', 'exhaustiveness']:
        if not manifest[field]:
            raise ValueError(f'Missing {field}')
    manifest.update(artifact_schema=1, created_at=datetime.now(timezone.utc).isoformat(), files=[])
    changes, expected = {}, dict(cat.file_hashes)
    roles, names = set(), set()
    for item in specification['files']:
        name, role = item['path'], item['role']
        tx.safe_path(Path(hub) / destination, name)
        if name in names or name == 'artifact-manifest.json' or role not in {'source', 'input', 'output'}:
            raise ValueError('Duplicate/reserved artifact path or invalid role')
        names.add(name); roles.add(role)
        source = Path(item['from']).expanduser().resolve()
        raw = source.read_bytes()
        rel = destination.rstrip('/') + '/' + name
        if (Path(hub) / rel).exists():
            raise ValueError(f'Immutable artifact path already exists: {rel}')
        changes[rel] = raw
        expected[rel] = None
        manifest['files'].append(dict(path=name, role=role, sha256=tx.digest(raw), bytes=len(raw)))
    if not {'source', 'input', 'output'} <= roles:
        raise ValueError('Exact source, input and output bytes are required')
    rel = destination.rstrip('/') + '/artifact-manifest.json'
    changes[rel] = cs.serialize(manifest)
    expected[rel] = None
    txid = tx.apply_batch(hub, changes, expected, scopes=[project], owner='artifact-pack')
    return dict(transaction=txid, manifest=rel, sha256=tx.digest(changes[rel].encode()),
                note='Byte retention only; computation and scientific validity were not certified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('specification', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(pack(Path(__file__).resolve().parents[1], args.project,
                              args.destination, json.loads(args.specification.read_bytes())), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'Error: {exc}\n')
