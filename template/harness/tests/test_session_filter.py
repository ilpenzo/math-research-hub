"""Prospector capture/read regression tests; temporary state, no model calls."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))
from core import session_filter, spool, transcripts

PROMPTS = [(HARNESS.parent / 'templates' / 'prospector' / name).read_text()
           for name in ('explorer.md', 'assay.md', 'dig.md')]


def native_rows(service, cwd, prompt, generation='response_item'):
    if service == 'claude-code':
        return [{'type': 'user', 'cwd': cwd, 'entrypoint': 'cli', 'sessionId': 'worker',
                 'message': {'content': [{'type': 'text', 'text': prompt}]}}]
    meta = {'type': 'session_meta', 'payload': {'cwd': cwd, 'id': 'worker', 'source': 'exec'}}
    if generation == 'response_item':
        row = {'type': generation, 'payload': {'type': 'message', 'role': 'user',
               'content': [{'type': 'input_text', 'text': prompt}]}}
    elif generation == 'user_message':
        row = {'type': 'event_msg', 'payload': {'type': generation, 'message': prompt}}
    else:
        row = {'type': 'event_msg', 'payload': {'type': 'item_completed',
               'item': {'type': 'UserMessage', 'content': [{'type': 'Text', 'text': prompt}]}}}
    return [meta, row]


class SessionFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_rows(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return str(path)

    def event(self, service, ident, text='Let $x=1$.', **values):
        event = spool.push_event(text, session=ident, cwd=str(self.root))
        event.update(service=service, **values)
        return event

    def test_exact_worker_signatures_and_user_controls(self):
        work = str(self.root / 'prospector' / 'run' / 'rounds' / 'r04' / 's1' / 'work')
        for service in ('codex', 'claude-code'):
            self.assertTrue(session_filter.is_internal_session(service, cwd=work))
            self.assertTrue(session_filter.is_internal_session(service, cwd=str(
                self.root / 'prospector-doctor-123' / 'codex' / 'work')))
            for prompt in PROMPTS:
                self.assertTrue(session_filter.is_internal_session(service, prompt=prompt))
                heading = prompt.splitlines()[0]
                for ordinary in ('Please explain Prospector.', 'Please review this:\n' + prompt,
                                 heading + '\n\nWhat does this heading mean?'):
                    self.assertFalse(session_filter.is_internal_session(service, prompt=ordinary))
            for cwd in (str(self.root), str(self.root / 'prospector' / 'run'), work + '-notes'):
                self.assertFalse(session_filter.is_internal_session(service, cwd=cwd))
        self.assertFalse(session_filter.is_internal_session('desk', cwd=work, prompt=PROMPTS[0]))

    def test_legacy_discovery_prompts_remain_internal(self):
        old = [
            '# Prospector explorer: retained\n\nYou are one of several independent explorers '
            'searching for a proof of the target below.',
            '# Prospector dig: develop this idea into an argument\n\nThe claim card below '
            'survived an adversarial assay. Develop it into the most complete argument you can.',
        ]
        for service in ('codex', 'claude-code'):
            for prompt in old:
                self.assertTrue(session_filter.is_internal_session(service, prompt=prompt))
                self.assertFalse(session_filter.is_internal_session(service, prompt='Review this:\n' + prompt))

    def test_bounded_native_metadata_all_generations(self):
        for service, generations in (('claude-code', ('claude',)),
                                     ('codex', ('response_item', 'user_message', 'item_completed'))):
            for generation in generations:
                for number, prompt in enumerate(PROMPTS):
                    rows = native_rows(service, str(self.root), prompt, generation)
                    if service == 'codex':
                        rows.insert(1, native_rows(service, str(self.root), '# AGENTS.md instructions')[1])
                    path = self.write_rows(self.root / (service + generation + str(number) + '.jsonl'), rows)
                    self.assertTrue(session_filter.is_internal_session(service, transcript_path=path))
                    # A later quoted template must not reclassify a user conversation.
                    rows = native_rows(service, str(self.root), 'Explain Prospector.', generation) + rows[1:]
                    path = self.write_rows(Path(path), rows)
                    self.assertFalse(session_filter.is_internal_session(service, transcript_path=path))
                    self.assertFalse(session_filter.is_internal_session(service, prompt=prompt,
                                                                         transcript_path=path))

    def test_native_cache_refresh_and_missing_or_oversize_data(self):
        path = self.root / 'growing.jsonl'
        path.write_text('')
        self.assertFalse(session_filter.is_internal_session('codex', transcript_path=str(path)))
        self.write_rows(path, native_rows('codex', str(self.root), PROMPTS[0]))
        self.assertTrue(session_filter.is_internal_session('codex', transcript_path=str(path)))
        self.assertFalse(session_filter.is_internal_session('codex', transcript_path=str(self.root / 'missing.jsonl')))
        path.write_text('x' * session_filter.HEAD_BYTES + '\n' + path.read_text())
        self.assertFalse(session_filter.is_internal_session('codex', transcript_path=str(path)))

    def test_future_capture_all_roles_and_both_services(self):
        for service in ('codex', 'claude-code'):
            for number, prompt in enumerate(PROMPTS):
                path = self.write_rows(self.root / (service + str(number) + '.jsonl'),
                                       native_rows(service, str(self.root), prompt))
                common = {'cwd': str(self.root), 'session_id': 'worker', 'transcript_path': path}
                for payload in (dict(common, hook_event_name='UserPromptSubmit', prompt=prompt),
                                dict(common, hook_event_name='Stop', last_assistant_message='Let $x=1$.')):
                    self.assertIsNone(spool.event_from_hook(service, payload, lambda _: True, lambda _: None))
            payload = {'cwd': str(self.root), 'session_id': 'human', 'hook_event_name': 'UserPromptSubmit',
                       'prompt': PROMPTS[0].splitlines()[0] + '\n\nPlease explain this heading.'}
            self.assertIsNotNone(spool.event_from_hook(service, payload, lambda _: True, lambda _: None))

    def test_existing_captures_are_hidden_everywhere_and_retained(self):
        folder = str(self.root / 'spool')
        hidden = []
        for service in ('codex', 'claude-code'):
            for number, prompt in enumerate(PROMPTS):
                ident = service + str(number)
                # Simulate old captures with no transcript path, and final arriving first.
                events = [self.event(service, ident), self.event(service, ident, prompt, kind='prompt')]
                for event in events:
                    spool.write_event(event, folder)
                    hidden.append(event)
        ordinary = self.event('codex', 'human', 'Please explain Prospector.', kind='prompt')
        spool.write_event(ordinary, folder)
        index = spool.SpoolIndex(folder)
        notices = []
        index.listeners.append(notices.append)
        self.assertEqual([e['id'] for e in index.scan()], [ordinary['id']])
        self.assertEqual([e['id'] for e in notices], [ordinary['id']])
        self.assertEqual([s['session_id'] for s in index.sessions()], ['human'])
        self.assertEqual([e['id'] for e in index.wait(0, 0)[0]], [ordinary['id']])
        for event in hidden:
            self.assertIsNone(index.get(event['id']))
            self.assertEqual(index.session_events(event['service'], event['session_id']), [])
        self.assertEqual(len(list(Path(folder).glob('*/*.json'))), len(hidden) + 1)
        self.assertEqual(len(spool.SpoolIndex(folder).scan()), 1)

    def test_late_identification_hides_previously_indexed_events(self):
        folder = str(self.root / 'spool')
        final = self.event('codex', 'late')
        spool.write_event(final, folder)
        index = spool.SpoolIndex(folder)
        index.scan()
        spool.write_event(self.event('codex', 'late', PROMPTS[0], kind='prompt'), folder)
        index.scan()
        self.assertIsNone(index.get(final['id']))
        self.assertEqual(index.sessions(), [])
        self.assertEqual(index.wait(0, 0)[0], [])

    def test_later_template_quote_preserves_user_conversation(self):
        folder = str(self.root / 'spool')
        index = spool.SpoolIndex(folder)
        for prompt in ['Explain Prospector.', PROMPTS[0]]:
            spool.write_event(self.event('codex', 'human', prompt, kind='prompt'), folder)
            index.scan()
        self.assertEqual([s['session_id'] for s in index.sessions()], ['human'])
        self.assertEqual(len(index.wait(0, 0)[0]), 2)

    def test_discovery_cannot_restore_a_hidden_capture_or_notify(self):
        from core.server import Desk
        folder = str(self.root / 'spool')
        event = self.event('codex', 'worker', PROMPTS[0], kind='prompt')
        spool.write_event(event, folder)
        desk = Desk.__new__(Desk)
        desk.index = spool.SpoolIndex(folder)
        desk.index.scan()
        desk.discover = lambda: [{'service': 'codex', 'session_id': 'worker', 'cwd': str(self.root)}]
        desk.pdf = mock.Mock()
        desk.settings = {'auto_pdf': True, 'autoopen': 'always'}
        self.assertEqual(desk.sessions(), [])
        self.assertIsNone(desk.session('codex', 'worker'))
        desk.on_event(dict(event, kind='final'))
        desk.reconcile_pdfs()
        desk.pdf.submit.assert_not_called()

    def test_native_discovery_filters_before_limit_and_ignores_titles(self):
        hub = str(self.root / 'hub')
        codex = self.root / 'codex'
        claude = self.root / 'claude'
        day = time.strftime('%Y/%m/%d')
        rows = []
        for number, prompt in enumerate(['Explain Prospector.'] + PROMPTS):
            ident = 'session-' + str(number)
            path = codex / 'sessions' / day / ('rollout-' + ident + '.jsonl')
            native = native_rows('codex', hub, prompt)
            native[0]['payload']['id'] = ident
            self.write_rows(path, native)
            stamp = int(time.time()) - 30 + number
            os.utime(path, (stamp, stamp))
            rows.append((ident, str(path), hub, stamp, PROMPTS[0], prompt))
            path = claude / 'projects' / transcripts._slug(hub) / (ident + '.jsonl')
            self.write_rows(path, native_rows('claude-code', hub, prompt)
                            + [{'type': 'custom-title', 'customTitle': PROMPTS[0]}])
            os.utime(path, (stamp, stamp))
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex), 'CLAUDE_CONFIG_DIR': str(claude)}):
            for service in ('claude-code', 'codex'):
                for include in (False, True):
                    found = transcripts.discover(service, hub, limit=1, include_subagents=include)
                    self.assertEqual([e['session_id'] for e in found], ['session-0'])
            con = sqlite3.connect(str(codex / 'state_5.sqlite'))
            con.execute('CREATE TABLE threads (id, rollout_path, cwd, updated_at, title, first_user_message)')
            con.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?)', rows)
            con.commit()
            con.close()
            self.assertEqual([e['session_id'] for e in transcripts.discover('codex', hub, limit=1)], ['session-0'])

    def test_prospector_backends_suppress_hooks(self):
        spec = importlib.util.spec_from_file_location('prospector_test', HARNESS.parent / 'tools' / 'prospector.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        settings = {'backends': {}, 'budget': {}}
        for backend in ('codex', 'claude'):
            with mock.patch.object(module.shutil, 'which', return_value=sys.executable), \
                    mock.patch.object(module, 'run_process', return_value=(0, 'completed', 0)) as run, \
                    mock.patch.object(module, 'jsonl_events', return_value=[]):
                module.CALLERS[backend](settings, {}, 'prompt', self.root, self.root)
                self.assertEqual(run.call_args.args[3]['MATHDESK_SKIP'], '1')


if __name__ == '__main__':
    unittest.main()
