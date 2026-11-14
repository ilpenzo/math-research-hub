"""Prospector: parallel proof-search rounds (prospect, assay, dig).

Launches independent explorers through the Codex and Claude CLIs, keeps every
prompt, response and claim card, and enforces the run's call budget. It finds
candidates; it certifies nothing. See PROSPECTOR.md.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HUB = Path(__file__).resolve().parent.parent
TEMPLATES = HUB / 'templates' / 'prospector'
STAGES = ('prospect', 'assay', 'dig', 'variants')
BACKENDS = ('codex', 'claude', 'stub')
STATUSES = ('open', 'survives', 'killed', 'unclear', 'promoted', 'parked', 'duplicate')
COMPLETED = ('ok', 'parse-error')
OUTCOMES = ('proof-candidate', 'partial', 'counterexample', 'reduction', 'none')
CARD_KINDS = ('lemma', 'construction', 'reduction', 'counterexample', 'strategy', 'argument')
CARD_TEXT = ('title', 'statement', 'role', 'sketch', 'weakest_step', 'kill_test',
             'evidence', 'differs_from', 'gap_after')
VARIANT_STATUSES = ('open', 'solved', 'refuted', 'parked')
VARIANT_KINDS = ('special-case', 'lower-dimension', 'stronger-hypothesis', 'weaker-conclusion',
                 'analogue')
VARIANT_RELATIONS = ('implies-target', 'implied-by-target', 'analogue')
VARIANT_TEXT = ('title', 'statement', 'relation_argument', 'first_step')
DEFAULT_REVIEW_SCOPE = 'unreviewed lead'
FRONTIER_WORD_LIMIT = 1200
HEARTBEAT_SECONDS = 30
MONITOR_SECONDS = 1
FIRST_RETURN_FILE = 'FIRST_RETURN.txt'
FIRST_RETURN_MAX_BYTES = 16384


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def append(path, obj):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + '\n')


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def fill(template, values):
    for key, value in values.items():
        template = template.replace('{{' + key + '}}', value)
    return template


def clip(text, limit):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


# Run state -----------------------------------------------------------------

def open_run(run_dir):
    run = Path(run_dir).resolve()
    if not (run / 'run.json').exists():
        sys.exit(f'Not a Prospector run (no run.json): {run}')
    return run, load(run / 'run.json')


def round_dir(run, number):
    return run / 'rounds' / f'r{number:02d}'


def round_numbers(run):
    rounds = run / 'rounds'
    if not rounds.exists():
        return []
    return sorted(int(p.name[1:]) for p in rounds.iterdir() if re.fullmatch(r'r\d+', p.name))


def all_slots(run):
    for number in round_numbers(run):
        path = round_dir(run, number) / 'slots.json'
        if path.exists():
            for slot in load(path):
                yield number, slot


def cards_by_id(run):
    return {card['id']: card for card in read_jsonl(run / 'cards.jsonl')}


def card_status(run):
    status = {card_id: 'open' for card_id in cards_by_id(run)}
    for event in read_jsonl(run / 'dispositions.jsonl'):
        status[event['card']] = event['status']
    return status


def budget_use(run):
    ledger = read_jsonl(run / 'ledger.jsonl')
    done = [e for e in ledger if e['status'] in COMPLETED]
    return len(done), sum(1 for e in done if e['backend'] == 'claude'), ledger


def lenses(spec):
    library = load(TEMPLATES / 'lenses.json')['lenses']
    if spec.get('lenses_extra'):
        library += load(HUB / spec['lenses_extra'])['lenses']
    return {lens['id']: lens for lens in library}, [lens['id'] for lens in library]


# variants ------------------------------------------------------------------

def variants_by_id(run):
    return {variant['id']: variant for variant in read_jsonl(run / 'variants.jsonl')}


def variant_state(run):
    """Latest status event per variant; a variant with no event is open."""
    state = {variant_id: dict(status='open') for variant_id in variants_by_id(run)}
    for event in read_jsonl(run / 'variant-events.jsonl'):
        if event['variant'] in state:
            state[event['variant']] = event
    return state


def open_variants(run):
    """Open variants, easiest first; ties keep the order in which they were recorded."""
    state = variant_state(run)
    pool = [v for v in variants_by_id(run).values() if state[v['id']]['status'] == 'open']
    return sorted(pool, key=lambda v: v['difficulty'])


def solved_variants(run):
    """Solved variant ids, most recently solved first."""
    state, latest = variant_state(run), {}
    for index, event in enumerate(read_jsonl(run / 'variant-events.jsonl')):
        latest[event['variant']] = index
    solved = [v for v in state if state[v]['status'] == 'solved']
    return sorted(solved, key=lambda v: -latest[v])


def variant_proof(run, variant_id):
    """The retained proof text of a solved variant, refused if its bytes changed."""
    event = variant_state(run)[variant_id]
    raw = (run / event['proof']).read_bytes()
    if sha(raw) != event['proof_sha256']:
        sys.exit(f'Proof of variant {variant_id} does not match its recorded hash; '
                 'record the changed proof again with `variant solve`.')
    return raw.decode('utf-8', errors='replace'), event


def normal_variant(raw):
    variant = {key: str(raw.get(key, '') or '') for key in VARIANT_TEXT}
    variant['kind'] = raw.get('kind') if raw.get('kind') in VARIANT_KINDS else 'special-case'
    variant['relation'] = raw.get('relation') if raw.get('relation') in VARIANT_RELATIONS else 'analogue'
    variant['relation_status'] = 'proved' if raw.get('relation_status') == 'proved' else 'conjectured'
    try:
        variant['difficulty'] = min(5, max(1, int(raw.get('difficulty', 3))))
    except (TypeError, ValueError):
        variant['difficulty'] = 3
    return variant


def variant_line(variant, status=None):
    relation = {'implies-target': 'implies the target', 'implied-by-target': 'is implied by the target',
                'analogue': 'is an analogue of the target'}[variant['relation']]
    return (f"{variant['id']}" + (f' [{status}]' if status else '') + f" ({variant['kind']}, difficulty "
            f"{variant['difficulty']}/5; {relation}, relation {variant['relation_status']}): "
            f"{variant['statement']}")


class InlineBudget:
    """Shared character allowance for inlined material: a text goes in whole or not at all."""

    def __init__(self, limit):
        self.room, self.omitted, self.words = max(0, limit), [], 0

    def take(self, label, text):
        if len(text) > self.room:
            self.omitted.append(f'{label}: omitted whole ({len(text)} characters; {self.room} left '
                                'of max_inline_chars)')
            return False
        self.room -= len(text)
        self.words += len(text.split())
        return True


def ladder_settings(spec):
    return spec.get('variant_ladder') or {}


def slot_variant(run, slot):
    """The variant a slot works on: its own, or the one its first card was written for."""
    if slot.get('variant'):
        return slot['variant']
    known = cards_by_id(run)
    for card_id in slot.get('cards') or []:
        if known.get(card_id, {}).get('variant'):
            return known[card_id]['variant']
    return None


def variant_assignment(run, variant_id):
    variant, state = variants_by_id(run)[variant_id], variant_state(run)[variant_id]
    return '\n'.join([
        '## Assigned variant (work on this statement, not the full target)', '',
        variant_line(variant, state['status']), '',
        f"Why the relation holds: {variant['relation_argument'] or 'not stated'}",
        'The target in the frontier supplies motivation and notation. Settle this variant: prove it, '
        'refute it, or isolate its earliest gap. Keep its exact hypotheses.'])


def solved_block(run, spec, slot, budget):
    """Complete proofs of solved variants: each whole with its review scope, or omitted and said so."""
    chosen = slot.get('solved_variants')
    if chosen is None:
        chosen = solved_variants(run)[:ladder_settings(spec).get('inline_solved', 2)]
    if not chosen:
        return ''
    known = variants_by_id(run)
    parts = ['## Solved variants of the target, with their complete proofs', '',
             'These are neighbours of the target that have been settled. Study how the proofs work; '
             'each carries its review scope, and none is a certified result unless its scope says so.']
    for variant_id in chosen:
        proof, event = variant_proof(run, variant_id)
        check_hidden(spec, f'proof of variant {variant_id}', proof + known[variant_id]['statement'])
        scope = event.get('review_scope') or DEFAULT_REVIEW_SCOPE
        parts += ['', f'### {variant_line(known[variant_id], "solved")}', '', f'Review scope: {scope}.']
        if budget.take(f'proof of variant {variant_id}', proof):
            parts += ['', f'--- begin complete proof of {variant_id} ---', proof.strip(),
                      f'--- end proof of {variant_id} ---']
        else:
            parts += ['', f'Proof omitted, not shortened: it has {len(proof)} characters and does not fit '
                          'the remaining inline allowance. Only the statement above is available here.']
    return '\n'.join(parts)


def cmd_variant(args):
    run, spec = open_run(args.run_dir)
    known, state = variants_by_id(run), variant_state(run)
    if args.action == 'list':
        for variant in sorted(known.values(), key=lambda v: (v['difficulty'], v['id'])):
            event = state[variant['id']]
            print(clip(variant_line(variant, event['status']), 220))
            if event['status'] == 'solved':
                print(f"    proof {event['proof']} sha256 {event['proof_sha256'][:12]}… "
                      f"review scope: {event.get('review_scope') or DEFAULT_REVIEW_SCOPE}")
        print(f'{len(known)} variants: ' + (', '.join(f'{k}={v}' for k, v in sorted(
            Counter(e['status'] for e in state.values()).items())) or 'none'))
        return
    if args.action == 'add':
        number = 1 + sum(1 for variant_id in known if variant_id.startswith('conductor-v'))
        variant = dict(id=f'conductor-v{number}', round=None, slot=None, backend='conductor',
                       created_utc=utc(), **normal_variant(vars(args)))
        if not variant['statement'].strip():
            sys.exit('A variant needs an exact statement')
        append(run / 'variants.jsonl', variant)
        print(f"Added {variant_line(variant, 'open')}")
        return
    if args.variant not in known:
        sys.exit(f'Unknown variant {args.variant}')
    event = dict(variant=args.variant, status=None, reason=args.reason or '', by='conductor', utc=utc())
    if args.action == 'mark':
        event['status'] = args.status
    else:  # solve: keep the complete proof as a hashed file beside the run's other evidence
        source = Path(args.proof)
        try:
            raw = source.read_bytes()
        except OSError as exc:
            sys.exit(f'Proof file: {exc}')
        if not raw.strip():
            sys.exit('The proof file is empty; a solved variant keeps its complete proof')
        if args.card and args.card not in cards_by_id(run):
            sys.exit(f'Unknown card {args.card}')
        digest = sha(raw)
        folder = run / 'variants' / args.variant
        folder.mkdir(parents=True, exist_ok=True)
        kept = folder / f'proof-{digest[:12]}{source.suffix or ".md"}'
        kept.write_bytes(raw)
        event.update(status='solved', proof=str(kept.relative_to(run)), proof_sha256=digest,
                     review_scope=args.review_scope or DEFAULT_REVIEW_SCOPE, card=args.card)
    append(run / 'variant-events.jsonl', event)
    print(f"Variant {args.variant}: {event['status']}"
          + (f" (proof {event['proof']}, review scope: {event['review_scope']})"
             if event['status'] == 'solved' else '')
          + '. This is run state only; no catalog record or status was changed.')


# init ----------------------------------------------------------------------

def hub_brief(project, gap):
    command = [sys.executable, str(HUB / 'tools' / 'hub.py'), 'brief', '--project', project,
               '--gap', gap, '--json']
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=600, cwd=HUB)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'warning: hub brief failed: {exc}', file=sys.stderr)
        return None
    if done.returncode:
        print(f'warning: hub brief failed: {done.stderr.strip()[:300]}', file=sys.stderr)
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        print('warning: hub brief returned no JSON', file=sys.stderr)
        return None


def quote(field):
    if isinstance(field, dict):
        return field.get('quote') or field.get('text') or ''
    return field or ''


def frontier_seed(brief):
    if not brief:
        return ''
    lines = ['## Seed from hub brief (edit into the sections above, then delete)', '']
    gap = brief.get('gap') or {}
    lines.append(f"Gap {gap.get('id')} r{gap.get('revision')}: {clip(quote(gap.get('question')), 600)}")
    lines.append('')
    for item in brief.get('ingredients') or []:
        lines.append(f"- {item.get('id')} ({item.get('kind')}/{item.get('status')}): {item.get('title')}. "
                     f"{clip(quote(item.get('statement')), 300)}")
        for caution in item.get('scope_cautions') or []:
            lines.append(f'  - caution: {clip(caution, 200)}')
    for key in ('attempts', 'reminders', 'stored_connections', 'corrections'):
        for item in brief.get(key) or []:
            lines.append(f"- {key[:-1]} {item.get('id', '')}: {clip(item.get('title') or item, 200)}")
    if brief.get('ingredient_drill_down'):
        lines.append(f"- further IDs: {', '.join(map(str, brief['ingredient_drill_down']))}")
    return '\n'.join(lines) + '\n'


def cmd_init(args):
    run = Path(args.run_dir).resolve()
    if (run / 'run.json').exists():
        sys.exit(f'Run already initialized: {run}')
    (run / 'rounds').mkdir(parents=True, exist_ok=True)
    spec = load(TEMPLATES / 'run.json')
    spec.update(name=run.name, project=args.project, gap=args.gap, target=args.target or '',
                owner=args.owner or '', created_utc=utc())
    if args.authorization:
        spec['authorization'] = args.authorization
    brief = hub_brief(args.project, args.gap) if args.gap else None
    if brief:
        dump(run / 'brief.json', brief)
    dump(run / 'run.json', spec)
    frontier = fill((TEMPLATES / 'frontier.md').read_text(encoding='utf-8'), {
        'title': args.target and clip(args.target, 80) or run.name,
        'target': args.target or 'State the exact theorem sought.',
        'seed': frontier_seed(brief)})
    (run / 'FRONTIER.md').write_text(frontier, encoding='utf-8')
    print(f'Initialized {run}. Edit FRONTIER.md and run.json (authorization, budget), then plan a round.')


# plan ----------------------------------------------------------------------

def next_round(run):
    numbers = round_numbers(run)
    return (numbers[-1] + 1) if numbers else 1


def plan_prospect(run, spec, count):
    library, order = lenses(spec)
    used = Counter(slot.get('lens') for _, slot in all_slots(run) if slot.get('stage') == 'prospect')
    chosen = sorted(order, key=lambda lens: (used[lens], order.index(lens)))[:count]
    _, claude_used, _ = budget_use(run)
    budget = spec['budget']
    claude_left = max(0, min(budget.get('claude_per_round', 2), budget['claude_calls'] - claude_used))
    backend = {}
    for lens in chosen:
        if library[lens]['backend'] == 'claude' and claude_left:
            backend[lens], claude_left = 'claude', claude_left - 1
    for lens in chosen:
        if lens not in backend and library[lens]['backend'] == 'any' and claude_left:
            backend[lens], claude_left = 'claude', claude_left - 1
    return [dict(id=f's{i}', stage='prospect', lens=lens, backend=backend.get(lens, 'codex'),
                 blind=bool(library[lens].get('blind')), files=[], note='')
            for i, lens in enumerate(chosen, 1)]


def assign_ladder(run, spec, slots, only=None, target_only=False):
    """Aim prospect slots at the easiest open variants; slots left over attack the target."""
    if target_only or (only is None and ladder_settings(spec).get('prefer_open', True) is False):
        return slots
    if only is not None:
        if only not in variants_by_id(run):
            sys.exit(f'Unknown variant {only}')
        if variant_state(run)[only]['status'] != 'open':
            sys.exit(f'Variant {only} is not open')
        ladder = [only] * len(slots)
    else:
        ladder = [variant['id'] for variant in open_variants(run)]
    for slot, variant_id in zip(slots, ladder):
        slot['variant'] = variant_id
    return slots


def slot_label(slot):
    label = slot.get('lens') or ','.join(slot.get('cards') or []) or slot['stage']
    return label + (f" -> variant {slot['variant']}" if slot.get('variant') else '')


def top_open_cards(run, count, stage):
    status = card_status(run)
    wanted = ('open',) if stage == 'assay' else ('survives', 'promoted')
    pool = [c for c in cards_by_id(run).values() if status[c['id']] in wanted and c['kind'] != 'argument']
    pool.sort(key=lambda c: -float(c.get('confidence') or 0))
    return [c['id'] for c in pool[:count]]


def other_backend(backend):
    return 'claude' if backend == 'codex' else 'codex'


def cmd_plan(args):
    run, spec = open_run(args.run_dir)
    number = args.round or next_round(run)
    path = round_dir(run, number) / 'slots.json'
    if path.exists() and not args.force:
        sys.exit(f'{path} exists; edit it or pass --force')
    defaults = dict(prospect=6, assay=3, dig=1, variants=1)
    defaults.update(spec.get('default_slots') or {})
    args.n = args.n or defaults[args.stage]
    if args.stage == 'prospect':
        slots = assign_ladder(run, spec, plan_prospect(run, spec, args.n), args.variant,
                              args.target_only)
    elif args.stage == 'variants':
        slots = [dict(id=f's{i}', stage='variants', backend=args.backend or 'codex', files=[], note='')
                 for i in range(1, args.n + 1)]
    else:
        known = cards_by_id(run)
        chosen = args.cards or top_open_cards(run, args.n * (args.per_slot if args.stage == 'assay' else 1),
                                              args.stage)
        missing = [c for c in chosen if c not in known]
        if missing:
            sys.exit(f'Unknown cards: {", ".join(missing)}')
        if not chosen:
            sys.exit(f'No cards available to {args.stage}')
        size = args.per_slot if args.stage == 'assay' else 1
        groups = [chosen[i:i + size] for i in range(0, len(chosen), size)]
        slots = []
        for i, group in enumerate(groups, 1):
            default = 'codex' if args.stage == 'assay' else other_backend(known[group[0]]['backend'])
            slots.append(dict(id=f's{i}', stage=args.stage, cards=group,
                              backend=args.backend or default, files=[], note=''))
    if args.backend and args.stage == 'prospect':
        for slot in slots:
            slot['backend'] = args.backend
    path.parent.mkdir(parents=True, exist_ok=True)
    dump(path, slots)
    print(f'Planned round {number} ({args.stage}) at {path}:')
    for slot in slots:
        print(f"  {slot['id']}: {slot['backend']:6} {slot_label(slot)}")
    print('Edit slots.json if needed (lens, backend, files, note, variant, solved_variants), '
          'then run it.')


# prompts -------------------------------------------------------------------

def check_hidden(spec, label, text):
    for token in spec.get('hidden') or []:
        if token and token in text:
            sys.exit(f'Hidden item {token!r} appears in {label}; refusing to build the prompt')


def section(text, heading):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r'(#+)\s+(.*)', line)
        if match and heading.lower() in match.group(2).lower():
            level = len(match.group(1))
            end = next((j for j in range(i + 1, len(lines))
                        if re.match(r'#{1,%d}\s' % level, lines[j])), len(lines))
            return '\n'.join(lines[i:end])
    raise ValueError(f'heading not found: {heading}')


def context_block(spec, files, budget=None):
    if not files:
        return '', []
    limit, used, parts, notes = spec.get('max_inline_chars', 60000), 0, ['## Named context'], []
    for item in files:
        check_hidden(spec, 'context file list', item)
        name, _, heading = item.partition('#')
        path = Path(name) if Path(name).is_absolute() else HUB / name
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
            if heading:
                text = section(text, heading)
        except (OSError, ValueError) as exc:
            sys.exit(f'Context file {item}: {exc}')
        check_hidden(spec, item, text)
        room = max(0, limit - used)
        if len(text) > room:
            notes.append(f'{item}: truncated to {room} of {len(text)} characters')
            text = text[:room] + '\n[truncated]'
        used += len(text)
        parts += ['', f'### {item}', '', text]
    if notes:
        parts += ['', 'Truncation: ' + '; '.join(notes)]
    if budget is not None:
        budget.room = max(0, budget.room - used)
    return '\n'.join(parts), notes


def survivor_sketch(card, status, judges):
    """A survivor's complete statement and sketch, labelled with what review it has had."""
    scope = (f"Prospector lead marked `{status}` by {', '.join(judges) or 'the record'}; "
             'not an independently reviewed proof')
    return '\n'.join([f"### {card['id']}: {card['title']}", '', f'Review scope: {scope}.', '',
                      f"Statement: {card['statement']}", '', f"Sketch: {card['sketch']}", '',
                      f"Weakest step: {card['weakest_step']}",
                      f"Earliest remaining gap: {card['gap_after'] or 'not stated'}"])


def feed_forward(run, spec, budget=None):
    limits, status, cards = spec.get('feed_forward') or {}, card_status(run), cards_by_id(run).values()
    reasons, judges = {}, {}
    for event in read_jsonl(run / 'dispositions.jsonl'):
        reasons[event['card']] = event.get('reason', '')
        if event['status'] in ('survives', 'promoted'):
            judges.setdefault(event['card'], []).append(event['by'])
    alive = sorted((c for c in cards if status[c['id']] in ('survives', 'promoted')),
                   key=lambda c: -float(c.get('confidence') or 0))[:limits.get('survivors', 6)]
    dead = [c for c in cards if status[c['id']] == 'killed'][-limits.get('kills', 12):]
    pending = [c for c in cards if status[c['id']] == 'open'][:limits.get('open', 10)]
    if not (alive or dead or pending):
        return ''
    lines = ['## Earlier rounds (build on survivors; do not repeat kills)']
    if alive:
        lines += ['', 'Surviving ideas:']
        lines += [f"- {c['id']} [{status[c['id']]}] {c['title']}: {clip(c['statement'], 400)} "
                  f"Weakest step: {clip(c['weakest_step'], 200)}"
                  + (f" (for variant {c['variant']})" if c.get('variant') else '') for c in alive]
    if dead:
        lines += ['', 'Killed ideas (one line each):']
        lines += [f"- {c['id']} {clip(c['title'], 120)}: {clip(reasons.get(c['id'], ''), 200)}" for c in dead]
    if pending:
        lines += ['', 'Already proposed, not yet tested (do not duplicate):']
        lines += [f"- {c['id']} {clip(c['title'], 160)}" for c in pending]
    if alive and budget is not None:
        # The one-line entries above are abridged; the full sketch goes in whole or not at all.
        lines += ['', '## Surviving ideas in full', '',
                  'Complete statements and sketches of the survivors listed above. They are leads '
                  'with the review scope shown, to be built on or corrected.']
        for card in alive:
            text = survivor_sketch(card, status[card['id']], judges.get(card['id'], []))
            if budget.take(f"sketch of survivor {card['id']}", text):
                lines += ['', text]
            else:
                lines += ['', f"### {card['id']}: sketch omitted, not shortened ({len(text)} characters "
                              'do not fit the remaining inline allowance).']
    return '\n'.join(lines)


def cards_block(run, ids):
    known, events = cards_by_id(run), read_jsonl(run / 'dispositions.jsonl')
    parts = []
    for card_id in ids:
        card = {k: v for k, v in known[card_id].items()
                if k in CARD_TEXT or k in ('id', 'kind', 'confidence') or (k == 'variant' and v)}
        parts.append(f'### {card_id}\n\n```json\n{json.dumps(card, indent=2, ensure_ascii=False)}\n```')
        history = [e for e in events if e['card'] == card_id]
        for event in history:
            parts.append(f"- {event['by']}: {event['status']}. {clip(event.get('reason', ''), 600)}"
                         + (f" Salvage: {clip(event['salvage'], 400)}" if event.get('salvage') else ''))
    return '\n\n'.join(parts)


def schema_path(stage):
    return TEMPLATES / ('variants.schema.json' if stage == 'variants' else 'card.schema.json')


def output_contract(backend, stage, max_cards):
    schema = schema_path(stage).read_text(encoding='utf-8')
    where = ('Your final message must be exactly this JSON object.' if backend != 'claude' else
             'End your answer with this JSON object in a single ```json fenced block; '
             'prose before it is optional.')
    if stage == 'variants':
        return '\n'.join([
            '## Output', '', where, f'`variants`: 1 to {max_cards} variants, easiest first.',
            'Variant fields: title; kind (special-case, lower-dimension [dimension or rank], '
            'stronger-hypothesis, weaker-conclusion, analogue); statement (exact, self-contained, with '
            'every hypothesis); relation (implies-target, implied-by-target or analogue); '
            'relation_status (proved only if relation_argument is a complete argument, otherwise '
            'conjectured); relation_argument; difficulty (integer 1 easiest to 5, relative to the '
            'target at 5); first_step (the first calculation or argument to try).',
            '', 'Schema:', '', '```json', schema.strip(), '```'])
    need = {'prospect': f'`cards`: 1 to {max_cards} claim cards; `verdicts`: [].',
            'assay': '`verdicts`: one per card above; `cards`: salvaged variants worth pursuing, or [].',
            'dig': '`cards`: one `argument` card, plus optional sub-lemmas or a counterexample; '
                   '`verdicts`: [].'}[stage]
    return '\n'.join([
        '## Output', '', where, need,
        'Card fields: title; kind; statement (exact, with hypotheses); role (how it closes or shrinks '
        'the gap); sketch (the argument or construction); weakest_step; kill_test (fastest refutation); '
        'evidence (computations with commands and output paths, or "none"); differs_from (recorded IDs '
        'or failed routes and what is new); confidence (0 to 1 that it is true and useful); cost '
        '(small/medium/large to develop); gap_after (earliest remaining gap, or "none").',
        '', 'Schema:', '', '```json', schema.strip(), '```'])


def tool_rule(backend, work):
    if backend == 'claude':
        return ('You have no tools or filesystem access in this slot. Use only the text supplied; '
                'record IDs and paths are provenance, not readable inputs. If a necessary statement '
                'or hypothesis is missing, identify it precisely in your return; do not infer its '
                'contents from an ID or title.')
    return (f'Use only the named inputs. Search the hub only if a specific missing fact blocks '
            f'this assigned move; state that fact and report what you searched for and found. '
            f'Do not repeat entry routing, campaign-history reads or broad archive reconnaissance. '
            f'You may run computations (Macaulay2 `M2`, Sage, Python). For a blocked fact use '
            f'`python3 {HUB}/tools/hub.py search|show|recall|library`. Write scripts and outputs only '
            f'in your working directory {work} and cite them in `evidence`.')


def validate_tool_free_prompt(prompt, truncation):
    """Catch unprepared packets; semantic completeness remains the conductor's job."""
    if truncation:
        sys.exit('Tool-free context was truncated; supply smaller complete sections before dispatch.')
    if '## Seed from hub brief (edit into the sections above, then delete)' in prompt:
        sys.exit('Tool-free frontier still contains a retrieval seed. Replace it with complete '
                 'statements, hypotheses and obstruction conclusions before dispatch.')
    # Deliberately narrow: do not rewrite source quotations or claim a semantic audit.
    for line in prompt.splitlines():
        directive = re.sub(r'^\s*(?:[-*]|\d+[.)])?\s*', '', line)
        if re.match(r'(?:Search|Browse|Query|Consult) (?:the )?(?:hub|library|catalog)\b',
                    directive, re.I):
            sys.exit('Tool-free prompt contains a retrieval instruction: ' + clip(line, 180)
                     + '. Supply the needed text and remove the instruction.')


def build_prompt(run, spec, number, slot):
    work = round_dir(run, number) / slot['id'] / 'work'
    frontier = (run / 'FRONTIER.md').read_text(encoding='utf-8')
    check_hidden(spec, 'FRONTIER.md', frontier)
    words = len(frontier.split())
    files = list(spec.get('context_files') or []) + list(slot.get('files') or [])
    budget = InlineBudget(spec.get('max_inline_chars', 60000))
    context, notes = context_block(spec, files, budget)
    stage, backend = slot['stage'], slot['backend']
    max_cards = spec.get('max_cards_per_slot', 3)
    if stage == 'variants':
        max_cards = ladder_settings(spec).get('max_per_slot', 6)
    values = dict(frontier=frontier.strip(), context=context, tool_rule=tool_rule(backend, work),
                  output_contract=output_contract(backend, stage, max_cards),
                  max_cards=str(max_cards))
    # Context files come first in the allowance, then solved proofs, then survivor sketches.
    assigned = slot_variant(run, slot)
    ladder = []
    if assigned:
        check_hidden(spec, f'variant {assigned}', variants_by_id(run)[assigned]['statement'])
        ladder.append(variant_assignment(run, assigned))
    if stage != 'variants':
        ladder.append(solved_block(run, spec, slot, budget))
    values['ladder'] = '\n\n'.join(part for part in ladder if part)
    if stage == 'variants':
        state = variant_state(run)
        listed = [variant_line(v, state[v['id']]['status']) for v in variants_by_id(run).values()]
        values['known_variants'] = ('## Variants already recorded (do not repeat them)\n\n'
                                    + '\n'.join(f'- {line}' for line in listed)) if listed else ''
        template = 'variants.md'
    elif stage == 'prospect':
        library, _ = lenses(spec)
        lens = library[slot['lens']]
        instruction = (lens.get('instruction_tool_free', lens['instruction'])
                       if backend == 'claude' else lens['instruction'])
        values.update(lens_name=lens['name'], lens_instruction=instruction,
                      feed_forward='' if slot.get('blind') else feed_forward(run, spec, budget))
        template = 'explorer.md'
    else:
        values['cards'] = cards_block(run, slot['cards'])
        template = 'assay.md' if stage == 'assay' else 'dig.md'
    prompt = fill((TEMPLATES / template).read_text(encoding='utf-8'), values)
    if stage in ('prospect', 'dig'):
        if backend == 'claude':
            early = ('You have no filesystem tools. Make your final structured return the first '
                     'useful mathematical result; do not wait to build a full mechanism. '
                     'No separate live checkpoint is available in this backend.')
        else:
            early = (f'As soon as the first useful derivation, decisive calculation or exact blockage '
                     f'is available, atomically write {FIRST_RETURN_FILE} in {work}. '
                     'Use about 100–250 words: the actual object/map, the reasoning or calculation, '
                     'the unproved step, and the next decision it enables. Write it once per attempt, '
                     'before polishing or extending the argument; do not write a placeholder. '
                     'Then finish only the assigned question. This note is provisional, not a proof certificate.')
        prompt += '\n## Early mathematical return\n\n' + early + '\n'
    if slot.get('note'):
        prompt += f"\n## Conductor's note for this slot\n\n{slot['note']}\n"
    if spec.get('hidden'):
        prompt += ('\n## Withheld material\n\nDo not open or search for these items: '
                   + ', '.join(spec['hidden']) + '\n')
    if backend == 'claude':
        validate_tool_free_prompt(prompt, notes)
    return prompt, dict(frontier_words=words, prompt_words=len(prompt.split()), truncation=notes,
                        inlined_words=budget.words, omitted=budget.omitted)


# backends ------------------------------------------------------------------

def timeout_limit(value):
    """Only an explicitly configured positive number imposes a deadline."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('budget.timeout_seconds must be null (no timeout) or a positive number')
    return float(value)


def atomic_dump(path, obj):
    """Readers see either complete version of the live process metadata."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=f'.{path.name}.', delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(obj, handle, indent=2)
        handle.write('\n')
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def progress_line(event):
    deadline = 'disabled' if event['timeout_seconds'] is None else f"{event['timeout_seconds']:g}s"
    return (f"{event['call']}: {event['status']} pid={event['pid']} "
            f"elapsed={event['elapsed_seconds']:.1f}s "
            f"output={event['stdout_bytes']}B+{event['stderr_bytes']}B "
            f"last-output={event['last_output_age_seconds']:.1f}s timeout={deadline}"
            + (f" first-return={event['first_return_seconds']:.1f}s"
               if event.get('first_return_seconds') is not None else ''))


def capture_first_return(cwd, sdir, started_utc, start_ns, elapsed):
    """Capture only an explicit new helper note, never model reasoning or stale scratch."""
    path = Path(cwd) / FIRST_RETURN_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime_ns < start_ns:
            return None
        with path.open('rb') as handle:
            raw = handle.read(FIRST_RETURN_MAX_BYTES + 1)
    except OSError:
        return None
    if not raw.strip() or len(raw) > FIRST_RETURN_MAX_BYTES:
        return None
    name = f'first-return-{time.time_ns()}.txt'
    (sdir / name).write_bytes(raw)
    result = dict(started_utc=started_utc, observed_utc=utc(),
                  elapsed_seconds=round(elapsed, 3), path=name, sha256=sha(raw),
                  status='provisional; usefulness requires conductor assessment')
    atomic_dump(sdir / 'first-return.json', result)
    return result


def run_process(command, prompt, cwd, env, timeout, stdout_path, stderr_path, *,
                config_path=None, label=None, heartbeat_seconds=HEARTBEAT_SECONDS):
    timeout = timeout_limit(timeout)
    heartbeat_seconds = min(60.0, max(0.01, heartbeat_seconds))
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    active_path, progress_path = stdout_path.parent / 'active.json', stdout_path.parent / 'progress.jsonl'
    start = time.monotonic()
    start_ns = time.time_ns()
    started_utc = utc()
    first_return = None
    with open(stdout_path, 'wb') as out, open(stderr_path, 'wb') as err:
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    cwd=cwd, env=env)
        except OSError as exc:
            err.write(str(exc).encode())
            return None, 'failed', time.monotonic() - start
        last_output, last_sizes = start, (0, 0)
        next_heartbeat, previous_error = start, None
        config_error, status = None, 'running'
        pending_input = prompt.encode()

        def snapshot(state, *, retain=False):
            nonlocal last_output, last_sizes, first_return
            now = time.monotonic()
            if first_return is None:
                first_return = capture_first_return(cwd, stdout_path.parent, started_utc,
                                                    start_ns, now - start)
                if first_return is not None:
                    retain = True
                    print(f"  {label or stdout_path.parent.name}: provisional mathematical return "
                          f"available at {stdout_path.parent / first_return['path']}", flush=True)
            sizes = (stdout_path.stat().st_size, stderr_path.stat().st_size)
            if sizes != last_sizes:
                last_output, last_sizes = now, sizes
            event = dict(call=label or stdout_path.parent.name, status=state,
                         pid=proc.pid, supervisor_pid=os.getpid(), started_utc=started_utc,
                         updated_utc=utc(), elapsed_seconds=round(now - start, 3),
                         stdout_bytes=sizes[0], stderr_bytes=sizes[1],
                         last_output_age_seconds=round(now - last_output, 3),
                         timeout_seconds=timeout, timeout_config_error=config_error,
                         first_return_seconds=(first_return or {}).get('elapsed_seconds'),
                         stdout_path=str(stdout_path.resolve()), stderr_path=str(stderr_path.resolve()))
            if state == 'running':
                atomic_dump(active_path, event)
            if retain:
                append(progress_path, event)
                print('  ' + progress_line(event), flush=True)

        try:
            while True:
                if config_path is not None:
                    try:
                        timeout = timeout_limit(load(config_path)['budget'].get('timeout_seconds'))
                        config_error = None
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        # A partially saved edit must not kill a job while its deadline is being removed.
                        timeout = None
                        config_error = 'Unreadable or invalid budget.timeout_seconds; deadline paused'
                    if config_error != previous_error:
                        if config_error:
                            print(f'  {label or stdout_path.parent.name}: {config_error}', flush=True)
                        previous_error = config_error
                now = time.monotonic()
                snapshot('running', retain=now >= next_heartbeat)
                if now >= next_heartbeat:
                    next_heartbeat = now + heartbeat_seconds
                if proc.poll() is not None:
                    proc.communicate()
                    status = 'completed' if proc.returncode == 0 else 'failed'
                    break
                remaining = None if timeout is None else timeout - (now - start)
                if remaining is not None and remaining <= 0:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    status = 'timeout'
                    break
                wait = min(MONITOR_SECONDS, max(0.01, next_heartbeat - now))
                if remaining is not None:
                    wait = min(wait, remaining)
                try:
                    proc.communicate(pending_input, timeout=wait)
                    status = 'completed' if proc.returncode == 0 else 'failed'
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None  # communicate resumes sending its original buffered input
            snapshot(status, retain=True)
        finally:
            if proc.stdin:
                proc.stdin.close()
            # If supervision fails while the child is alive, preserve its PID for deliberate recovery.
            if proc.poll() is not None:
                active_path.unlink(missing_ok=True)
    return proc.returncode, status, time.monotonic() - start


def find_usage(events):
    usage = None
    for event in events:
        stack = [event]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if isinstance(item.get('usage'), dict):
                    usage = item['usage']
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return usage


def jsonl_events(path):
    events = []
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def backend_settings(spec, slot):
    """Resolve explicit per-slot, per-stage, then legacy backend effort."""
    settings = dict(spec['backends'].get(slot['backend']) or {})
    stages = settings.get('effort_by_stage') or {}
    settings['effort'] = slot.get('effort', stages.get(slot.get('stage'), settings.get('effort')))
    return settings


def call_codex(spec, slot, prompt, sdir, work, *, config_path=None):
    settings = backend_settings(spec, dict(slot, backend='codex'))
    binary = shutil.which('codex')
    if not binary:
        return dict(status='failed', error='codex CLI not found'), ''
    command = [binary, 'exec', '--skip-git-repo-check', '--ephemeral', '--json',
               '-s', settings.get('sandbox') or 'workspace-write', '-C', str(work),
               '--output-schema', str(schema_path(slot.get('stage'))),
               '-o', str(sdir / 'last-message.txt')]
    if settings.get('model'):
        command += ['-m', settings['model']]
    if settings.get('effort'):
        command += ['-c', f"model_reasoning_effort={settings['effort']}"]
    command.append('-')
    env = dict(os.environ)
    env.pop('OPENAI_API_KEY', None)   # use the ChatGPT-subscription login, not API billing
    env.update(TMPDIR=str(work / 'tmp'), DOT_SAGE=str(work / '.sage'), MPLCONFIGDIR=str(work / 'tmp'),
               MATHDESK_SKIP='1')
    code, status, wall = run_process(command, prompt, work, env, spec['budget'].get('timeout_seconds'),
                                     sdir / 'codex-events.jsonl', sdir / 'stderr.txt',
                                     config_path=config_path, label=f'{sdir.parent.name}-{sdir.name} codex')
    last = sdir / 'last-message.txt'
    text = last.read_text(encoding='utf-8', errors='replace') if last.exists() else ''
    receipt = dict(status=status, exit_code=code, wall_seconds=round(wall, 1), command=command[1:],
                   model=settings.get('model'), effort=settings.get('effort'),
                   usage=find_usage(jsonl_events(sdir / 'codex-events.jsonl')))
    return receipt, text


def call_claude(spec, slot, prompt, sdir, work, *, config_path=None):
    settings = backend_settings(spec, dict(slot, backend='claude'))
    binary = shutil.which('claude') or str(Path.home() / '.local' / 'bin' / 'claude')
    if not Path(binary).exists():
        return dict(status='failed', error='claude CLI not found'), ''
    command = [binary, '--print', '--tools', '', '--strict-mcp-config', '--disable-slash-commands',
               '--no-session-persistence', '--safe-mode', '--verbose', '--output-format', 'stream-json',
               '--max-turns', '1']
    if settings.get('model'):
        command += ['--model', settings['model']]
    if settings.get('effort'):
        command += ['--effort', settings['effort']]
    code, status, wall = run_process(command, prompt, work, dict(os.environ, MATHDESK_SKIP='1'),
                                     spec['budget'].get('timeout_seconds'),
                                     sdir / 'claude-stream.jsonl', sdir / 'stderr.txt',
                                     config_path=config_path, label=f'{sdir.parent.name}-{sdir.name} claude')
    texts, receipt = [], dict(status=status, exit_code=code, wall_seconds=round(wall, 1),
                              command=command[1:], model=settings.get('model'),
                              effort=settings.get('effort'))
    result = None
    for event in jsonl_events(sdir / 'claude-stream.jsonl'):
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            receipt['actual_model'] = event.get('model')
        if event.get('type') == 'assistant':
            texts += [b.get('text', '') for b in event.get('message', {}).get('content', [])
                      if b.get('type') == 'text']
        if event.get('type') == 'result':
            result = event
    if result:
        receipt.update(usage=result.get('usage'), cost_usd=result.get('total_cost_usd'))
        if result.get('is_error'):
            receipt.update(status='failed', error=clip(result.get('result') or '', 300))
    text = (result or {}).get('result') or '\n\n'.join(texts)
    if receipt['status'] == 'completed' and result is None:
        receipt['status'] = 'failed'
    return receipt, text


def call_stub(spec, slot, prompt, sdir, work, *, config_path=None):
    """Deterministic offline backend for tests and dry rehearsals."""
    if slot.get('stub_fail'):
        return dict(status='failed', exit_code=1, wall_seconds=0.0, error='stub failure'), ''
    def card(title, kind='lemma', gap='unproved step'):
        return dict(title=title, kind=kind, statement=f'{title} holds.', role='closes the gap',
                    sketch='stub sketch', weakest_step='stub step', kill_test='stub test',
                    evidence='none', differs_from='none', confidence=0.5, cost='small', gap_after=gap)
    if slot['stage'] == 'variants':
        payload = dict(summary='stub', variants=[
            dict(title=f'stub variant {name}', kind=kind, statement=f'Every {name} widget is blue.',
                 relation=relation, relation_status='conjectured', relation_argument='stub relation',
                 difficulty=difficulty, first_step='stub step')
            for name, kind, relation, difficulty in (
                ('rank-two', 'lower-dimension', 'implied-by-target', 2),
                ('split', 'special-case', 'implied-by-target', 1),
                ('tropical', 'analogue', 'analogue', 4))])
    elif slot['stage'] == 'prospect':
        payload = dict(summary='stub', cards=[card(f"stub {slot['lens']}")], verdicts=[])
    elif slot['stage'] == 'assay':
        payload = dict(summary='stub', cards=[], verdicts=[
            dict(card=c, verdict='killed' if c in slot.get('stub_kill', []) else 'survives',
                 reason='stub reason', evidence='none', salvage='') for c in slot['cards']])
    else:
        payload = dict(summary='stub', cards=[card(f"argument for {slot['cards'][0]}", 'argument', 'none')],
                       verdicts=[])
    text = json.dumps(payload) if slot.get('stub_text') is None else slot['stub_text']
    return dict(status='completed', exit_code=0, wall_seconds=0.0), text


CALLERS = dict(codex=call_codex, claude=call_claude, stub=call_stub)


# parsing -------------------------------------------------------------------

def parse_payload(text):
    candidates = [text.strip()]
    candidates += list(reversed(re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)))
    first, last = text.find('{'), text.rfind('}')
    if 0 <= first < last:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and ('cards' in obj or 'verdicts' in obj or 'variants' in obj):
            return obj
    return None


def normal_card(raw):
    card = {key: str(raw.get(key, '') or '') for key in CARD_TEXT}
    card['kind'] = raw.get('kind') if raw.get('kind') in CARD_KINDS else 'strategy'
    try:
        card['confidence'] = min(1.0, max(0.0, float(raw.get('confidence', 0))))
    except (TypeError, ValueError):
        card['confidence'] = 0.0
    card['cost'] = raw.get('cost') if raw.get('cost') in ('small', 'medium', 'large') else 'medium'
    return card


# run -----------------------------------------------------------------------

def validate_slots(run, spec, slots):
    try:
        timeout_limit(spec['budget'].get('timeout_seconds'))
    except ValueError as exc:
        sys.exit(str(exc))
    library, _ = lenses(spec)
    known = cards_by_id(run)
    ids = [s.get('id') for s in slots]
    if len(set(ids)) != len(ids) or not all(ids):
        sys.exit('Slots need distinct ids')
    for slot in slots:
        if slot.get('stage') not in STAGES:
            sys.exit(f"{slot['id']}: stage must be one of {STAGES}")
        if slot.get('backend') not in BACKENDS:
            sys.exit(f"{slot['id']}: backend must be one of {BACKENDS}")
        if slot['backend'] != 'stub' and slot['backend'] not in spec.get('backends', {}):
            sys.exit(f"{slot['id']}: backend {slot['backend']} not configured in run.json")
        if slot['stage'] == 'prospect' and slot.get('lens') not in library:
            sys.exit(f"{slot['id']}: unknown lens {slot.get('lens')}")
        if slot['stage'] in ('assay', 'dig'):
            missing = [c for c in slot.get('cards') or [] if c not in known]
            if not slot.get('cards') or missing:
                sys.exit(f"{slot['id']}: needs existing cards (missing: {missing})")
        state = variant_state(run)
        if slot.get('variant') and slot['variant'] not in state:
            sys.exit(f"{slot['id']}: unknown variant {slot['variant']}")
        unsolved = [v for v in slot.get('solved_variants') or []
                    if state.get(v, {}).get('status') != 'solved']
        if unsolved:
            sys.exit(f"{slot['id']}: solved_variants must name solved variants (not: {unsolved})")


def execute(run, spec, number, slot, prompt):
    sdir = round_dir(run, number) / slot['id']
    work = sdir / 'work'
    (work / 'tmp').mkdir(parents=True, exist_ok=True)
    started = utc()
    receipt, text = CALLERS[slot['backend']](spec, slot, prompt, sdir, work,
                                           config_path=run / 'run.json')
    (sdir / 'response.md').write_text(text, encoding='utf-8')
    payload = parse_payload(text) if text else None
    if receipt['status'] == 'completed':
        receipt['status'] = 'ok' if payload is not None else 'parse-error'
    receipt.update(started_utc=started, ended_utc=utc(), prompt_sha256=sha(prompt),
                   response_sha256=sha(text))
    dump(sdir / 'receipt.json', receipt)
    return receipt, payload


def record(run, spec, number, slot, receipt, payload):
    call = f"r{number:02d}-{slot['id']}"
    new_cards, verdicts, new_variants = [], 0, []
    if payload:
        stamp = utc()
        assigned = slot_variant(run, slot)
        for k, raw in enumerate(payload.get('variants') or [] if slot['stage'] == 'variants' else [], 1):
            if not isinstance(raw, dict) or not str(raw.get('statement') or '').strip():
                continue
            variant = dict(id=f'{call}-v{k}', round=number, slot=slot['id'], backend=slot['backend'],
                           created_utc=stamp, **normal_variant(raw))
            append(run / 'variants.jsonl', variant)
            new_variants.append(variant['id'])
        for k, raw in enumerate(payload.get('cards') or [], 1):
            if not isinstance(raw, dict):
                continue
            card = dict(id=f'{call}-c{k}', round=number, slot=slot['id'], stage=slot['stage'],
                        backend=slot['backend'], lens=slot.get('lens'), origin=slot.get('cards') or [],
                        variant=assigned, created_utc=stamp, **normal_card(raw))
            append(run / 'cards.jsonl', card)
            new_cards.append(card['id'])
        known = cards_by_id(run)
        for raw in payload.get('verdicts') or []:
            if not isinstance(raw, dict) or raw.get('verdict') not in ('killed', 'survives', 'unclear'):
                continue
            target = str(raw.get('card', ''))
            append(run / 'dispositions.jsonl', dict(
                card=target, status=raw['verdict'], reason=str(raw.get('reason', '')),
                evidence=str(raw.get('evidence', '')), salvage=str(raw.get('salvage', '')),
                by=call, utc=stamp, unknown_card=target not in known))
            verdicts += 1
    append(run / 'ledger.jsonl', dict(
        call=call, round=number, slot=slot['id'], stage=slot['stage'], backend=slot['backend'],
        lens=slot.get('lens'), cards_in=slot.get('cards') or [], status=receipt['status'],
        error=receipt.get('error'), exit_code=receipt.get('exit_code'),
        started_utc=receipt.get('started_utc'), ended_utc=receipt.get('ended_utc'),
        wall_seconds=receipt.get('wall_seconds'), model=receipt.get('actual_model') or receipt.get('model'),
        usage=receipt.get('usage'), cost_usd=receipt.get('cost_usd'),
        prompt_sha256=receipt.get('prompt_sha256'), response_sha256=receipt.get('response_sha256'),
        cards_out=new_cards, verdicts_out=verdicts, variant=slot_variant(run, slot),
        variants_out=new_variants, budget=spec['budget']))
    return call, new_cards, verdicts, new_variants


def cmd_run(args):
    run, spec = open_run(args.run_dir)
    number = args.round or (round_numbers(run) or [0])[-1]
    path = round_dir(run, number) / 'slots.json'
    if not path.exists():
        sys.exit(f'No slots planned for round {number}')
    slots = load(path)
    validate_slots(run, spec, slots)
    used, claude_used, ledger = budget_use(run)
    done = {e['slot'] for e in ledger if e['round'] == number and e['status'] in COMPLETED}
    todo = [s for s in slots if (args.rerun or s['id'] not in done) and (not args.only or s['id'] in args.only)]
    if not todo:
        sys.exit(f'Nothing to run in round {number}')
    budget = spec['budget']
    need = len(todo)
    need_claude = sum(1 for s in todo if s['backend'] == 'claude')
    if not args.dry_run and (used + need > budget['total_calls']
                             or claude_used + need_claude > budget['claude_calls']):
        sys.exit(f"Budget exceeded: {used}+{need} of {budget['total_calls']} calls, "
                 f"{claude_used}+{need_claude} of {budget['claude_calls']} Claude calls. "
                 'Raise the budget in run.json only with user authorization, and record it '
                 'under budget_changes.')
    prompts = {}
    for slot in todo:
        prompt, info = build_prompt(run, spec, number, slot)
        sdir = round_dir(run, number) / slot['id']
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / 'prompt.md').write_text(prompt, encoding='utf-8')
        prompts[slot['id']] = prompt
        if info['frontier_words'] > FRONTIER_WORD_LIMIT and slot is todo[0]:
            print(f"warning: FRONTIER.md has {info['frontier_words']} words; keep it under "
                  f'{FRONTIER_WORD_LIMIT}', file=sys.stderr)
        for note in info['truncation'] + info['omitted']:
            print(f"warning: {slot['id']}: {note}", file=sys.stderr)
        target = spec.get('prompt_word_warning', 2500)
        # Whole solved-variant proofs and survivor sketches are expected to be long; the soft
        # target covers the rest of the prompt.
        own_words = info['prompt_words'] - info['inlined_words']
        if slot['stage'] in ('prospect', 'dig') and own_words > target:
            print(f"warning: {slot['id']}: discovery prompt has {own_words} words"
                  + (f" besides {info['inlined_words']} words of inlined proofs and sketches"
                     if info['inlined_words'] else '')
                  + f'; prefer one question and selected sections (soft target {target}; no truncation)',
                  file=sys.stderr)
    if args.dry_run:
        print(f'Dry run: wrote {len(todo)} prompts in round {number}; nothing launched or debited.')
        for slot in todo:
            print(f"  {slot['id']}: {slot['backend']:6} {slot['stage']:8} "
                  f"{slot_label(slot)} ({len(prompts[slot['id']])} chars; "
                  f"effort={backend_settings(spec, slot).get('effort') or 'inherited'})")
        return
    print(f"Round {number}: launching {len(todo)} calls, concurrency {budget['concurrency']}.", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, budget['concurrency'])) as pool:
        futures = {pool.submit(execute, run, spec, number, s, prompts[s['id']]): s for s in todo}
        for future in as_completed(futures):
            slot = futures[future]
            try:
                receipt, payload = future.result()
            except Exception as exc:  # retain the failure; other slots continue
                receipt, payload = dict(status='failed', error=f'{type(exc).__name__}: {exc}'), None
            call, new_cards, verdicts, new_variants = record(run, spec, number, slot, receipt, payload)
            print(f"  {call}: {receipt['status']:11} {slot['backend']:6} "
                  f"{receipt.get('wall_seconds', 0)}s cards={len(new_cards)} verdicts={verdicts}"
                  + (f' variants={len(new_variants)}' if slot['stage'] == 'variants' else '')
                  + (f" ({receipt['error']})" if receipt.get('error') else ''), flush=True)
    used, claude_used, _ = budget_use(run)
    print(f"Budget used: {used}/{budget['total_calls']} calls, {claude_used}/{budget['claude_calls']} Claude.")


# inspect and decide -------------------------------------------------------

def cmd_cards(args):
    run, _ = open_run(args.run_dir)
    known, status = cards_by_id(run), card_status(run)
    if args.show:
        card = known.get(args.show) or sys.exit(f'Unknown card {args.show}')
        print(json.dumps(dict(card, status=status[args.show]), indent=2, ensure_ascii=False))
        for event in read_jsonl(run / 'dispositions.jsonl'):
            if event['card'] == args.show:
                print(f"- {event['utc']} {event['by']}: {event['status']}. {event.get('reason', '')}"
                      + (f" Salvage: {event['salvage']}" if event.get('salvage') else ''))
        return
    rows = [c for c in known.values() if (not args.status or status[c['id']] in args.status)
            and (not args.round or c['round'] == args.round)]
    for card in rows:
        print(f"{card['id']:14} {status[card['id']]:9} {card['confidence']:.2f} {card['kind']:14} "
              f"{card['backend']:6} {clip(card['title'], 90)}")
    print(f'{len(rows)} cards')


def cmd_mark(args):
    run, _ = open_run(args.run_dir)
    known = cards_by_id(run)
    missing = [c for c in args.cards if c not in known]
    if missing:
        sys.exit(f'Unknown cards: {", ".join(missing)}')
    for card_id in args.cards:
        append(run / 'dispositions.jsonl', dict(card=card_id, status=args.status, reason=args.reason,
                                                 evidence='', salvage='', by='conductor', utc=utc()))
    print(f"Marked {', '.join(args.cards)} {args.status}.")


def cmd_decide(args):
    """Record the conductor's mathematical decision without dispatching or stopping work."""
    run, _ = open_run(args.run_dir)
    slots_path = round_dir(run, args.round) / 'slots.json'
    if not slots_path.exists() or args.slot not in {s['id'] for s in load(slots_path)}:
        sys.exit('Select an existing round and slot')
    sdir = round_dir(run, args.round) / args.slot
    state_path = sdir / 'active.json'
    if not state_path.exists():
        state_path = sdir / 'receipt.json'
    if not state_path.exists():
        sys.exit('No started attempt for this slot')
    state = load(state_path)
    started = state.get('started_utc')
    if not started:
        sys.exit('Attempt start time is unknown; cannot report decision latency')
    lead_path = sdir / 'first-return.json'
    lead = load(lead_path) if lead_path.exists() else None
    if lead and (lead['started_utc'] < started or
                 lead['started_utc'] > state.get('ended_utc', utc())):
        lead = None  # an earlier retry's note is not this attempt's evidence
    if lead:
        evidence = lead['path']
        evidence_hash = lead['sha256']
        first_seconds = lead['elapsed_seconds']
        if sha((sdir / evidence).read_bytes()) != evidence_hash:
            sys.exit('First-return snapshot hash mismatch')
    elif state_path.name == 'receipt.json' and (sdir / 'response.md').exists():
        raw = (sdir / 'response.md').read_bytes()
        if not raw.strip():
            sys.exit('No mathematical return available yet')
        evidence, evidence_hash = 'response.md', sha(raw)
        first_seconds = state.get('wall_seconds')
    else:
        sys.exit('No mathematical return available yet')
    for label, value in [('reason', args.reason), ('next question', args.next_question)]:
        if not value.strip():
            sys.exit(f'A substantive {label} is required')
    now = utc()
    elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(started)).total_seconds()
    event = dict(call=f'r{args.round:02d}-{args.slot}', started_utc=started, utc=now,
                 action=args.action, reason=args.reason, next_question=args.next_question,
                 evidence=str((sdir / evidence).relative_to(run)), evidence_sha256=evidence_hash,
                 first_return_seconds=first_seconds, decision_seconds=max(0, elapsed),
                 by='conductor')
    append(run / 'decisions.jsonl', event)
    print(f"Recorded {event['call']}: {args.action}, decision after {elapsed / 60:.1f} min. "
          'No process, card status, budget or frontier was changed.')


def cmd_status(args):
    run, spec = open_run(args.run_dir)
    used, claude_used, ledger = budget_use(run)
    budget, status = spec['budget'], card_status(run)
    print(f"Run {spec['name']} ({spec['project']}, gap {spec.get('gap')}): {clip(spec.get('target'), 160)}")
    print(f"Budget: {used}/{budget['total_calls']} calls, {claude_used}/{budget['claude_calls']} Claude, "
          f"concurrency {budget['concurrency']}; {len(spec.get('budget_changes') or [])} recorded changes")
    print('Calls: ' + ', '.join(f'{k}={v}' for k, v in sorted(Counter(
        f"{e['stage']}/{e['backend']}/{e['status']}" for e in ledger).items())) if ledger else 'Calls: none')
    active = []
    for path in sorted(run.glob('rounds/r*/*/active.json')):
        try:
            event = load(path)
        except (OSError, json.JSONDecodeError):
            continue  # The process may have finished between listing and reading.
        active.append(event)
    print(f'Active process records: {len(active)}')
    for event in active:
        child = 'present' if process_alive(event['pid']) else 'exited (stale record)'
        supervisor = 'present' if process_alive(event['supervisor_pid']) else 'exited'
        print('  ' + progress_line(event) + f"; child {child}, supervisor {event['supervisor_pid']} "
              f"{supervisor}; updated {event['updated_utc']}")
    for path in sorted(run.glob('rounds/r*/*/first-return.json')):
        try:
            lead = load(path)
        except (OSError, json.JSONDecodeError):
            continue
        print(f"  Provisional return {path.parent.parent.name}-{path.parent.name}: "
              f"{lead['elapsed_seconds'] / 60:.1f} min; "
              f"started {lead['started_utc']}; {path.parent / lead['path']}")
    decisions = read_jsonl(run / 'decisions.jsonl')
    first_decisions = {}
    for event in decisions:
        first_decisions.setdefault((event['call'], event['started_utc']), event)
    if first_decisions:
        median = statistics.median(e['decision_seconds'] for e in first_decisions.values())
        print(f'Conductor decisions: {len(first_decisions)} attempts; '
              f'median first decision {median / 60:.1f} min from attempt start '
              '(manually assessed; not proof certification)')
    wall = sum(e.get('wall_seconds') or 0 for e in ledger)
    cost = sum(e.get('cost_usd') or 0 for e in ledger)
    print(f'Aggregate helper wall time {wall / 60:.1f} min'
          + (f'; reported Claude cost ${cost:.2f}' if cost else ''))
    print('Cards: ' + (', '.join(f'{k}={v}' for k, v in sorted(Counter(status.values()).items())) or 'none'))
    for card in cards_by_id(run).values():
        if status[card['id']] in ('survives', 'promoted'):
            print(f"  {card['id']} [{status[card['id']]}] {clip(card['title'], 100)}"
                  + (f" | gap: {clip(card['gap_after'], 80)}" if card['kind'] == 'argument' else ''))
    ladder = variant_state(run)
    if ladder:
        print('Variants: ' + ', '.join(f'{k}={v}' for k, v in sorted(
            Counter(e['status'] for e in ladder.values()).items())))
        for variant in open_variants(run)[:3]:
            print(f"  next: {clip(variant_line(variant, 'open'), 160)}")
    words = len((run / 'FRONTIER.md').read_text(encoding='utf-8').split())
    print(f'FRONTIER.md: {words} words' + (' (over limit)' if words > FRONTIER_WORD_LIMIT else ''))
    if spec.get('outcome'):
        print(f"Outcome: {spec['outcome']['result']}: {spec['outcome'].get('note', '')}")


def cmd_close(args):
    run, spec = open_run(args.run_dir)
    spec['outcome'] = dict(result=args.outcome, note=args.note, cards=args.cards or [], utc=utc())
    dump(run / 'run.json', spec)
    print(f'Closed {spec["name"]}: {args.outcome}')


def cmd_scoreboard(args):
    roots = [HUB / 'investigations'] + sorted((HUB / 'projects').glob('*/investigations'))
    rows = []
    for root in roots:
        for path in root.rglob('run.json'):
            try:
                spec = load(path)
            except (OSError, json.JSONDecodeError):
                continue
            if spec.get('kind') != 'prospector-run':
                continue
            run = path.parent
            used, claude_used, _ = budget_use(run)
            status = Counter(card_status(run).values())
            outcome = (spec.get('outcome') or {}).get('result', 'open')
            rows.append((spec.get('created_utc', ''), spec.get('project', ''), spec.get('name', ''),
                         used, claude_used, sum(status.values()), status['survives'] + status['promoted'],
                         outcome, run.relative_to(HUB)))
    for row in sorted(rows):
        print(f'{row[0][:10]} {row[1]:26} {row[2]:24} calls={row[3]} claude={row[4]} '
              f'cards={row[5]} alive={row[6]} {row[7]:15} {row[8]}')
    print(f'{len(rows)} runs')


def cmd_doctor(args):
    for name in ('codex', 'claude', 'M2', 'sage'):
        print(f"{name:7} {shutil.which(name) or 'not found'}")
    if not args.live:
        print('Pass --live to send one short test prompt to each model backend (uses a little quota).')
        return
    spec = load(TEMPLATES / 'run.json')
    spec['budget']['timeout_seconds'] = 300
    scratch = (Path(args.scratch or os.environ.get('TMPDIR', '/tmp')) /
               f'prospector-doctor-{os.getpid()}').resolve()
    prompt = ('Connectivity check. Return the JSON object {"summary": "ok", "cards": [], "verdicts": []} '
              'and nothing else.')
    for backend in ('codex', 'claude'):
        sdir = scratch / backend
        (sdir / 'work' / 'tmp').mkdir(parents=True, exist_ok=True)
        receipt, text = CALLERS[backend](spec, dict(stage='prospect', backend=backend), prompt, sdir, sdir / 'work')
        ok = receipt['status'] == 'completed' and parse_payload(text) is not None
        print(f"{backend:7} {'ok' if ok else receipt['status']} "
              f"{receipt.get('actual_model') or receipt.get('model') or ''} {receipt.get('error') or clip(text, 120)}")
    print(f'Raw output kept in {scratch}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init', help='Create a run directory with run.json and FRONTIER.md')
    p.add_argument('run_dir')
    p.add_argument('--project', required=True)
    p.add_argument('--gap', help='Seed the frontier from hub.py brief for this Gap ID')
    p.add_argument('--target', help='Exact target statement')
    p.add_argument('--owner')
    p.add_argument('--authorization', help="Quote of the user's authorizing request")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser('plan', help='Write slots.json for the next round')
    p.add_argument('run_dir')
    p.add_argument('--stage', choices=STAGES, default='prospect')
    p.add_argument('-n', type=int, help='Slots (new-run defaults: 2 prospect, 1 assay, 1 dig, 1 variants)')
    p.add_argument('--cards', nargs='+', help='Cards to assay or dig (default: best open/surviving)')
    p.add_argument('--per-slot', type=int, default=2, help='Cards per assay slot')
    p.add_argument('--backend', choices=BACKENDS, help='Override every slot backend')
    p.add_argument('--round', type=int)
    p.add_argument('--force', action='store_true')
    p.add_argument('--variant', help='Aim every prospect slot at this open variant')
    p.add_argument('--target-only', action='store_true',
                   help='Prospect the target itself even when open variants exist')
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser('variant', help='List, add, mark or solve variants of the target (run state only)')
    p.add_argument('run_dir')
    actions = p.add_subparsers(dest='action', required=True)
    actions.add_parser('list', help='Variants, easiest first, with status')
    a = actions.add_parser('add', help='Record a variant chosen by the conductor')
    a.add_argument('--title', required=True)
    a.add_argument('--statement', required=True, help='Exact statement with every hypothesis')
    a.add_argument('--kind', choices=VARIANT_KINDS, required=True)
    a.add_argument('--relation', choices=VARIANT_RELATIONS, required=True)
    a.add_argument('--relation-status', choices=('proved', 'conjectured'), default='conjectured')
    a.add_argument('--relation-argument', default='')
    a.add_argument('--difficulty', type=int, choices=range(1, 6), required=True)
    a.add_argument('--first-step', default='')
    a = actions.add_parser('mark', help='Set a variant open, refuted or parked')
    a.add_argument('variant')
    a.add_argument('--status', choices=[s for s in VARIANT_STATUSES if s != 'solved'], required=True)
    a.add_argument('--reason', required=True)
    a = actions.add_parser('solve', help='Mark a variant solved and keep its complete proof, hashed')
    a.add_argument('variant')
    a.add_argument('--proof', required=True, help='File holding the complete proof')
    a.add_argument('--review-scope', help=f'Who checked what (default: "{DEFAULT_REVIEW_SCOPE}")')
    a.add_argument('--card', help='Card the proof came from, as provenance')
    a.add_argument('--reason', default='')
    p.set_defaults(func=cmd_variant)
    p = sub.add_parser('run', help='Launch a planned round in parallel')
    p.add_argument('run_dir')
    p.add_argument('--round', type=int, help='Default: latest planned round')
    p.add_argument('--only', nargs='+', help='Slot ids to run')
    p.add_argument('--rerun', action='store_true', help='Also rerun completed slots')
    p.add_argument('--dry-run', action='store_true', help='Write prompts only; launch nothing')
    p.set_defaults(func=cmd_run)
    p = sub.add_parser('cards', help='List cards with current status')
    p.add_argument('run_dir')
    p.add_argument('--status', nargs='+', choices=STATUSES)
    p.add_argument('--round', type=int)
    p.add_argument('--show', help='Print one card with its history')
    p.set_defaults(func=cmd_cards)
    p = sub.add_parser('mark', help='Record a conductor disposition')
    p.add_argument('run_dir')
    p.add_argument('cards', nargs='+')
    p.add_argument('--status', choices=STATUSES, required=True)
    p.add_argument('--reason', required=True)
    p.set_defaults(func=cmd_mark)
    p = sub.add_parser('decide', help='Record a mathematical decision and its latency; launches nothing')
    p.add_argument('run_dir')
    p.add_argument('--round', type=int, required=True)
    p.add_argument('--slot', required=True)
    p.add_argument('--action', choices=('develop', 'revise', 'park'), required=True)
    p.add_argument('--reason', required=True, help='Mathematical evidence that changed the decision')
    p.add_argument('--next-question', required=True, help='Next precise question, or why no work is needed')
    p.set_defaults(func=cmd_decide)
    p = sub.add_parser('status', help='Budget, calls and surviving cards')
    p.add_argument('run_dir')
    p.set_defaults(func=cmd_status)
    p = sub.add_parser('close', help='Record the run outcome for the scoreboard')
    p.add_argument('run_dir')
    p.add_argument('--outcome', choices=OUTCOMES, required=True)
    p.add_argument('--note', required=True)
    p.add_argument('--cards', nargs='*')
    p.set_defaults(func=cmd_close)
    p = sub.add_parser('scoreboard', help='Summarize all runs in the hub')
    p.set_defaults(func=cmd_scoreboard)
    p = sub.add_parser('doctor', help='Check CLIs; --live sends a one-line test to each backend')
    p.add_argument('--live', action='store_true')
    p.add_argument('--scratch')
    p.set_defaults(func=cmd_doctor)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
