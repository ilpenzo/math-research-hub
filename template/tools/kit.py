"""General-purpose installer and convenience commands over the publication kernel."""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

import catalog_store as cs
import catalog_validation as cv
import hub_operations as ops
import transactions as tx
from recall import knowledge, sessions

COMMANDS = {'add-project', 'add-archive', 'new-note', 'put', 'reserve', 'package',
            'doctor', 'hash', 'register-workspace', 'enable-semantics'}


def configure(sub):
    p = sub.add_parser('add-archive'); p.add_argument('path'); p.add_argument('--project', action='append', required=True); p.add_argument('--alias')
    p = sub.add_parser('new-note'); p.add_argument('topic'); p.add_argument('--project', required=True); p.add_argument('--owner', default='researcher')
    p = sub.add_parser('put'); p.add_argument('file'); p.add_argument('--project'); p.add_argument('--expect', action='append', default=[]); p.add_argument('--owner', default='researcher')
    p = sub.add_parser('reserve'); p.add_argument('--project', required=True); p.add_argument('--kind', choices=list('ROCQ LK'.replace(' ', '')), default='R'); p.add_argument('--owner', default='researcher')
    p = sub.add_parser('package'); p.add_argument('spec'); p.add_argument('--project', required=True); p.add_argument('--dest', required=True)
    p = sub.add_parser('hash'); p.add_argument('paths', nargs='+')
    sub.add_parser('doctor')
    p = sub.add_parser('register-workspace'); g = p.add_mutually_exclusive_group(); g.add_argument('--move', action='store_true'); g.add_argument('--new-identity', action='store_true')
    p = sub.add_parser('enable-semantics'); p.add_argument('--model', required=True); p.add_argument('--url', default='http://127.0.0.1:11434')


def publish(hub, changes, scopes, owner='kit', catalog=None, expected_extra=None):
    cat = catalog or cs.load_catalog(hub)
    expected = dict(cat.file_hashes)
    for path in changes:
        expected.setdefault(path, tx.digest(tx.read_bytes(Path(hub)/path)))
    expected.update(expected_extra or {})
    return ops.apply(hub, dict(changes=changes, expected=expected), scopes, owner)


def project_files(hub, name, title, prefix, description='', conventions=''):
    values = dict(TITLE=title, SLUG=name, PREFIX=prefix.rstrip('-'), DATE=date.today().isoformat(),
                  DESCRIPTION=description or '(Describe the main question here.)',
                  CONVENTIONS=conventions or '- State the category, conventions and exact hypotheses.', OWNER='the hub owner')
    changes = {}
    for template in (Path(hub)/'templates/project').glob('*.md'):
        text = template.read_text()
        for key, value in values.items():
            text = text.replace('{{'+key+'}}', value)
        changes[f'projects/{name}/{template.name}'] = text
    return changes


def navigation(hub, cfg):
    path = Path(hub)/'START_HERE.md'
    text = path.read_text()
    start, end = '<!-- projects:start -->', '<!-- projects:end -->'
    if start not in text or end not in text:
        return {}
    rows = ['| Project | Guide | State |', '|---|---|---|']
    for name, info in cfg['projects'].items():
        rows.append(f'| {info["title"]} | [projects/{name}/START_HERE.md](projects/{name}/START_HERE.md) | {info["state"]} |')
    before, rest = text.split(start, 1); _, after = rest.split(end, 1)
    return {'START_HERE.md': before+start+'\n'+'\n'.join(rows)+'\n'+end+after}


def add_project(hub, args):
    name, prefix = args.name, args.prefix.upper().rstrip('-')+'-'
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or name == 'hub':
        raise ValueError('Invalid or reserved project name')
    cfg = json.loads((hub/'hub.json').read_text())
    if not cfg.get('projects'):
        # First project, inside the installer's disposable staging directory.
        cfg.update(schema_version=2, catalog_layout='federated', roots={}, projects={}, legacy_default_project=name,
                   exclude_dirs=['.git', '.hub', '.venv', 'venv', 'node_modules', '__pycache__'],
                   extensions=['.md', '.txt', '.tex', '.bib', '.pdf', '.py', '.sage', '.lean', '.json', '.csv', '.jl', '.m2', '.g', '.gp', '.magma'],
                   text_extensions=['.md', '.txt', '.tex', '.bib', '.py', '.sage', '.lean', '.json', '.csv', '.jl', '.m2', '.g', '.gp', '.magma'],
                   infrastructure_routes=['START_HERE.md', 'AGENTS.md', 'CLAUDE.md', 'WORKFLOW.md', 'PROOF_SWARM.md', 'PROMPTS.md'])
        cfg.pop('archives', None)
        cfg['projects'][name] = dict(title=args.title, roots=[], state='active', id_prefix=prefix,
            catalog_state='initialized', document_routes=[f'projects/{name}'], archive_selectors=[], shared_selectors=[], exclude_selectors=[], code_roots=[])
        cs.validate_config(hub, cfg)
        changes = {'hub.json': cs.serialize(cfg), 'catalog/knowledge.json': cs.serialize(knowledge.empty_envelope()),
                   f'projects/{name}/catalog/records.json': '[]\n', f'projects/{name}/catalog/sources.json': '[]\n',
                   f'projects/{name}/catalog/knowledge.json': cs.serialize(knowledge.empty_envelope())}
        changes.update(project_files(hub,name,args.title,prefix,args.description,args.conventions))
        changes.update(navigation(hub,cfg))
        expected = {key: tx.digest(tx.read_bytes(hub/key)) for key in changes}
        if any(value is not None for key,value in expected.items() if key not in {'hub.json', 'START_HERE.md'}):
            raise ValueError('First-project setup refuses existing data')
        def validate(overrides):
            cat = cs.load_catalog(hub, overrides=overrides, already_locked=True)
            errors = cv.validate(cat, hub, overrides=overrides)
            if errors: raise ValueError('\n'.join(errors))
        result = dict(transaction=tx.apply_batch(hub,changes,expected,scopes=['hub'],owner='initialize',validate=validate))
    else:
        if cfg.get('schema_version') != 2:
            raise ValueError('This is a version 1 hub. Follow UPGRADE.md; do not replace its tool in place.')
        result = ops.add_project(hub,name,args.title,prefix)
        cat = cs.load_catalog(hub)
        changes = project_files(hub,name,args.title,prefix,args.description,args.conventions)
        changes.update(navigation(hub,cat.cfg))
        if changes: result = publish(hub,changes,['hub'],catalog=cat)
    return result


def add_archive(hub, args):
    cat = cs.load_catalog(hub); cfg = copy.deepcopy(cat.cfg)
    path = Path(args.path).expanduser().resolve()
    if not path.is_dir() or set(args.project)-set(cfg['projects']):
        raise ValueError('Existing archive directory and registered project required')
    alias = args.alias or re.sub(r'[^a-z0-9]+','-',path.name.lower()).strip('-') or 'archive'
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*',alias) or alias == 'hub':
        raise ValueError('Invalid or reserved archive alias')
    if alias in cfg['roots'] and cfg['roots'][alias] != str(path):
        raise ValueError('Archive alias already belongs to another path; choose --alias')
    cfg['roots'][alias] = str(path)
    for project in args.project:
        info = cfg['projects'][project]
        if alias not in info['roots']: info['roots'].append(alias)
        selector = dict(root=alias, selector='subtree', path='')
        if selector not in info['archive_selectors']: info['archive_selectors'].append(selector)
    return publish(hub,{'hub.json':cs.serialize(cfg)},['hub'],catalog=cat)


def entry_hash(entry):
    return hashlib.sha256(cs.canonical(entry).encode()).hexdigest()


def put(hub, args):
    data = json.loads(Path(args.file).read_text())
    rows = data if isinstance(data,list) else [data]
    cat = cs.load_catalog(hub); known = cat.by_id(); changes = {}; affected = set(); seen = set()
    expected_entries = dict(item.split('=',1) for item in args.expect)
    reservations = {}
    registries = {}
    for supplied in rows:
        entry = copy.deepcopy(supplied)
        project = entry.get('project') or args.project
        if project not in cat.cfg['projects']: raise ValueError('Every entry needs a registered project')
        entry['project'] = project
        kind = 'sources' if 'citation' in entry else 'records'
        letter = 'L' if kind=='sources' else {'result':'R','conjecture':'R','question':'Q','obstruction':'O','correction':'C'}.get(entry.get('kind'))
        if letter is None: raise ValueError('Unknown record kind')
        rel = f'projects/{project}/catalog/{kind}.json'
        registry = registries.setdefault(rel,copy.deepcopy(getattr(cat,kind)))
        registry[:] = [r for r in registry if r['project']==project]
        reserve_rel = f'projects/{project}/catalog/reservations.json'
        if reserve_rel not in reservations:
            raw = tx.read_bytes(hub/reserve_rel)
            reservations[reserve_rel] = ops.read_reservations(raw)
            cat.file_hashes[reserve_rel] = tx.digest(raw)
        allocated = reservations[reserve_rel]
        key = entry.get('id','auto')
        prefix = cat.cfg['projects'][project]['id_prefix']+letter
        if key == 'auto':
            taken = set(known) | seen | {r['id'] for r in allocated}
            numbers = [int(k[len(prefix):]) for k in taken if k.startswith(prefix) and k[len(prefix):].isdigit()]
            key = prefix+f'{max(numbers,default=0)+1:03d}'; entry['id']=key
        if key in seen: raise ValueError('Duplicate ID in input')
        seen.add(key)
        if key in known:
            if known[key]['project'] != project: raise ValueError('Use an explicit scoped apply batch for ownership transfers')
            expected = expected_entries.get(key)
            if not expected or expected != entry_hash(known[key]):
                raise ValueError(f'{key}: update requires --expect {key}=HASH from show --json')
        elif not any(r['id']==key for r in allocated):
            allocated.append(dict(id=key,owner=args.owner))
        for ev in list(entry.get('evidence',[]))+([entry['review']['artifact']] if entry.get('review',{}).get('artifact') else []):
            if ev.get('sha256')=='auto': ev['sha256']=cs.sha256_file(cv.evidence_path(hub,cat.cfg,ev))
        registry[:] = [r for r in registry if r['id'] != key]; registry.append(entry)
        affected.add(project)
    for rel, rows in registries.items(): changes[rel]=cs.serialize(rows)
    for rel, rows in reservations.items(): changes[rel]=cs.serialize(rows)
    result = publish(hub,changes,affected,args.owner,cat)
    result['ids']=sorted(seen)
    return result


def register(hub, args):
    cat = cs.load_catalog(hub); state = sessions.workspace_identity(hub,cat)
    existing = next((r for r in cat.knowledge['hub']['checkpoints'] if r['id']==sessions.IDENTITY_ID),None)
    if state['state']=='current' and not args.new_identity: return state
    if existing and not (args.move or args.new_identity):
        raise ValueError('Copied/moved hub: choose --move to retain identity or --new-identity for an independent copy')
    row = dict(id=sessions.IDENTITY_ID, workspace_id=existing['workspace_id'] if existing and args.move else str(uuid.uuid4()),
               registered_root=str(hub.resolve()), prior_roots=(existing.get('prior_roots',[])+[existing['registered_root']]) if existing and args.move else [])
    return knowledge.publish_section(hub,'hub','checkpoints',row,'register-workspace',expected_revision=existing['revision'] if existing else None)


def dispatch(hub, args):
    hub = Path(hub).resolve(); command = args.command
    if command == 'add-project': result=add_project(hub,args)
    elif command == 'add-archive': result=add_archive(hub,args)
    elif command == 'put': result=put(hub,args)
    elif command == 'reserve': result=ops.reserve_id(hub,args.project,args.kind,args.owner)
    elif command == 'register-workspace': result=register(hub,args)
    elif command == 'enable-semantics':
        from recall.providers import Ollama
        config=dict(provider='ollama',model=args.model,url=args.url,enabled=True)
        pin=Ollama(config).identity(); config['digest']=pin['digest']
        result=publish(hub,{'catalog/recall-config.json':cs.serialize(config)},['hub'])
    elif command == 'package':
        import artifacts
        result=artifacts.pack(hub,args.project,args.dest,json.loads(Path(args.spec).read_text()))
    elif command == 'new-note':
        cat=cs.load_catalog(hub)
        if args.project not in cat.cfg['projects']: raise ValueError('Unknown project')
        slug=re.sub(r'[^a-z0-9]+','-',args.topic.lower()).strip('-')
        if not slug: raise ValueError('Supply a meaningful topic')
        rel=f'projects/{args.project}/investigations/{date.today().isoformat()}-{slug}/NOTE.md'
        if (hub/rel).exists(): raise ValueError('NOTE already exists; continue that investigation')
        text=(hub/'templates/investigation.md').read_text()
        for key,value in dict(TITLE=args.topic,TOPIC=args.topic,PROJECT=args.project,DATE=date.today().isoformat(),OWNER=args.owner).items():
            text=text.replace('{{'+key+'}}',value)
        result=publish(hub,{rel:text},[args.project],args.owner,cat,{rel:None}); result['path']=rel
    elif command == 'hash':
        result={p:cs.sha256_file(Path(p)) for p in args.paths}
    elif command == 'doctor':
        from recall import index
        cat=cs.load_catalog(hub)
        result=dict(python=sys.version,projects=list(cat.cfg['projects']),identity=sessions.workspace_identity(hub,cat),
                    optional_tools={x:bool(shutil.which(x)) for x in ['pdftotext','ollama','claude','codex']},recall=index.status(hub),
                    note='No installs, model calls or native transcript scans performed.')
    else: raise ValueError('Unknown kit command')
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
