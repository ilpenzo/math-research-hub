"""Prospector orchestration and subprocess monitoring controls; no model calls."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prospector as pr


def call(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        pr.main(list(argv))
    return out.getvalue()


def call_err(*argv):
    """Run a command and return what it wrote to stderr (warnings)."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        pr.main(list(argv))
    return err.getvalue()


class RunCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.run = self.tmp / 'run'
        call('init', str(self.run), '--project', 'fixture', '--target', 'Every widget is blue.')

    def slots(self, number):
        return json.loads((self.run / 'rounds' / f'r{number:02d}' / 'slots.json').read_text())

    def write_slots(self, number, slots):
        path = self.run / 'rounds' / f'r{number:02d}' / 'slots.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(slots))

    def spec(self, **budget):
        path = self.run / 'run.json'
        spec = json.loads(path.read_text())
        spec['budget'].update(budget)
        path.write_text(json.dumps(spec))

    def edit_spec(self, **changes):
        spec = pr.load(self.run / 'run.json')
        spec.update(changes)
        pr.dump(self.run / 'run.json', spec)
        return spec

    def prompt(self, number, slot='s1'):
        return (self.run / 'rounds' / f'r{number:02d}' / slot / 'prompt.md').read_text()


class ProspectorTests(RunCase):
    def test_init_writes_frontier_and_spec(self):
        spec = json.loads((self.run / 'run.json').read_text())
        self.assertEqual(spec['kind'], 'prospector-run')
        self.assertIsNone(spec['budget']['timeout_seconds'])
        self.assertIn('Every widget is blue.', (self.run / 'FRONTIER.md').read_text())
        with self.assertRaises(SystemExit):
            call('init', str(self.run), '--project', 'fixture')

    def test_short_discovery_defaults_preserve_legacy_allocations(self):
        spec = pr.load(self.run / 'run.json')
        self.assertEqual(spec['max_cards_per_slot'], 1)
        self.assertIsNone(spec['budget']['timeout_seconds'])
        call('plan', str(self.run), '--backend', 'stub')
        self.assertEqual(len(self.slots(1)), 2)
        spec.pop('default_slots')
        pr.dump(self.run / 'run.json', spec)
        call('plan', str(self.run), '--backend', 'stub')
        self.assertEqual(len(self.slots(2)), 6, 'old run allocation is not silently rewritten')

    def test_backend_specific_early_returns_and_unassayed_dig(self):
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        slot = self.slots(1)[0]
        spec = pr.load(self.run / 'run.json')
        codex, _ = pr.build_prompt(self.run, spec, 1, dict(slot, backend='codex'))
        claude, _ = pr.build_prompt(self.run, spec, 1, dict(slot, backend='claude'))
        self.assertIn('atomically write FIRST_RETURN.txt', codex)
        self.assertIn('No separate live checkpoint is available', claude)
        self.assertNotIn('atomically write FIRST_RETURN.txt', claude)
        call('run', str(self.run))
        card = pr.read_jsonl(self.run / 'cards.jsonl')[0]['id']
        call('plan', str(self.run), '--stage', 'dig', '--backend', 'stub', '--cards', card)
        call('run', str(self.run), '--dry-run')
        prompt = (self.run / 'rounds/r02/s1/prompt.md').read_text()
        self.assertIn('unassayed discovery lead', prompt)
        self.assertNotIn('survived an adversarial assay', prompt)

    def test_plan_diversifies_lenses_and_caps_claude(self):
        call('plan', str(self.run), '-n', '6')
        slots = self.slots(1)
        self.assertEqual(len({s['lens'] for s in slots}), 6)
        self.assertLessEqual(sum(s['backend'] == 'claude' for s in slots), 2)
        self.assertTrue(any(s['backend'] == 'codex' for s in slots))

    def test_effective_effort_reaches_both_commands_and_receipts(self):
        spec = pr.load(self.run / 'run.json')
        sdir = self.tmp / 'backend'; sdir.mkdir()
        work = sdir / 'work'; work.mkdir()

        def fake_process(command, prompt, cwd, env, timeout, stdout, stderr, **kwargs):
            stdout.write_text('')
            stderr.write_text('')
            (sdir / 'last-message.txt').write_text('{"summary":"ok","cards":[],"verdicts":[]}')
            return 0, 'completed', .1

        with patch.object(pr.shutil, 'which', return_value=sys.executable), \
                patch.object(pr, 'run_process', side_effect=fake_process):
            for backend in ('codex', 'claude'):
                caller = getattr(pr, 'call_' + backend)
                for stage, expected in (('prospect', 'high'), ('assay', 'high'), ('dig', 'xhigh')):
                    with self.subTest(backend=backend, stage=stage):
                        slot = dict(backend=backend, stage=stage)
                        receipt, _ = caller(spec, slot, 'fixture', sdir, work)
                        self.assertEqual(receipt['effort'], expected)
                        flag = 'model_reasoning_effort=' + expected if backend == 'codex' else expected
                        self.assertIn(flag, receipt['command'])
                        receipt, _ = caller(spec, dict(slot, effort='high'), 'fixture', sdir, work)
                        self.assertEqual(receipt['effort'], 'high', 'explicit slot overrides stage')
                # Old run settings keep their recorded inheritance; no global config writes.
                spec['backends'][backend].pop('effort_by_stage')
                spec['backends'][backend]['effort'] = None
                receipt, _ = caller(spec, dict(backend=backend, stage='dig'), 'fixture', sdir, work)
                self.assertIsNone(receipt['effort'])
                self.assertNotIn('--effort', receipt['command'])
                self.assertFalse(any('model_reasoning_effort=' in x for x in receipt['command']))

    def test_tool_free_packet_rejects_missing_context_and_retrieval_instructions(self):
        spec = pr.load(self.run / 'run.json')
        slot = dict(id='s1', stage='prospect', backend='claude', lens='weakest-sufficient')
        context = self.tmp / 'premise.txt'; context.write_text('A complete hypothesis and conclusion.')
        with self.assertRaisesRegex(SystemExit, 'truncated'):
            pr.build_prompt(self.run, dict(spec, max_inline_chars=8), 1,
                            dict(slot, files=[str(context)]))
        with self.assertRaisesRegex(SystemExit, 'retrieval instruction'):
            pr.build_prompt(self.run, spec, 1, dict(slot, note='Search the hub first for R001.'))
        frontier = self.run / 'FRONTIER.md'
        frontier.write_text(pr.frontier_seed({'gap': {'id': 'K001', 'question': 'Fixture'}}))
        with self.assertRaisesRegex(SystemExit, 'retrieval seed'):
            pr.build_prompt(self.run, spec, 1, slot)

    def test_lenses_use_backend_appropriate_inputs_and_dry_run_reports_effort(self):
        spec = pr.load(self.run / 'run.json')
        library, _ = pr.lenses(spec)
        for lens in library:
            with self.subTest(lens=lens):
                slot = dict(id='s1', stage='prospect', backend='claude', lens=lens)
                prompt, _ = pr.build_prompt(self.run, spec, 1, slot)
                self.assertIn('record IDs and paths are provenance', prompt)
                self.assertNotIn('hub.py', prompt)
                self.assertNotIn('Search the hub', prompt)
        slot['backend'] = 'codex'
        prompt, _ = pr.build_prompt(self.run, spec, 1, slot)
        self.assertIn('Search the hub only if a specific missing fact blocks', prompt)
        self.assertIn('report what you searched for', prompt)
        call('plan', str(self.run), '-n', '1', '--backend', 'codex')
        self.assertIn('effort=high', call('run', str(self.run), '--dry-run'))

    def test_full_cycle_with_feed_forward(self):
        call('plan', str(self.run), '-n', '3', '--backend', 'stub')
        first = {s['lens'] for s in self.slots(1)}
        out = call('run', str(self.run))
        self.assertIn('Budget used: 3/24', out)
        cards = pr.read_jsonl(self.run / 'cards.jsonl')
        self.assertEqual(len(cards), 3)
        doomed = cards[0]['id']
        call('plan', str(self.run), '--stage', 'assay', '--backend', 'stub', '--cards',
             *[c['id'] for c in cards])
        assay = self.slots(2)
        assay[0]['stub_kill'] = [doomed]
        self.write_slots(2, assay)
        call('run', str(self.run))
        status = pr.card_status(self.run)
        self.assertEqual(status[doomed], 'killed')
        self.assertEqual(sum(v == 'survives' for v in status.values()), 2)
        call('plan', str(self.run), '-n', '3', '--backend', 'stub')
        third = self.slots(3)
        self.assertFalse(first & {s['lens'] for s in third}, 'unused lenses come first')
        call('run', str(self.run), '--dry-run')
        prompt = (self.run / 'rounds' / 'r03' / 's1' / 'prompt.md').read_text()
        self.assertIn('Surviving ideas', prompt)
        self.assertIn(doomed, prompt)
        self.assertEqual(len(pr.read_jsonl(self.run / 'ledger.jsonl')), 5, 'dry run debits nothing')
        survivor = next(c for c, v in status.items() if v == 'survives')
        call('plan', str(self.run), '--stage', 'dig', '--backend', 'stub', '--cards', survivor, '--force',
             '--round', '3')
        call('run', str(self.run))
        argument = pr.read_jsonl(self.run / 'cards.jsonl')[-1]
        self.assertEqual(argument['kind'], 'argument')
        self.assertEqual(argument['origin'], [survivor])
        call('mark', str(self.run), argument['id'], '--status', 'promoted', '--reason', 'complete')
        out = call('status', str(self.run))
        self.assertIn('[promoted]', out)
        call('close', str(self.run), '--outcome', 'partial', '--note', 'fixture')
        self.assertEqual(json.loads((self.run / 'run.json').read_text())['outcome']['result'], 'partial')

    def test_budget_refusal_and_failed_calls_not_counted(self):
        self.spec(total_calls=2)
        call('plan', str(self.run), '-n', '3', '--backend', 'stub')
        with self.assertRaises(SystemExit):
            call('run', str(self.run))
        slots = self.slots(1)[:2]
        slots[0]['stub_fail'] = True
        self.write_slots(1, slots)
        out = call('run', str(self.run))
        self.assertIn('failed', out)
        self.assertEqual(pr.budget_use(self.run)[0], 1)
        slots[0].pop('stub_fail')
        self.write_slots(1, slots)
        call('run', str(self.run))
        self.assertEqual(pr.budget_use(self.run)[0], 2)
        with self.assertRaises(SystemExit):
            call('run', str(self.run))

    def test_parse_payload_forms(self):
        obj = {'summary': 's', 'cards': [], 'verdicts': []}
        self.assertEqual(pr.parse_payload(json.dumps(obj)), obj)
        fenced = 'Discussion first.\n```json\n' + json.dumps(obj, indent=2) + '\n```\n'
        self.assertEqual(pr.parse_payload(fenced), obj)
        self.assertIsNone(pr.parse_payload('no structured answer {here}'))
        card = pr.normal_card({'title': 't', 'kind': 'bogus', 'confidence': 7, 'cost': 'huge'})
        self.assertEqual((card['kind'], card['confidence'], card['cost']), ('strategy', 1.0, 'medium'))

    def test_parse_error_is_retained(self):
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        slots = self.slots(1)
        slots[0]['stub_text'] = 'I could not decide.'
        self.write_slots(1, slots)
        out = call('run', str(self.run))
        self.assertIn('parse-error', out)
        self.assertEqual((self.run / 'rounds' / 'r01' / 's1' / 'response.md').read_text(),
                         'I could not decide.')

    def test_hidden_material_refused(self):
        spec = json.loads((self.run / 'run.json').read_text())
        spec['hidden'] = ['SECRET-R001']
        (self.run / 'run.json').write_text(json.dumps(spec))
        with open(self.run / 'FRONTIER.md', 'a') as handle:
            handle.write('\nSee SECRET-R001.\n')
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        with self.assertRaises(SystemExit):
            call('run', str(self.run), '--dry-run')

    def test_context_section_extraction(self):
        note = self.tmp / 'NOTE.md'
        note.write_text('# Title\n\nintro\n\n## Keep\n\nkept text\n\n### Sub\n\nalso kept\n\n## Drop\n\nnot this\n')
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        slots = self.slots(1)
        slots[0]['files'] = [f'{note}#Keep']
        self.write_slots(1, slots)
        call('run', str(self.run), '--dry-run')
        prompt = (self.run / 'rounds' / 'r01' / 's1' / 'prompt.md').read_text()
        self.assertIn('also kept', prompt)
        self.assertNotIn('not this', prompt)

    def process(self, script, timeout=None, *, config_path=None):
        sdir = self.run / 'rounds' / 'r01' / 's1'
        sdir.mkdir(parents=True, exist_ok=True)
        return pr.run_process([sys.executable, '-c', script], 'private prompt', sdir, dict(os.environ),
                              timeout, sdir / 'events.jsonl', sdir / 'stderr.txt',
                              config_path=config_path, label='r01-s1 fixture', heartbeat_seconds=0.05)

    def test_no_timeout_subprocess_retains_progress_and_output(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, status, wall = self.process(
                'import sys,time; print(sys.stdin.read(), flush=True); time.sleep(.2); print("done")')
        self.assertEqual((code, status), (0, 'completed'))
        self.assertGreaterEqual(wall, .2)
        sdir = self.run / 'rounds' / 'r01' / 's1'
        self.assertEqual((sdir / 'events.jsonl').read_text(), 'private prompt\ndone\n')
        progress = pr.read_jsonl(sdir / 'progress.jsonl')
        self.assertGreaterEqual(sum(e['status'] == 'running' for e in progress), 2)
        self.assertEqual(progress[-1]['status'], 'completed')
        self.assertGreater(progress[-1]['stdout_bytes'], 0)
        self.assertTrue(all(e['timeout_seconds'] is None for e in progress))
        self.assertFalse((sdir / 'active.json').exists())
        self.assertIn('pid=', out.getvalue())
        self.assertIn('last-output=', out.getvalue())
        self.assertNotIn('private prompt', out.getvalue())

    def test_early_mathematical_note_is_captured_before_process_finishes(self):
        sdir = self.run / 'rounds/r01/s1'
        script = ('from pathlib import Path; import time; '
                  'Path("note.tmp").write_text("Explicit comparison map; remaining twist unknown."); '
                  'Path("note.tmp").replace("FIRST_RETURN.txt"); '
                  'time.sleep(.25); print("finished")')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code, status, wall = self.process(script)
        self.assertEqual((code, status), (0, 'completed'))
        lead = pr.load(sdir / 'first-return.json')
        self.assertLess(lead['elapsed_seconds'], wall)
        self.assertEqual(pr.sha((sdir / lead['path']).read_bytes()), lead['sha256'])
        progress = pr.read_jsonl(sdir / 'progress.jsonl')
        self.assertTrue(any(e['status'] == 'running' and e.get('first_return_seconds') is not None
                            for e in progress))
        self.assertIn('provisional mathematical return available', out.getvalue())
        self.assertNotIn('remaining twist unknown', out.getvalue(), 'heartbeat shows metadata only')
        self.assertTrue(all(e['timeout_seconds'] is None for e in progress))

    def test_stale_empty_symlinked_and_oversized_notes_are_not_captured(self):
        cwd, sdir = self.tmp / 'work', self.tmp / 'slot'
        cwd.mkdir(); sdir.mkdir()
        note = cwd / pr.FIRST_RETURN_FILE
        note.write_text('Previous attempt')
        os.utime(note, ns=(1, 1))
        start = time.time_ns()
        self.assertIsNone(pr.capture_first_return(cwd, sdir, pr.utc(), start, .1))
        note.write_text('  ')
        self.assertIsNone(pr.capture_first_return(cwd, sdir, pr.utc(), start, .2))
        note.write_bytes(b'x' * (pr.FIRST_RETURN_MAX_BYTES + 1))
        self.assertIsNone(pr.capture_first_return(cwd, sdir, pr.utc(), start, .3))
        note.unlink()
        outside = self.tmp / 'unrelated.txt'; outside.write_text('Do not capture')
        note.symlink_to(outside)
        self.assertIsNone(pr.capture_first_return(cwd, sdir, pr.utc(), start, .4))
        self.assertFalse((sdir / 'first-return.json').exists())

    def test_decision_records_evidence_and_latency_without_launch_or_budget_change(self):
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        sdir = self.run / 'rounds/r01/s1'; sdir.mkdir()
        pr.dump(sdir / 'active.json', dict(started_utc=pr.utc()))
        common = ('decide', str(self.run), '--round', '1', '--slot', 's1',
                  '--action', 'develop', '--reason', 'Explicit map found',
                  '--next-question', 'Determine its kernel')
        with self.assertRaisesRegex(SystemExit, 'No mathematical return'):
            call(*common)
        work = sdir / 'work'; work.mkdir()
        (work / pr.FIRST_RETURN_FILE).write_text('An explicit map; its kernel is unresolved.')
        lead = pr.capture_first_return(work, sdir, pr.utc(), 0, 3)
        before = pr.budget_use(self.run)[0]
        with patch.dict(pr.CALLERS, stub=lambda *a, **k: self.fail('decision launched a worker')):
            call(*common)
        event = pr.read_jsonl(self.run / 'decisions.jsonl')[0]
        self.assertEqual(event['evidence_sha256'], lead['sha256'])
        self.assertEqual(event['first_return_seconds'], 3)
        self.assertEqual(pr.budget_use(self.run)[0], before)
        self.assertFalse((self.run / 'dispositions.jsonl').exists())
        (sdir / lead['path']).write_text('tampered')
        with self.assertRaisesRegex(SystemExit, 'hash mismatch'):
            call(*common)

    def test_decision_uses_final_return_when_no_early_note_exists(self):
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        call('run', str(self.run))
        common = ('decide', str(self.run), '--round', '1', '--slot', 's1',
                  '--action', 'revise', '--reason', 'The hypothesis needs repair',
                  '--next-question', 'Test the revised hypothesis')
        call(*common); call(*common)
        events = pr.read_jsonl(self.run / 'decisions.jsonl')
        self.assertTrue(events[0]['evidence'].endswith('response.md'))
        status = call('status', str(self.run))
        self.assertIn('Conductor decisions: 1 attempts', status)
        self.assertIn('Aggregate helper wall time', status)
        self.assertEqual(pr.budget_use(self.run)[0], 1)

    def test_explicit_timeout_terminates_and_cleans_active_metadata(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code, status, wall = self.process('import time; print("partial", flush=True); time.sleep(5)', .2)
        self.assertEqual(status, 'timeout')
        self.assertNotEqual(code, 0)
        self.assertLess(wall, 3)
        sdir = self.run / 'rounds' / 'r01' / 's1'
        progress = pr.read_jsonl(sdir / 'progress.jsonl')
        self.assertEqual(progress[-1]['status'], 'timeout')
        self.assertEqual(progress[-1]['timeout_seconds'], .2)
        self.assertFalse(pr.process_alive(progress[-1]['pid']))
        self.assertFalse((sdir / 'active.json').exists())
        self.assertEqual((sdir / 'events.jsonl').read_text(), 'partial\n')

    def test_active_status_and_live_deadline_removal(self):
        self.spec(timeout_seconds=.6)
        sdir = self.run / 'rounds' / 'r01' / 's1'
        active_path = sdir / 'active.json'
        out = io.StringIO()
        with contextlib.redirect_stdout(out), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process, 'import time; time.sleep(1); print("finished")', .6,
                                 config_path=self.run / 'run.json')
            wait_until = time.monotonic() + 2
            while not active_path.exists() and not future.done() and time.monotonic() < wait_until:
                time.sleep(.01)
            self.assertTrue(active_path.exists(), 'subprocess published its PID')
            active = pr.load(active_path)
            self.assertEqual(active['supervisor_pid'], os.getpid())
            self.assertTrue(pr.process_alive(active['pid']))
            status = call('status', str(self.run))
            self.assertIn('Active process records: 1', status)
            self.assertIn(f"pid={active['pid']}", status)
            self.assertEqual(pr.budget_use(self.run)[0], 0, 'live progress does not debit completed calls')
            self.spec(timeout_seconds=None)
            code, status, wall = future.result(timeout=4)
        self.assertEqual((code, status), (0, 'completed'))
        self.assertGreater(wall, .6, 'removing the deadline allowed the child to finish')
        self.assertIsNone(pr.read_jsonl(sdir / 'progress.jsonl')[-1]['timeout_seconds'])
        self.assertFalse(active_path.exists())
        self.assertIn('Active process records: 0', call('status', str(self.run)))

    def test_missing_timeout_is_disabled_and_invalid_edit_pauses_deadline(self):
        config = self.tmp / 'monitor.json'
        for content in ('{"budget": {}}', '{unfinished edit', '{"budget": null}'):
            with self.subTest(content=content):
                config.write_text(content)
                with contextlib.redirect_stdout(io.StringIO()):
                    code, status, wall = self.process('import time; time.sleep(.15)', .05,
                                                     config_path=config)
                self.assertEqual((code, status), (0, 'completed'))
                self.assertGreater(wall, .05)
        event = pr.read_jsonl(self.run / 'rounds' / 'r01' / 's1' / 'progress.jsonl')[-1]
        self.assertIsNotNone(event['timeout_config_error'])

    def test_timeout_validation_and_bounded_doctor_with_relative_scratch(self):
        for value in (0, -1, True, '1800', float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pr.timeout_limit(value)
        self.spec(timeout_seconds=0)
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        with self.assertRaisesRegex(SystemExit, 'positive number'):
            call('run', str(self.run))
        observed = []

        def doctor_backend(spec, slot, prompt, sdir, work):
            observed.append((spec['budget']['timeout_seconds'], sdir.is_absolute(), work.is_absolute()))
            return {'status': 'completed'}, '{"summary":"ok","cards":[],"verdicts":[]}'

        with contextlib.chdir(self.tmp), patch.dict(pr.CALLERS, codex=doctor_backend, claude=doctor_backend):
            call('doctor', '--live', '--scratch', 'relative-scratch')
        self.assertEqual(observed, [(300, True, True), (300, True, True)])


class VariantLadderTests(RunCase):
    """Item 1: variants of the gap, solved proofs and survivor sketches inlined whole."""

    def ladder(self):
        call('plan', str(self.run), '--stage', 'variants', '--backend', 'stub')
        call('run', str(self.run))
        return {v['title'].split()[-1]: v['id'] for v in pr.read_jsonl(self.run / 'variants.jsonl')}

    def solve(self, variant, text, *extra):
        proof = self.tmp / f'{variant}.md'
        proof.write_text(text)
        return call('variant', str(self.run), 'solve', variant, '--proof', str(proof), *extra)

    def test_variants_stage_records_exact_variants_and_debits_one_call(self):
        ids = self.ladder()
        variants = pr.read_jsonl(self.run / 'variants.jsonl')
        self.assertEqual(len(variants), 3)
        self.assertEqual({v['relation'] for v in variants}, {'implied-by-target', 'analogue'})
        self.assertTrue(all(v['relation_status'] == 'conjectured' and 1 <= v['difficulty'] <= 5
                            for v in variants))
        self.assertEqual(pr.budget_use(self.run)[0], 1, 'a variants slot is one ordinary call')
        entry = pr.read_jsonl(self.run / 'ledger.jsonl')[0]
        self.assertEqual((entry['stage'], len(entry['variants_out'])), ('variants', 3))
        prompt = self.prompt(1)
        self.assertIn('special case', prompt)
        self.assertIn('"relation_status"', prompt, 'the variants schema is the output contract')
        self.assertNotIn('"verdicts"', prompt)
        listing = call('variant', str(self.run), 'list')
        self.assertLess(listing.index(ids['split']), listing.index(ids['rank-two']), 'easiest first')
        self.assertIn('3 variants: open=3', listing)
        self.assertIn('Variants: open=3', call('status', str(self.run)))
        # A second variants slot is told what already exists.
        call('plan', str(self.run), '--stage', 'variants', '--backend', 'stub')
        call('run', str(self.run), '--dry-run')
        self.assertIn('Variants already recorded', self.prompt(2))
        self.assertIn(ids['split'], self.prompt(2))

    def test_codex_receives_the_schema_of_its_stage(self):
        spec = pr.load(self.run / 'run.json')
        sdir = self.tmp / 'backend'; sdir.mkdir()

        def fake_process(command, prompt, cwd, env, timeout, stdout, stderr, **kwargs):
            stdout.write_text(''); stderr.write_text('')
            return 0, 'completed', .1

        with patch.object(pr.shutil, 'which', return_value=sys.executable), \
                patch.object(pr, 'run_process', side_effect=fake_process):
            for stage, name in (('variants', 'variants.schema.json'), ('prospect', 'card.schema.json'),
                                ('dig', 'card.schema.json')):
                receipt, _ = pr.call_codex(spec, dict(backend='codex', stage=stage), 'x', sdir, sdir)
                schema = receipt['command'][receipt['command'].index('--output-schema') + 1]
                self.assertEqual(Path(schema).name, name)
        strict = pr.load(pr.TEMPLATES / 'variants.schema.json')
        item = strict['properties']['variants']['items']
        self.assertEqual(sorted(item['required']), sorted(item['properties']), 'strict: every field required')
        self.assertFalse(item['additionalProperties'])

    def test_plan_prefers_easiest_open_variants_before_the_target(self):
        ids = self.ladder()
        call('plan', str(self.run), '-n', '4', '--backend', 'stub')
        self.assertEqual([s.get('variant') for s in self.slots(2)],
                         [ids['split'], ids['rank-two'], ids['tropical'], None])
        call('variant', str(self.run), 'mark', ids['split'], '--status', 'refuted', '--reason', 'red widget')
        call('plan', str(self.run), '-n', '2', '--backend', 'stub', '--force', '--round', '2')
        self.assertEqual([s.get('variant') for s in self.slots(2)], [ids['rank-two'], ids['tropical']])
        call('plan', str(self.run), '-n', '2', '--backend', 'stub', '--force', '--round', '2',
             '--target-only')
        self.assertEqual([s.get('variant') for s in self.slots(2)], [None, None])
        call('plan', str(self.run), '-n', '2', '--backend', 'stub', '--force', '--round', '2',
             '--variant', ids['tropical'])
        self.assertEqual([s.get('variant') for s in self.slots(2)], [ids['tropical']] * 2)
        with self.assertRaisesRegex(SystemExit, 'not open'):
            call('plan', str(self.run), '--force', '--round', '2', '--variant', ids['split'])
        self.edit_spec(variant_ladder={'prefer_open': False})
        call('plan', str(self.run), '-n', '2', '--backend', 'stub', '--force', '--round', '2')
        self.assertEqual([s.get('variant') for s in self.slots(2)], [None, None])

    def test_cards_inherit_the_variant_and_later_stages_show_its_statement(self):
        ids = self.ladder()
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        call('run', str(self.run))
        self.assertIn('## Assigned variant', self.prompt(2))
        self.assertIn('Every split widget is blue.', self.prompt(2))
        card = pr.read_jsonl(self.run / 'cards.jsonl')[0]
        self.assertEqual(card['variant'], ids['split'])
        call('plan', str(self.run), '--stage', 'dig', '--backend', 'stub', '--cards', card['id'])
        call('run', str(self.run))
        self.assertIn('Every split widget is blue.', self.prompt(3))
        argument = pr.read_jsonl(self.run / 'cards.jsonl')[-1]
        self.assertEqual(argument['variant'], ids['split'], 'the argument is for the variant, not the target')

    def test_solved_variant_keeps_hashed_proof_and_review_scope(self):
        ids = self.ladder()
        text = 'Lemma. Every split widget is blue.\n\nProof. Split widgets are painted blue. QED\n'
        out = self.solve(ids['split'], text)
        self.assertIn('review scope: unreviewed lead', out)
        self.assertIn('no catalog record or status was changed', out)
        event = pr.variant_state(self.run)[ids['split']]
        kept = self.run / event['proof']
        self.assertEqual(kept.read_text(), text)
        self.assertEqual(pr.sha(kept.read_bytes()), event['proof_sha256'])
        self.assertEqual(event['review_scope'], 'unreviewed lead')
        self.assertIn('proof variants/', call('variant', str(self.run), 'list'))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            call('variant', str(self.run), 'mark', ids['rank-two'], '--status', 'solved', '--reason', 'x')
        empty = self.tmp / 'empty.md'; empty.write_text(' ')
        with self.assertRaisesRegex(SystemExit, 'complete proof'):
            call('variant', str(self.run), 'solve', ids['rank-two'], '--proof', str(empty))
        self.solve(ids['rank-two'], 'Proof by rank.', '--review-scope',
                   'fresh proof-checker, frozen text, whole argument')
        call('plan', str(self.run), '-n', '2', '--backend', 'stub')
        self.assertEqual([s.get('variant') for s in self.slots(2)], [ids['tropical'], None],
                         'solved variants are no longer planned')
        call('run', str(self.run), '--dry-run')
        prompt = self.prompt(2)
        self.assertIn(text.strip(), prompt)
        self.assertIn('Review scope: unreviewed lead.', prompt)
        self.assertIn('Review scope: fresh proof-checker, frozen text, whole argument.', prompt)
        kept.write_text(text + 'An unrecorded edit.')
        with self.assertRaisesRegex(SystemExit, 'recorded hash'):
            call('run', str(self.run), '--dry-run')

    def test_default_inlines_the_two_most_recently_solved_and_slots_can_choose(self):
        ids = self.ladder()
        for name in ('split', 'rank-two', 'tropical'):
            self.solve(ids[name], f'PROOF-OF-{name.upper()} complete.')
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        call('run', str(self.run), '--dry-run')
        prompt = self.prompt(2)
        self.assertNotIn('PROOF-OF-SPLIT', prompt)
        self.assertIn('PROOF-OF-RANK-TWO', prompt)
        self.assertIn('PROOF-OF-TROPICAL', prompt)
        slots = self.slots(2)
        slots[0]['solved_variants'] = [ids['split']]
        self.write_slots(2, slots)
        call('run', str(self.run), '--dry-run')
        self.assertIn('PROOF-OF-SPLIT', self.prompt(2))
        self.assertNotIn('PROOF-OF-TROPICAL', self.prompt(2))
        slots[0]['solved_variants'] = []
        self.write_slots(2, slots)
        call('run', str(self.run), '--dry-run')
        self.assertNotIn('PROOF-OF-', self.prompt(2))
        slots[0]['solved_variants'] = ['no-such-variant']
        self.write_slots(2, slots)
        with self.assertRaisesRegex(SystemExit, 'must name solved variants'):
            call('run', str(self.run), '--dry-run')

    def test_proof_goes_in_whole_or_is_omitted_and_said_so(self):
        ids = self.ladder()
        proof = 'HEAD-MARK ' + 'step ' * 400 + 'TAIL-MARK'
        self.solve(ids['split'], proof)
        spec = self.edit_spec(max_inline_chars=len(proof) - 1)
        slot = dict(id='s1', stage='prospect', backend='claude', lens='weakest-sufficient')
        prompt, info = pr.build_prompt(self.run, spec, 2, slot)
        self.assertNotIn('HEAD-MARK', prompt, 'never a clipped proof')
        self.assertNotIn('TAIL-MARK', prompt)
        self.assertIn('Proof omitted, not shortened', prompt)
        self.assertIn('Every split widget is blue.', prompt, 'the solved statement is still given')
        self.assertEqual(len(info['omitted']), 1)
        self.assertEqual(info['truncation'], [], 'an omitted proof is not a truncated tool-free packet')
        prompt, info = pr.build_prompt(self.run, dict(spec, max_inline_chars=len(proof)), 2, slot)
        self.assertIn('HEAD-MARK', prompt)
        self.assertIn('TAIL-MARK', prompt)
        self.assertEqual(info['omitted'], [])
        # Named context is counted first, so a large context file leaves no room for the proof.
        context = self.tmp / 'context.txt'; context.write_text('c' * 300)
        prompt, info = pr.build_prompt(self.run, dict(spec, max_inline_chars=len(proof) + 200), 2,
                                       dict(slot, files=[str(context)]))
        self.assertNotIn('HEAD-MARK', prompt)
        self.assertEqual(len(info['omitted']), 1)

    def test_size_warning_expects_inlined_proofs(self):
        ids = self.ladder()
        self.solve(ids['split'], 'word ' * 3000)
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        spec = pr.load(self.run / 'run.json')
        _, info = pr.build_prompt(self.run, spec, 2, self.slots(2)[0])
        self.assertGreaterEqual(info['inlined_words'], 3000)
        self.assertGreater(info['prompt_words'], spec['prompt_word_warning'])
        self.assertNotIn('discovery prompt has', call_err('run', str(self.run), '--dry-run'))
        self.edit_spec(prompt_word_warning=100)
        warning = call_err('run', str(self.run), '--dry-run')
        self.assertIn('besides', warning)
        self.assertIn('words of inlined proofs and sketches', warning)
        self.edit_spec(prompt_word_warning=2500, max_inline_chars=50)
        self.assertIn('omitted whole', call_err('run', str(self.run), '--dry-run'))

    def test_hidden_material_in_a_proof_is_refused(self):
        ids = self.ladder()
        self.solve(ids['split'], 'This follows from SECRET-R001.')
        self.edit_spec(hidden=['SECRET-R001'])
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        with self.assertRaisesRegex(SystemExit, 'Hidden item'):
            call('run', str(self.run), '--dry-run')

    def survivor(self, statement, sketch):
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        number = pr.round_numbers(self.run)[-1]
        slots = self.slots(number)
        slots[0]['stub_text'] = json.dumps(dict(summary='s', verdicts=[], cards=[dict(
            title='Long idea', kind='lemma', statement=statement, role='r', sketch=sketch,
            weakest_step='w', kill_test='k', evidence='none', differs_from='none', confidence=.4,
            cost='small', gap_after='the gluing step')]))
        self.write_slots(number, slots)
        call('run', str(self.run))
        card = pr.read_jsonl(self.run / 'cards.jsonl')[-1]['id']
        call('plan', str(self.run), '--stage', 'assay', '--backend', 'stub', '--cards', card)
        call('run', str(self.run))
        return card

    def test_feed_forward_passes_the_survivors_full_sketch(self):
        statement = 'STATEMENT-HEAD ' + 'hypothesis ' * 60 + 'STATEMENT-TAIL'
        sketch = 'SKETCH-HEAD ' + 'derive ' * 300 + 'SKETCH-TAIL'
        card = self.survivor(statement, sketch)
        spec = pr.load(self.run / 'run.json')
        slot = dict(id='s1', stage='prospect', backend='codex', lens='monovariant')
        prompt, info = pr.build_prompt(self.run, spec, 3, slot)
        self.assertNotIn('STATEMENT-TAIL', prompt.split('## Surviving ideas in full')[0],
                         'the one-line entry stays abridged')
        full = prompt.split('## Surviving ideas in full')[1]
        self.assertIn(statement, full)
        self.assertIn(sketch, full)
        self.assertIn('the gluing step', full)
        self.assertIn(f'Prospector lead marked `survives` by r02-s1', full)
        self.assertIn('not an independently reviewed proof', full)
        self.assertGreaterEqual(info['inlined_words'], 360)
        prompt, info = pr.build_prompt(self.run, dict(spec, max_inline_chars=500), 3, slot)
        self.assertNotIn('SKETCH-HEAD', prompt)
        self.assertNotIn('SKETCH-TAIL', prompt)
        self.assertIn('sketch omitted, not shortened', prompt)
        self.assertIn(card, prompt, 'the abridged line remains')
        self.assertEqual(len(info['omitted']), 1)
        blind, _ = pr.build_prompt(self.run, spec, 3, dict(slot, blind=True))
        self.assertNotIn('SKETCH-HEAD', blind, 'blind slots still see no earlier rounds')

    def test_runs_without_ladder_settings_or_variants_are_unchanged(self):
        spec = pr.load(self.run / 'run.json')
        spec.pop('variant_ladder')
        pr.dump(self.run / 'run.json', spec)
        call('plan', str(self.run), '-n', '2', '--backend', 'stub')
        self.assertTrue(all('variant' not in s for s in self.slots(1)))
        call('run', str(self.run))
        prompt = self.prompt(1)
        self.assertNotIn('Assigned variant', prompt)
        self.assertNotIn('Solved variants', prompt)
        self.assertNotIn('{{', prompt, 'no unfilled placeholder')
        self.assertFalse((self.run / 'variants.jsonl').exists())
        self.assertNotIn('Variants:', call('status', str(self.run)))

    def test_conductor_can_add_a_variant_with_an_outside_proof(self):
        out = call('variant', str(self.run), 'add', '--title', 'Classical case', '--statement',
                   'Every classical widget is blue.', '--kind', 'analogue', '--relation', 'analogue',
                   '--difficulty', '1')
        self.assertIn('conductor-v1', out)
        self.solve('conductor-v1', 'Known proof, quoted with its locator.', '--review-scope',
                   'published; source check pending')
        call('plan', str(self.run), '-n', '1', '--backend', 'stub')
        call('run', str(self.run), '--dry-run')
        self.assertIn('Review scope: published; source check pending.', self.prompt(1))
        with self.assertRaisesRegex(SystemExit, 'Unknown variant'):
            call('variant', str(self.run), 'mark', 'nope', '--status', 'parked', '--reason', 'x')

    def test_new_prompts_are_recognized_as_internal_workers_by_mathdesk(self):
        harness = pr.HUB / 'harness' / 'core'
        if not (harness / 'session_filter.py').exists():
            self.skipTest('MathDesk harness is not installed beside the tools')
        sys.path.insert(0, str(harness))
        self.addCleanup(sys.path.remove, str(harness))
        import session_filter
        call('plan', str(self.run), '--stage', 'variants', '--backend', 'stub')
        call('run', str(self.run))
        self.assertTrue(session_filter._worker_prompt(self.prompt(1)))


if __name__ == '__main__':
    unittest.main()
