"""Library navigation and capture; source checks remain the canonical authority.

Physical copies and virtual reading collections are optional metadata on source
records. Reading membership is deliberately NOT a logical dependency relation.
"""
import argparse
import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
import urllib.request
from urllib.parse import urlparse

import catalog_store as cs
import catalog_validation as cv
import hub_operations as ops
import transactions as tx

KINDS = {'paper', 'textbook', 'reference', 'virtual'}
ROLES = {'full-text', 'excerpt', 'extracted-text', 'metadata', 'primary-candidate'}


def validate(source, by_id):
    if 'library' not in source:
        return []
    lib = source['library']
    errors = []
    def bad(message):
        errors.append(f'{source["id"]}: library {message}')
    if not isinstance(lib, dict):
        return [f'{source["id"]}: library must be an object']
    if not isinstance(lib.get('kind'), str) or lib['kind'] not in KINDS:
        bad('kind must be paper, textbook, reference or virtual')
    aliases = lib.get('aliases', [])
    if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
        bad('aliases must be nonempty strings')
    if not isinstance(lib.get('availability_note'), str) or not lib['availability_note'].strip():
        bad('needs an availability_note, including missing-copy limitations')
    copies = lib.get('copies', [])
    if not isinstance(copies, list):
        bad('copies must be a list')
        copies = []
    for item in copies:
        if not isinstance(item, dict):
            bad('copy must be an object'); continue
        if not isinstance(item.get('role'), str) or item['role'] not in ROLES:
            bad('invalid copy role')
        for field in ['version', 'url', 'accessed', 'root', 'path', 'locator', 'sha256']:
            if not isinstance(item.get(field), str) or not item[field].strip():
                bad(f'copy needs {field}')
        try:
            date.fromisoformat(item.get('accessed', ''))
        except (ValueError, TypeError):
            bad('copy accessed must be an ISO date')
        evidence = {k: item.get(k) for k in ['root', 'path', 'locator', 'sha256']}
        if not isinstance(source.get('evidence'), list) or evidence not in source['evidence']:
            bad('every copy must also be registered as hashed evidence')
        if 'source_version' in item:
            if item['source_version'] != source.get('version'):
                bad('copy source_version must match the exact registered version')
            if not isinstance(item.get('version_match_basis'), str) or not item['version_match_basis'].strip():
                bad('copy source_version needs a version_match_basis')
    members = lib.get('records', [])
    if not isinstance(members, list):
        bad('records must be a list'); members = []
    seen = set()
    for member in members:
        if not isinstance(member, dict):
            bad('member must be an object'); continue
        key = member.get('id')
        if not isinstance(key, str) or key not in by_id or 'kind' not in by_id[key]:
            bad('member must reference an existing mathematical record')
        elif key in seen:
            bad(f'duplicate member {key}')
        else:
            seen.add(key)
        if not isinstance(member.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', member['sha256']):
            bad('member needs its canonical record SHA-256')
    if lib.get('kind') == 'virtual' and not members:
        bad('virtual collection needs members')
    if lib.get('kind') != 'virtual' and members:
        bad('only virtual collections can have members')
    return errors


def member_state(member, by_id):
    record = by_id.get(member['id'])
    if record is None:
        return 'MISSING'
    if record.get('status') != 'verified':
        return 'REVIEW NEEDED: current status ' + record.get('status', 'unknown')
    if cs.sha256_text(cs.canonical(record)) != member['sha256']:
        return 'STALE selection: record changed; re-read before reuse'
    return 'unchanged verified record; read exact scope and corrections'


def availability(source):
    lib = source.get('library', {})
    if lib.get('kind') == 'virtual':
        return f"virtual reading collection ({len(lib.get('records', []))} records)"
    roles = {c['role'] for c in lib.get('copies', [])}
    if 'full-text' in roles:
        qualifier = 'exact source version retained' if not needs_retention_review(source) else 'exact source version still needs reconciliation'
        return f'full text registered; {qualifier} (freshness: show/check)'
    if roles:
        return 'only ' + ', '.join(sorted(roles)) + ' registered'
    pdfs = sum(e['path'].lower().endswith('.pdf') for e in source.get('evidence', []))
    if pdfs:
        return f'{pdfs} PDF evidence item(s); primary/full-text identity not classified'
    return 'primary copy not indexed; may exist in linked evidence or archives'


def select(sources, query=''):
    words = re.findall(r'\w+', query.casefold())
    def matches(s):
        # Word boundaries keep OSS from matching unrelated words such as loss.
        body = ' '.join([s['id'], s['citation'], s.get('use', ''),
                         ' '.join(s.get('topics', [])),
                         ' '.join(s.get('library', {}).get('aliases', []))])
        tokens = set(re.findall(r'\w+', body.casefold()))
        return all(w in tokens for w in words)
    return sorted((s for s in sources if matches(s)),
                  key=lambda s: (s.get('library', {}).get('kind') != 'textbook', s['citation'], s['id']))


def needs_retention_review(source):
    lib = source.get('library', {})
    return lib.get('kind') != 'virtual' and not any(
        c.get('role') == 'full-text' and
        (c.get('version') == source['version'] or c.get('source_version') == source['version'])
        for c in lib.get('copies', []))


def retention_issue(source):
    """Separate acquisition from identifying material already on disk."""
    if not needs_retention_review(source):
        return None
    lib = source.get('library', {})
    roles = {c['role'] for c in lib.get('copies', [])}
    if 'full-text' in roles:
        return 'Full text retained; reconcile registered version'
    if lib.get('kind') == 'reference':
        return 'Reference or internal source; inspect retained evidence'
    if roles or any(e['path'].lower().endswith('.pdf') for e in source.get('evidence', [])):
        return 'Partial text or candidate; inspect before acquiring'
    return 'No primary copy indexed; locate or acquire'


def render_gaps(sources, stamp):
    pending = [s for s in select(sources) if needs_retention_review(s)]
    lines = ['# Literature acquisition and identity gaps', '', stamp, '',
             f'{len(pending)} source entries lack an explicitly identified full text of their registered version. This is not a count of missing papers:',
             'some entries are reference pages, excerpts, separate checks of the same work, or files still needing identification.',
             'Inspect retained candidates before downloading. Exact checked versions and applicability scopes remain in show ID.', '']
    groups = {}
    for s in pending:
        groups.setdefault(retention_issue(s), []).append(s)
    for label, items in groups.items():
        lines += [f'## {label} ({len(items)} entries)', '']
        for s in items:
            lines += [f"- **{s['id']}** — {s['citation']}. {availability(s)}. Source: {s['url']}"]
            note = s.get('library', {}).get('availability_note')
            if note:
                lines += ['  ' + note]
        lines += ['']
    return '\n'.join(lines) + '\n'


def render_catalog(sources, stamp):
    lines = ['# Library', '', stamp, '',
             'Navigation over canonical source records. Entries may represent distinct uses of the same work.',
             'Downloaded, read and checked for an application are separate. Unclassified evidence is not a missing-paper finding.',
             'Use `hub.py library "words"`, `hub.py show ID`, or `hub.py reading ID` for a virtual collection.', '']
    for source in select(sources):
        lib = source.get('library', {})
        lines += [f"## {source['id']} — {source['citation']}", '',
                  f"{lib.get('kind', 'unclassified source')} | {source['check_state']} | {availability(source)}", '',
                  'Use (navigation excerpt): ' + source['use'][:220] + ('…' if len(source['use']) > 220 else ''), '']
        if lib.get('aliases'):
            lines += ['Aliases: ' + ', '.join(lib['aliases']), '']
        lines += ['Source: ' + source['url'], '']
        for c in lib.get('copies', []):
            lines += [f"- {c['role']}: `{c['root']}:{c['path']}` — {c['version']}"]
        if lib.get('availability_note'):
            lines += ['', lib['availability_note'], '']
        if lib.get('records'):
            lines += ['Members: ' + ', '.join(m['id'] for m in lib['records']), '']
    return '\n'.join(lines) + '\n'


def capture(hub, spec, *, local_file=None, existing=None, download=None, owner='library-capture'):
    """Retain bytes and publish source metadata in one validated transaction.

    Existing-source capture adds retention only; statements, review and version
    remain untouched. A downloaded new version is not a new applicability check.
    """
    hub = Path(hub).resolve()
    cat = cs.load_catalog(hub)
    if not isinstance(spec, dict):
        raise ValueError('Capture spec must be an object')
    required = ['project', 'url', 'version', 'locator']
    if not spec.get('id'):
        required += ['citation', 'use', 'limitations']
    for field in required:
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError('Capture needs nonempty string ' + field)
    for field in ['aliases', 'topics']:
        value = spec.get(field, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError('Capture needs a list of nonempty strings for ' + field)
    if not spec.get('id') and not spec.get('topics'):
        raise ValueError('New source needs topics')
    for field in ['id', 'kind', 'role', 'availability_note', 'source_version', 'version_match_basis']:
        if field in spec and (not isinstance(spec[field], str) or not spec[field].strip()):
            raise ValueError('Capture needs nonempty string ' + field)
    if 'accessed' in spec:
        try:
            date.fromisoformat(spec['accessed'])
        except (ValueError, TypeError):
            raise ValueError('Capture accessed must be an ISO date') from None
    if sum(x is not None for x in [local_file, existing, download]) > 1:
        raise ValueError('Choose file, existing reference or download')
    project = spec['project']
    if project not in cat.cfg['projects'] or cat.cfg['projects'][project]['state'] != 'active':
        raise ValueError('Capture requires an active project')
    kind = spec.get('kind', 'paper')
    if kind not in KINDS or kind == 'virtual':
        raise ValueError('Capture physical papers/texts; curate virtual members through apply')
    url, version = spec['url'], spec['version']
    role = spec.get('role', 'full-text')
    if role not in ROLES:
        raise ValueError('Unknown copy role')
    data, suffix, old_evidence = None, '', None
    if existing is not None:
        root, rel = existing.split(':', 1)
        old_evidence = dict(root=root, path=rel, locator=spec['locator'])
        path = cv.evidence_path(hub, cat.cfg, old_evidence)
        data, suffix = path.read_bytes(), path.suffix.lower()
        old_evidence['sha256'] = tx.digest(data)
    elif local_file is not None:
        path = Path(local_file)
        data, suffix = path.read_bytes(), path.suffix.lower()
    elif download is not None:
        if urlparse(download).scheme != 'https':
            raise ValueError('Download requires an HTTPS primary-source URL')
        request = urllib.request.Request(download, headers={'User-Agent': 'ResearchHub/1.0 source retention'})
        with urllib.request.urlopen(request, timeout=40) as response:
            if urlparse(response.url).scheme != 'https':
                raise ValueError('Refusing a non-HTTPS redirect')
            data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError('Download exceeds 64 MiB; download explicitly and use --file')
            suffix = '.pdf' if data.startswith(b'%PDF-') else Path(urlparse(response.url).path).suffix.lower()
            url = response.url
    if data is not None:
        if not data or suffix not in {'.pdf', '.txt', '.html', '.htm', '.tex', '.epub', '.djvu', '.ps'}:
            raise ValueError('Empty or unsupported source file')
        if suffix == '.pdf' and not data.startswith(b'%PDF-'):
            raise ValueError('File is not a PDF (possibly an access/error page)')
        if download and role == 'full-text' and suffix != '.pdf':
            raise ValueError('Non-PDF download: inspect it first, then capture with --file')
    elif not spec.get('availability_note'):
        raise ValueError('No file: explicitly record why the primary text is unavailable')
    key = spec.get('id')
    if key:
        source = copy.deepcopy(cat.by_id().get(key))
        if source is None or 'citation' not in source or cat.project_of(source) != project:
            raise ValueError('Existing ID must be a source owned by the selected project')
        errors = validate(source, cat.by_id())
        if errors:
            raise ValueError('; '.join(errors))
    else:
        for s in cat.sources:
            if s['url'].rstrip('/') == spec['url'].rstrip('/') and s['version'] == version:
                raise ValueError(f'Existing source {s["id"]}: reuse its ID or distinguish the version')
        for field in ['citation', 'topics', 'locator', 'use', 'limitations']:
            if not spec.get(field):
                raise ValueError('New source needs ' + field)
        key = ops.reserve_id(hub, project, 'L', owner)['id']
        source = {f: copy.deepcopy(spec[f]) for f in
                  ['project', 'citation', 'url', 'version', 'topics', 'locator', 'use', 'limitations']}
        source.update(id=key, check_state='lead', evidence=[])
    base = f'projects/{project}/library/{key}'
    changes = {}
    lib = source.setdefault('library', dict(kind=kind, aliases=[], copies=[], availability_note=''))
    if lib['kind'] != kind:
        raise ValueError('Existing library kind differs; reconcile explicitly through apply')
    lib.setdefault('copies', [])
    lib['aliases'] = sorted(set(lib.get('aliases', []) + spec.get('aliases', [])))
    if data is not None:
        digest = tx.digest(data)
        evidence = old_evidence or dict(root='hub', path=f'{base}/{digest}{suffix}',
                                       locator=spec['locator'], sha256=digest)
        if old_evidence is None:
            changes[evidence['path']] = data
        item = dict(evidence, role=role, version=version, url=url,
                    accessed=spec.get('accessed', datetime.now(timezone.utc).date().isoformat()))
        if 'source_version' in spec:
            if spec['source_version'] != source['version'] or not spec.get('version_match_basis'):
                raise ValueError('Explicit source_version match needs the registered version and a basis')
            item.update(source_version=spec['source_version'], version_match_basis=spec['version_match_basis'])
        # Repeating the same capture is idempotent, independent of access date.
        if not any(all(c.get(k) == item[k] for k in ['root', 'path', 'sha256', 'role', 'version'])
                   for c in lib['copies']):
            lib['copies'].append(item)
        if evidence not in source['evidence']:
            source['evidence'].append(evidence)
    lib['availability_note'] = spec.get('availability_note') or lib.get('availability_note') or 'Primary material retained; only the registered source-check scope is certified.'
    receipt = dict(source_id=key, citation=source['citation'], url=url, version=version,
                   locator=spec['locator'], kind=kind, availability_note=lib['availability_note'],
                   copies=lib['copies'], note='Retention and bibliographic metadata only; no review promotion.')
    raw = cs.serialize(receipt)
    receipt_path = f'{base}/capture-{cs.sha256_text(raw)}.json'
    changes[receipt_path] = raw
    ev = dict(root='hub', path=receipt_path, locator='capture receipt', sha256=cs.sha256_text(raw))
    if ev not in source['evidence']:
        source['evidence'].append(ev)
    # Content-addressed evidence and receipts are immutable, even if corrupted.
    # Never turn a repeat capture into an unrequested repair of retained bytes.
    retention_expected = {}
    for name, value in changes.items():
        before = tx.read_bytes(tx.safe_path(hub, name))
        proposed = value.encode('utf-8') if isinstance(value, str) else value
        if before is not None and before != proposed:
            raise ValueError(f'Retained bytes differ at {name}; reconcile explicitly')
        retention_expected[name] = tx.digest(before)
    rel = cat.location[key][1] if key in cat.location else f'projects/{project}/catalog/sources.json'
    entries = [source if s['id'] == key else s for s in cat.sources if cat.project_of(s) == project]
    if key not in cat.by_id():
        entries.append(source)
    changes[rel] = cs.serialize(entries)
    expected = dict(cat.file_hashes)
    expected.update(retention_expected)
    for name in changes:
        expected.setdefault(name, tx.digest(tx.read_bytes(tx.safe_path(hub, name))))
    result = ops.apply(hub, dict(changes=changes, expected=expected), [project], owner)
    return dict(id=key, copies=lib['copies'], **result)


def main():
    parser = argparse.ArgumentParser(description='Retain a primary source and catalogue it without promoting review status.')
    parser.add_argument('spec', type=Path, help='JSON source metadata; see templates/library-source.json')
    parser.add_argument('--hub-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--owner', required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--file', type=Path)
    group.add_argument('--existing', help='Registered root:path; retain in place')
    group.add_argument('--download', help='HTTPS primary PDF URL; retrieval precedes the publication lock')
    args = parser.parse_args()
    print(json.dumps(capture(args.hub_dir, json.loads(args.spec.read_bytes()),
                            local_file=args.file, existing=args.existing,
                            download=args.download, owner=args.owner), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
