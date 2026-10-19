"""CLI adapters. Research synthesis belongs to the calling collaborator."""
import argparse
import json
import os
from pathlib import Path
import sys


COMMANDS = {'recall', 'brief', 'compare', 'frontier', 'unlocks', 'remember',
            'graph', 'recall-index', 'session'}


def output_options(parser, project=False, owner=False):
    parser.add_argument('--json', action='store_true', help='Stable structured JSON output')
    if project:
        parser.add_argument('--project', required=True)
    if owner:
        parser.add_argument('--owner', default='recall-cli')


def configure(sub):
    p = sub.add_parser('recall', help='Ranked local retrieval with current corrections')
    p.add_argument('query'); p.add_argument('--project')
    p.add_argument('--limit', type=int, default=8)
    p.add_argument('--no-semantic', action='store_true')
    p.add_argument('--broad', action='store_true', help='Explicit cross-project candidate retrieval')
    p.add_argument('--without-aids', action='store_true')
    output_options(p)
    p = sub.add_parser('brief', help='Research evidence and candidate slots for a Gap')
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--gap'); group.add_argument('--task')
    p.add_argument('--limit', type=int, default=8)
    output_options(p, project=True)
    p = sub.add_parser('compare', help='Compare declared scope; does not certify applicability')
    p.add_argument('ids', nargs='+'); p.add_argument('--project'); output_options(p)
    p = sub.add_parser('frontier', help='Changed-input candidates and durable dispositions')
    p.add_argument('--since')
    actions = p.add_mutually_exclusive_group()
    for action in ('dismiss', 'defer', 'reactivate'):
        actions.add_argument('--' + action, metavar='ITEM')
    p.add_argument('--reason'); output_options(p, project=True, owner=True)
    p = sub.add_parser('unlocks', help='Conditional consequences and threatening controls')
    p.add_argument('query'); output_options(p, project=True)
    p = sub.add_parser('remember', help='Validate and publish a research aid; - reads stdin')
    p.add_argument('spec'); output_options(p, project=True, owner=True)
    p = sub.add_parser('graph', help='Typed graph explanations')
    actions = p.add_subparsers(dest='graph_action', required=True)
    p = actions.add_parser('neighbors'); p.add_argument('id')
    p.add_argument('--hops', type=int, default=2); p.add_argument('--project'); output_options(p)
    p = actions.add_parser('path'); p.add_argument('start'); p.add_argument('end')
    p.add_argument('--project'); output_options(p)
    p = sub.add_parser('recall-index', help='Explicit corpus setup and incremental refresh')
    actions = p.add_subparsers(dest='index_action', required=True)
    p = actions.add_parser('status'); output_options(p)
    p = actions.add_parser('manifest'); p.add_argument('--selection'); p.add_argument('--output')
    output_options(p)
    p = actions.add_parser('refresh'); p.add_argument('--project'); p.add_argument('--manifest')
    modes = p.add_mutually_exclusive_group()
    modes.add_argument('--semantic', dest='semantic', action='store_true')
    modes.add_argument('--no-semantic', dest='semantic', action='store_false')
    p.set_defaults(semantic=True); output_options(p)
    p = sub.add_parser('session', help='Durable same-service conversation context')
    actions = p.add_subparsers(dest='session_action', required=True)
    p = actions.add_parser('checkpoint'); p.add_argument('spec')
    p.add_argument('--service', required=True, choices=['codex', 'claude-code'])
    output_options(p, owner=True)
    p = actions.add_parser('refresh')
    p.add_argument('--service', required=True, choices=['codex', 'claude-code'])
    p.add_argument('--thread'); p.add_argument('--limit', type=int, default=100)
    output_options(p)


def read_spec(path):
    data = json.load(sys.stdin) if path == '-' else json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('Specification must be a JSON object')
    return data


def _text(value, indent=0):
    """Readable lossless structured text. Long evidence is expanded via show."""
    pad = '  ' * indent
    if isinstance(value, dict):
        for key, item in value.items():
            label = key.replace('_', ' ')
            if isinstance(item, (dict, list)):
                print(f'{pad}{label}:')
                _text(item, indent + 1)
            else:
                print(f'{pad}{label}: {item if item is not None else "unknown"}')
    elif isinstance(value, list):
        if not value:
            print(pad + '(none)')
        for item in value:
            if isinstance(item, (dict, list)):
                print(pad + '-'); _text(item, indent + 1)
            else:
                print(pad + '- ' + str(item))
    else:
        print(pad + str(value))


def emit(result, structured=False):
    if structured:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _text(result)


def dispatch(hub, args):
    command = args.command
    if command == 'context' and getattr(args, 'resume', False):
        from . import sessions
        service = args.service
        if service is None:
            service = 'codex' if os.environ.get('CODEX_THREAD_ID') else None
        if service is None:
            raise ValueError('Service identity unavailable; pass --service codex or claude-code')
        result = sessions.resume(hub, service, thread=args.thread, project=args.project,
                                 query=args.query or '', cwd=Path.cwd())
    elif command == 'recall':
        from . import retrieve
        if not args.query.strip() or args.limit < 1:
            raise ValueError('A nonempty query and positive limit are required')
        result = retrieve.search(hub, args.query, project=args.project, limit=args.limit,
                                 semantic=not args.no_semantic, broad=args.broad,
                                 include_knowledge=not args.without_aids)
    elif command == 'brief':
        from . import packets
        if args.limit < 1:
            raise ValueError('limit must be positive')
        result = packets.brief(hub, args.project, gap=args.gap, task=args.task, limit=args.limit)
    elif command == 'compare':
        from . import packets
        if len(args.ids) < 2:
            raise ValueError('compare requires at least two IDs')
        result = packets.compare(hub, args.ids, project=args.project)
    elif command == 'frontier':
        from . import frontier
        action = next((a for a in ('dismiss', 'defer', 'reactivate') if getattr(args, a)), None)
        if action:
            if not args.reason:
                raise ValueError('A reason is required for a frontier disposition')
            result = frontier.act(hub, args.project, getattr(args, action), action, args.reason, args.owner)
        else:
            result = frontier.scan(hub, args.project, since=args.since)
    elif command == 'unlocks':
        from . import frontier
        result = frontier.unlocks(hub, args.query, args.project)
    elif command == 'remember':
        from . import knowledge
        result = knowledge.remember(hub, read_spec(args.spec), args.project, args.owner)
    elif command == 'graph':
        from . import graph
        if args.graph_action == 'neighbors':
            if not 0 <= args.hops <= 8:
                raise ValueError('hops must be between 0 and 8')
            result = graph.neighbors(hub, args.id, project=args.project, hops=args.hops)
        else:
            result = graph.path(hub, args.start, args.end, project=args.project)
    elif command == 'recall-index':
        from . import index, corpus
        if args.index_action == 'status':
            result = index.status(hub)
        elif args.index_action == 'manifest':
            result = corpus.freeze_manifest(hub, read_spec(args.selection) if args.selection else None)
            if args.output:
                target = Path(args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('x') as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2); stream.write('\n')
        else:
            result = index.refresh(hub, project=args.project,
                                   manifest=read_spec(args.manifest) if args.manifest else None,
                                   semantic=args.semantic)
    elif command == 'session':
        from . import sessions
        if args.session_action == 'checkpoint':
            result = sessions.checkpoint(hub, read_spec(args.spec), args.service, args.owner)
        else:
            result = sessions.refresh(hub, args.service, thread=args.thread, limit=args.limit)
    else:
        return None
    emit(result, getattr(args, 'json', False))
    return 0
