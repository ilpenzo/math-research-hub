#!/usr/bin/env python3
"""Account for all directory entries and screen documents without certifying claims."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote

HUB = Path(__file__).resolve().parents[1]
DEPENDENCIES = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', 'toricpy.egg-info'}
BUILD_EXTENSIONS = {'.aux', '.toc', '.fls', '.fdb_latexmk', '.gz', '.pyc'}
MARKER = re.compile(r'(?i)(\b(theorem|lemma|proposition|corollary|correction|errat\w*|counterexample|countermodel|refut\w*|supersed\w*|retract\w*|verdict|failure|failed|missing|unproved|unresolved|conjecture)\b|exact.gap|not.proved|not.certified)')
LINK = re.compile(r'\]\((<[^>]+>|[^)]+)\)')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def role(path, indexed):
    p = Path(path)
    if any(x in DEPENDENCIES for x in p.parts[:-1]):
        return 'dependency-or-runtime-metadata'
    if p.name == '.DS_Store' or p.name == '.gitignore' or '.claude' in p.parts:
        return 'administrative-or-command-history'
    if p.suffix.lower() in BUILD_EXTENSIONS:
        return 'build-product'
    if indexed:
        return 'indexed-evidence-or-support'
    return 'unindexed-needs-disposition'


def document_lines(path):
    if path.suffix.lower() == '.ipynb':
        obj = json.loads(path.read_text())
        return '\n'.join(''.join(c.get('source', [])) for c in obj.get('cells', [])).splitlines()
    if path.suffix.lower() == '.docx':
        with zipfile.ZipFile(path) as z:
            obj = ET.fromstring(z.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return [''.join(t.text or '' for t in p.findall('.//w:t', ns))
                for p in obj.findall('.//w:p', ns)]
    if path.suffix.lower() == '.pdf':
        extractor = shutil.which('pdftotext')
        if not extractor:
            raise OSError('PDF screening requires pdftotext')
        result = subprocess.run([extractor, '-layout', str(path), '-'],
                                capture_output=True, timeout=60, check=True)
        # Preserve page boundaries as lines so marker locators include PDF pages.
        return result.stdout.decode('utf-8', errors='replace').replace('\f', '\n\f\n').split('\n')
    return path.read_text(errors='replace').splitlines()


def checkpoint_info(path):
    original = path.parent.parent / path.name.replace('-checkpoint', '')
    return dict(original=str(original), original_exists=original.is_file(),
                exact_duplicate=original.is_file() and path.read_bytes() == original.read_bytes())


def zip_members(path):
    result = []
    with zipfile.ZipFile(path) as z:
        for member in z.infolist():
            if member.is_dir():
                continue
            target = (path.parent / member.filename).resolve()
            safe = target.is_relative_to(path.parent.resolve())
            data = z.read(member)
            result.append(dict(member=member.filename, bytes=len(data), sha256=sha(data),
                               loose_path=str(target) if safe else None,
                               disposition='exact-duplicate-of-loose-file' if safe and target.is_file() and sha(target.read_bytes()) == sha(data) else 'distinct-member-review-pending'))
    return result


def screen(path, root, relative, registered):
    lines = document_lines(path)
    headings, candidates, missing_links = [], [], []
    fenced = False
    page = 1
    for number, line in enumerate(lines, 1):
        if line == '\f':
            page += 1
            continue
        locator = dict(line=number)
        if path.suffix.lower() == '.pdf':
            locator['page'] = page
        if line.lstrip().startswith('```'):
            fenced = not fenced
        if line.startswith('#') or line.startswith('\\section') or line.startswith('\\subsection'):
            headings.append(dict(**locator, text=line[:300]))
        if MARKER.search(line):
            candidates.append(dict(**locator, text=line[:400]))
        if not fenced and path.suffix.lower() == '.md':
            for m in LINK.finditer(line):
                raw = m.group(1).strip().strip('<>')
                if '://' in raw or raw.startswith(('#', 'mailto:', 'data:')):
                    continue
                # Quoted Markdown titles are not part of a target.
                raw = re.split(r'\s+["\']', raw, maxsplit=1)[0]
                target = unquote(raw.split('#')[0])
                target = re.sub(r':\d+(?::\d+)?$', '', target)
                if not target:
                    continue
                # Avoid mathematical expressions such as [M](t) in prose.
                if '/' not in target and not Path(target).suffix and not target.startswith('.'):
                    continue
                candidate = Path(target).expanduser()
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                if not candidate.exists():
                    missing_links.append(dict(line=number, target=target, resolved=str(candidate.resolve())))
    methods = {'.ipynb': 'notebook-cell-source', '.pdf': 'pdf-text-layer-markers',
               '.docx': 'word-paragraph-markers'}
    return dict(root=root, path=relative, sha256=sha(path.read_bytes()),
                screen=methods.get(path.suffix.lower(), 'text-markers-and-local-links'),
                extracted_text_nonempty=any(line.strip() for line in lines),
                mathematical_review='pending', registered_records=registered,
                headings=headings, candidate_markers=candidates, missing_local_links=missing_links)


def run(publisher=None, snapshot=None):
    if publisher is None:
        import hub
        return hub.sweep()
    cfg, inv, catalog = snapshot
    registered = defaultdict(list)
    for r in catalog.records:
        for e in r['evidence']:
            registered[(e['root'], e['path'])].append(r['id'])
    indexed = {(e['root'], e['path']) for e in inv['entries']}
    dirs, omissions, docs, containers, checkpoints, errors = [], [], [], [], [], []
    totals = {}
    for root, base in cfg['roots'].items():
        file_count = 0
        # Enumerate dependency directories too; do not read their file bodies or execute anything.
        for directory, children, filenames in os.walk(base, followlinks=False, onerror=lambda e: errors.append(str(e))):
            children.sort(); filenames.sort()
            dpath = Path(directory)
            relative_dir = dpath.relative_to(base).as_posix()
            categories = Counter()
            for name in children:
                p = dpath / name
                if p.is_symlink():
                    omissions.append(dict(root=root, path=p.relative_to(base).as_posix(), disposition='symlink-not-followed', target=os.readlink(p)))
            for name in filenames:
                path = dpath / name; rel = path.relative_to(base).as_posix()
                file_count += 1
                category = role(rel, (root, rel) in indexed)
                if path.is_symlink():
                    category = 'symlink-not-followed'
                categories[category] += 1
                if (root, rel) not in indexed:
                    omissions.append(dict(root=root, path=rel, disposition=category))
                if category in {'dependency-or-runtime-metadata', 'symlink-not-followed'}:
                    continue
                try:
                    if '.ipynb_checkpoints' in path.parts:
                        checkpoints.append(dict(root=root, path=rel, **checkpoint_info(path)))
                    if path.suffix.lower() == '.zip':
                        containers.append(dict(root=root, path=rel, members=zip_members(path)))
                    if path.suffix.lower() in {'.md', '.tex', '.txt', '.ipynb', '.pdf', '.docx'} and name != 'MEMORY.md':
                        docs.append(screen(path, root, rel, sorted(set(registered[(root, rel)]))))
                except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, subprocess.SubprocessError) as exc:
                    errors.append(f'{root}:{rel}: {exc}')
            dirs.append(dict(root=root, path=relative_dir, direct_files=len(filenames), subdirectories=len(children), dispositions=dict(categories)))
        totals[root] = dict(directories=sum(d['root'] == root for d in dirs), files=file_count)
    by_name = defaultdict(list)
    for e in inv['entries']:
        by_name[Path(e['path']).name].append(e)
    for doc in docs:
        for link in doc['missing_local_links']:
            suffix = link['target'].lstrip('./')
            candidates = [e for e in by_name[Path(link['target']).name]
                          if e['path'].endswith(suffix)]
            link['candidate_paths'] = [dict(root=e['root'], path=e['path']) for e in candidates]
            link['disposition'] = 'unique-existing-suffix-match' if len(candidates) == 1 else 'ambiguous-existing-matches' if candidates else 'target-not-located'
    stamp = datetime.now(timezone.utc).isoformat()
    data = dict(generated_at=stamp, inventory_generated_at=inv['generated_at'], scope='Every directory and file under the manifest roots; no symlink traversal; runtime directories enumerated but bodies unread.', totals=totals, directories=dirs, not_in_inventory=omissions, zip_containers=containers, notebook_checkpoints=checkpoints, errors=errors)
    publisher('indexes/DIRECTORY_SWEEP.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    screen_data = dict(generated_at=stamp, scope='Every nonruntime MD/TEX/TXT, notebook cell source, Word paragraph and PDF text layer, except legacy MEMORY. Marker matches are candidates, not extracted/certified claims. Notebook line numbers refer to flattened cell sources; Word line numbers are paragraph numbers; PDFs also carry page numbers. No OCR or notebook-output execution.', documents=docs, errors=errors)
    publisher('indexes/DOCUMENT_SCREEN.json', json.dumps(screen_data, ensure_ascii=False, indent=2) + '\n')
    by_root = Counter(d['root'] for d in docs)
    categories = Counter(e['disposition'] for e in omissions)
    out = ['# Full directory sweep', '', f'Generated {stamp}.', '',
           'This includes files outside the ordinary inventory filter. Enumeration and document screening are not proof review.', '',
           '| Root | Directories | Files | Screened documents |', '|---|---:|---:|---:|']
    for root, counts in totals.items():
        out.append(f"| {root} | {counts['directories']} | {counts['files']} | {by_root[root]} |")
    out += ['', '## Outside the inventory', ''] + [f'- {k}: {n}' for k, n in sorted(categories.items())]
    out += ['', f"Unassigned nonruntime files: {categories['unindexed-needs-disposition']}.", f'Read/traversal errors: {len(errors)}.', '',
            'Full per-directory accounting and excluded paths: DIRECTORY_SWEEP.json.',
            'Every screened document, marker locator and missing local link candidate: DOCUMENT_SCREEN.json.',
            'Missing-link candidates need contextual review; paths inside old transcripts may be historical.', '',
            '## ZIP members and checkpoints', '']
    for z in containers:
        out.append(f"- {z['root']}:{z['path']}: {len(z['members'])} members; " + str(dict(Counter(m['disposition'] for m in z['members']))))
    for c in checkpoints:
        out.append(f"- {c['root']}:{c['path']}: " + ('exact duplicate' if c['exact_duplicate'] else 'distinct; original exists' if c['original_exists'] else 'original absent; retained evidence'))
    out += ['', '## Document-screen scope', '',
            f"{len(docs)} documents screened; {sum(bool(d['candidate_markers']) for d in docs)} contain result/correction/gap markers; {sum(len(d['missing_local_links']) for d in docs)} unresolved local-link occurrences.",
            'Individual results remain pending unless present in the canonical claim registry. Registered evidence files can contain additional unextracted results.', '',
            f"Documents with no extractable text: {sum(not d['extracted_text_nonempty'] for d in docs)} (listed in DOCUMENT_SCREEN.json).",
            'PDF text layers and Word paragraphs are screened for candidates; source images are inventoried without OCR. Primary-source statements are not thereby checked.', '']
    publisher('indexes/DIRECTORY_SWEEP.md', '\n'.join(out))
    print(json.dumps(dict(totals=totals, documents=len(docs), unindexed_dispositions=dict(categories), errors=len(errors))))
    return bool(errors or categories['unindexed-needs-disposition'])


if __name__ == '__main__':
    raise SystemExit(run())
