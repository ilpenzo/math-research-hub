"""Tests for core/transcripts.py (builder A).

Run: python3 harness/tests/test_transcripts.py   (also under /usr/bin/python3 3.9)

The fixtures in fixtures/transcripts/ are synthetic: hand-made lines that
reproduce the shapes of Claude Code 2.1.30x and Codex 0.146/0.163 transcripts
with invented content and fake paths under /fake/hub.  Every string that must
never reach the page contains "SHOULD NOT APPEAR" or is hidden thinking.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.dirname(HERE)
sys.path.insert(0, HARNESS)

from core import transcripts  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'transcripts')
CLAUDE_FIXTURE = os.path.join(FIXTURES, 'claude_session.jsonl')
CODEX_NEW = os.path.join(FIXTURES, 'codex_rollout_new.jsonl')
CODEX_OLD = os.path.join(FIXTURES, 'codex_rollout_old.jsonl')
FORBIDDEN = ('SHOULD NOT APPEAR', 'THINKING', 'ENCRYPTED', 'DUPLICATE', 'OUTPUT')
REAL_HUB = os.path.dirname(HARNESS)


def read_bytes(path):
    with open(path, 'rb') as handle:
        return handle.read()


def read_lines(path):
    with open(path, 'rb') as handle:
        return handle.read().split(b'\n')[:-1]


def parse(service, path):
    tr = transcripts.Transcript(service, path)
    tr.refresh()
    return tr.items()


def summary(items):
    return [(i['kind'], i['name'], i['text']) for i in items]


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mathdesk-transcripts-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data, mode='wb'):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as handle:
            handle.write(data)
        return path

    def append(self, path, data):
        with open(path, 'ab') as handle:
            handle.write(data)


class ClaudeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tr = transcripts.Transcript('claude-code', CLAUDE_FIXTURE)
        self.tr.refresh()
        self.items = self.tr.items()

    def test_expected_items(self):
        self.assertEqual(summary(self.items), [
            ('user', None, 'Prove that $x^2 \\ge 0$ for every real $x$.\nUse the case split on the sign of $x$.'),
            ('assistant', None, 'First, split on the sign of $x$.\n\nThen $$x^2 = |x|^2 \\ge 0.$$'),
            ('tool', 'Bash', 'ls -la /fake/hub/notes …'),
            ('assistant', None, 'The notes directory has one file.'),
            ('tool', 'Read', 'notes/a.md'),
            ('tool', 'Grep', 'lemma [0-9]+'),
            ('tool', 'mcp__fake__lookup', 'Serre duality'),
            ('assistant', None, 'Hence $x^2 \\ge 0$. $\\blacksquare$'),
            ('notice', 'command', '/model'),
            ('user', None, 'Now do it for complex numbers.'),
            ('assistant', None, 'For complex $z$, $z^2$ need not be real.'),
            ('notice', 'interrupted', 'interrupted'),
            ('notice', 'compaction', 'context compacted'),
            ('user', None, 'Queued follow-up about $|z|^2$.'),
            ('assistant', None, 'Indeed $|z|^2 = z\\bar z \\ge 0$.'),
            ('user', None, 'What does this diagram show?\n\n[1 image attached]'),
            ('user', None, 'Legacy typed prompt without origin fields.'),
            ('notice', 'unknown', 'unknown line type: future-thing'),
        ])

    def test_injections_thinking_and_blobs_are_omitted(self):
        for item in self.items:
            for word in FORBIDDEN + ('HIDDEN', 'Caveat', 'continued from a previous'):
                self.assertNotIn(word, item['text'])

    def test_item_shape(self):
        for item in self.items:
            self.assertEqual(set(item), {'kind', 'ts', 'text', 'name', 'id'})
            self.assertIn(item['kind'], ('user', 'assistant', 'tool', 'notice'))
            self.assertTrue(item['ts'].endswith('Z'))
            self.assertTrue(item['id'])
            if item['kind'] == 'tool':
                self.assertLessEqual(len(item['text']), transcripts.GIST_MAX)
                self.assertNotIn('\n', item['text'])

    def test_duplicate_line_counted_once(self):
        texts = [i['text'] for i in self.items]
        self.assertEqual(texts.count('For complex $z$, $z^2$ need not be real.'), 1)

    def test_status(self):
        status = self.tr.status()
        self.assertEqual(status['adapter'], 'claude-jsonl/1')
        self.assertTrue(status['ok'])
        self.assertEqual(status['lines'], 44)
        self.assertEqual(status['malformed'], 0)
        self.assertEqual(status['unknown'], 1)
        self.assertEqual(status['bytes'], os.path.getsize(CLAUDE_FIXTURE))
        self.assertIn('future-thing', status['warning'])
        self.assertEqual(status['title'], 'Squares of reals are nonnegative')
        self.assertEqual(status['subagents'], 0)
        self.assertEqual(status['tool_results'], 4)
        json.dumps(status)

    def test_items_returns_copies(self):
        self.items[0]['text'] = 'mutated'
        self.assertNotEqual(self.tr.items()[0]['text'], 'mutated')

    def test_long_gist_is_truncated(self):
        gist = transcripts._claude_tool_gist('Bash', {'command': 'echo ' + 'x' * 400}, '/fake/hub')
        self.assertEqual(len(gist), transcripts.GIST_MAX)
        self.assertTrue(gist.endswith('…'))


class IncrementalTest(TempDirCase):
    def test_partial_line_in_two_writes(self):
        lines = read_lines(CLAUDE_FIXTURE)
        head = b'\n'.join(lines[:4]) + b'\n'          # mode, permission-mode, queue-op, prompt
        nxt = lines[4] + b'\n'
        path = self.write('s.jsonl', head + nxt[:25])
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        self.assertEqual([i['kind'] for i in tr.items()], ['user'])
        self.assertEqual(tr.status()['bytes'], len(head))
        self.assertEqual(tr.status()['malformed'], 0)
        self.append(path, nxt[25:] + b'\n'.join(lines[5:]) + b'\n')
        tr.refresh()
        self.assertEqual(tr.items(), parse('claude-code', CLAUDE_FIXTURE))
        self.assertEqual(tr.status()['malformed'], 0)
        self.assertEqual(tr.status()['resets'], 0)

    def test_text_blocks_join_across_refresh(self):
        lines = read_lines(CLAUDE_FIXTURE)
        cut = next(n for n, raw in enumerate(lines) if b'First, split' in raw) + 1
        path = self.write('s.jsonl', b'\n'.join(lines[:cut]) + b'\n')
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        self.assertEqual(tr.items()[-1]['text'], 'First, split on the sign of $x$.')
        self.append(path, b'\n'.join(lines[cut:]) + b'\n')
        tr.refresh()
        assistants = [i for i in tr.items() if i['kind'] == 'assistant']
        self.assertEqual(assistants[0]['text'], 'First, split on the sign of $x$.\n\nThen $$x^2 = |x|^2 \\ge 0.$$')
        self.assertEqual(tr.items(), parse('claude-code', CLAUDE_FIXTURE))

    def test_bytewise_growth_matches_one_shot(self):
        for service, fixture in (('claude-code', CLAUDE_FIXTURE), ('codex', CODEX_NEW), ('codex', CODEX_OLD)):
            data = read_bytes(fixture)
            path = self.write('grow-%s.jsonl' % os.path.basename(fixture), b'')
            tr = transcripts.Transcript(service, path)
            last = 0
            for start in range(0, len(data), 97):
                self.append(path, data[start:start + 97])
                tr.refresh()
                self.assertGreaterEqual(tr.status()['bytes'], last)
                last = tr.status()['bytes']
            self.assertEqual(tr.items(), parse(service, fixture), fixture)
            status = tr.status()
            self.assertEqual((status['malformed'], status['resets']), (0, 0))
            self.assertEqual(status['lines'], parse_status(service, fixture)['lines'])

    def test_malformed_line_counted(self):
        path = self.write('s.jsonl', read_bytes(CLAUDE_FIXTURE))
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        before = tr.items()
        self.append(path, b'{"type":"user","message":{"content":"tru\n[1,2]\n')
        tr.refresh()
        status = tr.status()
        self.assertEqual(status['malformed'], 2)
        self.assertTrue(status['ok'])
        self.assertIn('2 malformed lines', status['warning'])
        self.assertEqual(tr.items(), before)

    def test_truncation_restarts_from_zero(self):
        lines = read_lines(CLAUDE_FIXTURE)
        path = self.write('s.jsonl', read_bytes(CLAUDE_FIXTURE))
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        short = b'\n'.join(lines[:4]) + b'\n'
        with open(path, 'r+b') as handle:          # in-place truncation, same inode
            handle.truncate(0)
            handle.write(short)
        tr.refresh()
        self.assertEqual(summary(tr.items()), summary(parse('claude-code', path)))
        self.assertEqual(len(tr.items()), 1)
        self.assertEqual(tr.status()['resets'], 1)
        self.assertEqual(tr.status()['lines'], 4)

    def test_rotation_restarts_from_zero(self):
        path = self.write('s.jsonl', read_bytes(CODEX_OLD))
        tr = transcripts.Transcript('codex', path)
        tr.refresh()
        self.assertEqual(tr.status()['generation'], 'events')
        replacement = self.write('next.jsonl', read_bytes(CODEX_NEW))
        os.replace(replacement, path)              # new inode, larger file
        tr.refresh()
        self.assertEqual(tr.items(), parse('codex', CODEX_NEW))
        self.assertEqual(tr.status()['generation'], 'items')
        self.assertEqual(tr.status()['resets'], 1)

    def test_rewrite_with_different_head_restarts(self):
        path = self.write('s.jsonl', read_bytes(CODEX_OLD))
        tr = transcripts.Transcript('codex', path)
        tr.refresh()
        with open(path, 'r+b') as handle:          # same inode, grows, different content
            handle.write(read_bytes(CODEX_NEW))
        tr.refresh()
        self.assertEqual(tr.items(), parse('codex', CODEX_NEW))

    def test_missing_file(self):
        tr = transcripts.Transcript('codex', os.path.join(self.tmp, 'absent.jsonl'))
        tr.refresh()
        self.assertEqual(tr.items(), [])
        status = tr.status()
        self.assertFalse(status['ok'])
        self.assertIn('cannot read', status['warning'])

    def test_garbage_file_is_not_ok(self):
        path = self.write('g.jsonl', b'\n'.join([b'not json'] * 30) + b'\n')
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        self.assertFalse(tr.status()['ok'])
        self.assertEqual(tr.items(), [])

    def test_concurrent_refresh_and_items(self):
        data = read_bytes(CLAUDE_FIXTURE)
        path = self.write('s.jsonl', b'')
        tr = transcripts.Transcript('claude-code', path)
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    tr.refresh()
                    tr.items()
                    tr.status()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        for start in range(0, len(data), 211):
            self.append(path, data[start:start + 211])
            time.sleep(0.001)
        stop.set()
        for thread in threads:
            thread.join()
        tr.refresh()
        self.assertEqual(errors, [])
        self.assertEqual(tr.items(), parse('claude-code', CLAUDE_FIXTURE))


def parse_status(service, path):
    tr = transcripts.Transcript(service, path)
    tr.refresh()
    return tr.status()


class CodexAdapterTest(TempDirCase):
    def test_new_generation_items(self):
        tr = transcripts.Transcript('codex', CODEX_NEW)
        tr.refresh()
        self.assertEqual(summary(tr.items()), [
            ('user', None, 'Show that $\\sqrt 2$ is irrational.'),
            ('assistant', 'commentary', 'Checking the notes first.'),
            ('tool', 'shell', 'rg -n "irrational" /fake/hub/notes'),
            ('tool', 'edit', 'notes/sqrt2.md'),
            ('assistant', 'final', 'Suppose $\\sqrt 2 = p/q$ in lowest terms; then $p^2 = 2q^2$.'),
            ('user', None, 'And $\\sqrt 3$?\n\n[1 image attached]'),
            ('notice', 'interrupted', 'interrupted'),
            ('notice', 'compaction', 'context compacted'),
            ('tool', 'subagent', 'started /root/checker'),
            ('tool', 'mcp', 'codex_tui.list_threads'),
            ('tool', 'web.search', 'irrationality of sqrt 3'),
            ('notice', 'unknown', 'unknown line type: future_kind'),
        ])
        for item in tr.items():
            for word in FORBIDDEN + ('AGENTS', 'environment_context', 'DEVELOPER', 'REASONING'):
                self.assertNotIn(word, item['text'])
            self.assertTrue(item['id'])
        status = tr.status()
        self.assertEqual(status['adapter'], 'codex-rollout/1')
        self.assertEqual((status['generation'], status['duplicates'], status['unknown']), ('items', 1, 1))
        self.assertEqual(status['session_id'], '01a15cf8-0000-7000-8000-00000000c0de')
        self.assertEqual(status['version'], '0.163.0')
        self.assertTrue(status['ok'])

    def test_old_generation_items(self):
        tr = transcripts.Transcript('codex', CODEX_OLD)
        tr.refresh()
        self.assertEqual(summary(tr.items()), [
            ('user', None, 'Is every finite integral domain a field?'),
            ('tool', 'shell', 'ls /fake/hub/notes'),
            ('assistant', 'commentary', 'Looking at the notes.'),
            ('tool', 'edit', 'notes/domains.md'),
            ('tool', 'web.search', 'Wedderburn little theorem'),
            ('assistant', 'final', 'Yes: multiplication by $a \\ne 0$ is injective, hence bijective.'),
            ('user', None, 'Now for division rings.'),
            ('notice', 'interrupted', 'interrupted'),
            ('notice', 'compaction', 'context compacted'),
        ])
        self.assertEqual(tr.status()['generation'], 'events')
        self.assertTrue(all(i['id'] for i in tr.items()))

    def test_response_stream_until_generation_is_known(self):
        lines = read_lines(CODEX_NEW)
        cut = next(n for n, raw in enumerate(lines) if b'item_completed' in raw)
        path = self.write('r.jsonl', b'\n'.join(lines[:cut]) + b'\n')
        tr = transcripts.Transcript('codex', path)
        tr.refresh()
        self.assertEqual(tr.status()['generation'], 'responses')
        self.assertEqual(summary(tr.items()), [('user', None, 'Show that $\\sqrt 2$ is irrational.')])
        self.append(path, b'\n'.join(lines[cut:]) + b'\n')
        tr.refresh()
        self.assertEqual(tr.items(), parse('codex', CODEX_NEW))

    def test_subagent_thread_hides_parent_task_text(self):
        lines = [json.loads(raw) for raw in read_lines(CODEX_NEW)]
        lines[0]['payload']['thread_source'] = 'subagent'
        for line in lines:
            item = line['payload'].get('item') or {}
            if item.get('id') == 'um-2':
                del item['client_id']
        path = self.write('sub.jsonl', ''.join(json.dumps(l) + '\n' for l in lines).encode())
        tr = transcripts.Transcript('codex', path)
        tr.refresh()
        users = [i['text'] for i in tr.items() if i['kind'] == 'user']
        self.assertEqual(users, ['Show that $\\sqrt 2$ is irrational.'])   # client_id present
        self.assertEqual(tr.status()['kind'], 'subagent')

    def test_question_reply_is_readable(self):
        answers = json.dumps([{'answer': 'Yes, use them', 'question': 'Use $x$?\nReally?', 'questionItemId': 'q'}])
        text = '<send_user_message_question_reply>\n%s\n</send_user_message_question_reply>' % answers
        line = {'timestamp': '2026-10-21T05:09:00.000Z', 'ordinal': 999, 'type': 'event_msg',
                'payload': {'type': 'item_completed', 'turn_id': 't', 'item': {
                    'type': 'UserMessage', 'id': 'um-q', 'client_id': 'c-q',
                    'content': [{'type': 'text', 'text': text, 'text_elements': []}]}}}
        path = self.write('q.jsonl', read_bytes(CODEX_NEW) + (json.dumps(line) + '\n').encode())
        last = parse('codex', path)[-1]
        self.assertEqual((last['kind'], last['name'], last['text']), ('user', 'answer', '> Use $x$?\n> Really?\n\nYes, use them'))

    def test_claude_continued_in_notice(self):
        line = {'type': 'continued-in', 'timestamp': '2026-10-21T06:10:00.000Z',
                'sessionId': 'x', 'continuedInSessionId': 'bbbbbbbb-0000-4000-8000-000000000009'}
        path = self.write('c.jsonl', read_bytes(CLAUDE_FIXTURE) + (json.dumps(line) + '\n').encode())
        tr = transcripts.Transcript('claude-code', path)
        tr.refresh()
        last = tr.items()[-1]
        self.assertEqual((last['kind'], last['name'], last['id']), ('notice', 'continued', 'bbbbbbbb-0000-4000-8000-000000000009'))
        self.assertEqual(tr.status()['unknown'], 1)


def claude_lines(cwd, session, entrypoint='cli', titles=True):
    out = []
    for raw in read_lines(CLAUDE_FIXTURE):
        line = json.loads(raw)
        if not titles and line.get('type') in ('ai-title', 'custom-title', 'last-prompt'):
            continue
        if 'cwd' in line:
            line['cwd'] = cwd
        if 'entrypoint' in line:
            line['entrypoint'] = entrypoint
        if 'sessionId' in line:
            line['sessionId'] = session
        out.append(json.dumps(line) + '\n')
    return ''.join(out).encode()


def codex_lines(fixture, cwd, ident, thread_source='user'):
    out = []
    for n, raw in enumerate(read_lines(fixture)):
        line = json.loads(raw)
        if n == 0:
            line['payload'].update({'cwd': cwd, 'id': ident, 'thread_source': thread_source})
        out.append(json.dumps(line) + '\n')
    return ''.join(out).encode()


class GenerationSwitchTest(TempDirCase):
    """A resumed old rollout gains item_completed lines; earlier history must stay (review finding)."""

    def test_old_events_survive_switch_to_items(self):
        def line(n, kind, payload):
            return json.dumps({'timestamp': '2026-10-01T00:00:%02dZ' % n, 'type': kind, 'payload': payload})
        rows = [
            line(1, 'session_meta', {'id': 'thread-1', 'cwd': '/fake/hub'}),
            line(2, 'event_msg', {'type': 'user_message', 'message': 'old question'}),
            line(3, 'event_msg', {'type': 'agent_message', 'message': 'old answer'}),
            line(4, 'event_msg', {'type': 'item_completed', 'item': {
                'type': 'UserMessage', 'id': 'u2', 'client_id': 'c2',
                'content': [{'type': 'text', 'text': 'new question'}]}}),
            line(5, 'event_msg', {'type': 'item_completed', 'item': {
                'type': 'AgentMessage', 'id': 'a2', 'phase': 'final_answer',
                'content': [{'type': 'Text', 'text': 'new answer'}]}}),
        ]
        path = self.write('rollout.jsonl', ('\n'.join(rows) + '\n').encode())
        reader = transcripts.Transcript('codex', path)
        reader.refresh()
        texts = [i['text'] for i in reader.items() if i['kind'] in ('user', 'assistant')]
        self.assertEqual(texts, ['old question', 'old answer', 'new question', 'new answer'])

    def test_turn_in_both_forms_is_not_duplicated(self):
        def line(n, kind, payload):
            return json.dumps({'timestamp': '2026-10-01T00:00:%02dZ' % n, 'type': kind, 'payload': payload})
        rows = [
            line(1, 'session_meta', {'id': 'thread-2', 'cwd': '/fake/hub'}),
            line(2, 'event_msg', {'type': 'user_message', 'message': 'question'}),
            line(3, 'event_msg', {'type': 'item_completed', 'item': {
                'type': 'UserMessage', 'id': 'u1', 'client_id': 'c1',
                'content': [{'type': 'text', 'text': 'question'}]}}),
            line(4, 'event_msg', {'type': 'item_completed', 'item': {
                'type': 'AgentMessage', 'id': 'a1', 'phase': 'final_answer',
                'content': [{'type': 'Text', 'text': 'answer'}]}}),
        ]
        path = self.write('rollout2.jsonl', ('\n'.join(rows) + '\n').encode())
        reader = transcripts.Transcript('codex', path)
        reader.refresh()
        texts = [i['text'] for i in reader.items() if i['kind'] in ('user', 'assistant')]
        self.assertEqual(texts, ['question', 'answer'])


class DiscoverTest(TempDirCase):
    HUB = '/fake/hub'

    def setUp(self):
        TempDirCase.setUp(self)
        self.claude = os.path.join(self.tmp, 'claude')
        self.codex = os.path.join(self.tmp, 'codex')
        self.env = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.claude, 'CODEX_HOME': self.codex})
        self.env.start()
        self.now = time.time()

    def tearDown(self):
        self.env.stop()
        TempDirCase.tearDown(self)

    def touch(self, path, age_seconds):
        stamp = self.now - age_seconds
        os.utime(path, (stamp, stamp))

    def make_claude(self):
        sessions = [
            ('-fake-hub', 'aaaaaaaa-0000-4000-8000-000000000001', '/fake/hub', 'cli', True, 300),
            ('-fake-hub-projects-demo', 'aaaaaaaa-0000-4000-8000-000000000002', '/fake/hub/projects/demo', 'cli', False, 60),
            ('-fake-hubby', 'aaaaaaaa-0000-4000-8000-000000000003', '/fake/hubby', 'cli', True, 10),
            ('-fake-hub', 'aaaaaaaa-0000-4000-8000-000000000004', '/fake/hub', 'cli', True, 30 * 86400),
            ('-fake-hub', 'aaaaaaaa-0000-4000-8000-000000000005', '/fake/hub', 'sdk-cli', True, 120),
        ]
        for folder, session, cwd, entry, titles, age in sessions:
            path = self.write(os.path.join('claude', 'projects', folder, session + '.jsonl'),
                              claude_lines(cwd, session, entry, titles))
            self.touch(path, age)
        os.makedirs(os.path.join(self.claude, 'projects', '-fake-hub', 'aaaaaaaa-0000-4000-8000-000000000001', 'subagents'))

    def test_claude_discover(self):
        self.make_claude()
        start = time.monotonic()
        found = transcripts.discover('claude-code', self.HUB)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual([f['session_id'][-1] for f in found], ['2', '1'])
        demo, root = found
        self.assertEqual(root['title'], 'Squares of reals are nonnegative')
        self.assertEqual(demo['title'], 'Prove that $x^2 \\ge 0$ for every real $x$.')   # first prompt, first line
        self.assertEqual(root['cwd'], '/fake/hub')
        self.assertEqual(root['started'], '2026-10-21T06:01:00.000Z')
        self.assertTrue(root['updated'].endswith('Z') and root['updated'] > demo['started'])
        self.assertTrue(root['transcript_path'].endswith('aaaaaaaa-0000-4000-8000-000000000001.jsonl'))
        self.assertEqual({f['kind'] for f in found}, {'user'})
        for entry in found:
            self.assertTrue({'session_id', 'transcript_path', 'cwd', 'started', 'updated', 'title', 'kind'} <= set(entry))
        everything = transcripts.discover('claude-code', self.HUB, include_subagents=True)
        self.assertEqual([f['session_id'][-1] for f in everything], ['2', '5', '1'])
        self.assertEqual(everything[1]['kind'], 'sdk')
        self.assertEqual(len(transcripts.discover('claude-code', self.HUB, limit=1)), 1)
        self.assertEqual(len(transcripts.discover('claude-code', self.HUB, since_days=60)), 3)

    def make_codex(self, with_db=True):
        day = time.strftime('%Y/%m/%d', time.localtime(self.now))
        rows = [
            ('01a15cf8-0000-7000-8000-000000000011', '/fake/hub', 'user', 200, 'Irrationality of roots', CODEX_NEW),
            ('01a15cf8-0000-7000-8000-000000000012', '/fake/hub/projects/demo', 'user', 100, '', CODEX_OLD),
            ('01a15cf8-0000-7000-8000-000000000013', '/fake/hub', 'subagent', 50, 'Checker', CODEX_NEW),
            ('01a15cf8-0000-7000-8000-000000000014', '/fake/hubby', 'user', 20, 'Elsewhere', CODEX_NEW),
            ('01a15cf8-0000-7000-8000-000000000015', '/fake/hub', 'user', 30 * 86400, 'Old thread', CODEX_NEW),
        ]
        index = []
        records = []
        for ident, cwd, source, age, name, fixture in rows:
            path = self.write(os.path.join('codex', 'sessions', day, 'rollout-2026-10-21T05-00-00-%s.jsonl' % ident),
                              codex_lines(fixture, cwd, ident, source))
            if age > 86400:
                old_day = time.strftime('%Y/%m/%d', time.localtime(self.now - age))
                moved = os.path.join(self.codex, 'sessions', old_day, os.path.basename(path))
                os.makedirs(os.path.dirname(moved), exist_ok=True)
                os.replace(path, moved)
                path = moved
            self.touch(path, age)
            if name:
                index.append(json.dumps({'id': ident, 'thread_name': name, 'updated_at': '2026-10-21T05:00:00Z'}))
            stamp = int(self.now - age)
            records.append((ident, path, stamp - 60, stamp, cwd, name, source, (stamp - 60) * 1000, stamp * 1000))
        records.append(('01a15cf8-0000-7000-8000-000000000016', os.path.join(self.codex, 'missing.jsonl'),
                        int(self.now) - 70, int(self.now) - 10, '/fake/hub', 'Gone', 'user', None, None))
        self.write(os.path.join('codex', 'session_index.jsonl'), ('\n'.join(index) + '\n').encode())
        if with_db:
            con = sqlite3.connect(os.path.join(self.codex, 'state_5.sqlite'))
            con.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,'
                        ' updated_at INTEGER NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL, name TEXT,'
                        ' first_user_message TEXT NOT NULL DEFAULT "", thread_source TEXT,'
                        ' created_at_ms INTEGER, updated_at_ms INTEGER)')
            for ident, path, created, updated, cwd, name, source, cms, ums in records:
                con.execute('INSERT INTO threads (id, rollout_path, created_at, updated_at, cwd, title, name,'
                            ' first_user_message, thread_source, created_at_ms, updated_at_ms)'
                            ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (ident, path, created, updated, cwd, 'First message title', name or None,
                             'First message title', source, cms, ums))
            con.commit()
            con.close()

    def check_codex(self, found, titles):
        self.assertEqual([f['session_id'][-2:] for f in found], ['12', '11'])
        self.assertEqual([f['title'] for f in found], titles)
        self.assertEqual({f['kind'] for f in found}, {'user'})
        self.assertEqual(found[1]['cwd'], '/fake/hub')
        for entry in found:
            self.assertTrue(os.path.exists(entry['transcript_path']))
            self.assertTrue(entry['started'].endswith('Z') and entry['updated'].endswith('Z'))

    def test_codex_discover_sqlite(self):
        self.make_codex(with_db=True)
        start = time.monotonic()
        found = transcripts.discover('codex', self.HUB)
        self.assertLess(time.monotonic() - start, 1.0)
        self.check_codex(found, ['First message title', 'Irrationality of roots'])
        everything = transcripts.discover('codex', self.HUB, include_subagents=True)
        self.assertEqual([f['session_id'][-2:] for f in everything], ['13', '12', '11'])
        self.assertEqual(everything[0]['kind'], 'subagent')
        self.assertEqual(len(transcripts.discover('codex', self.HUB, since_days=60)), 3)

    def test_codex_discover_without_sqlite(self):
        self.make_codex(with_db=False)
        found = transcripts.discover('codex', self.HUB)
        self.check_codex(found, [None, 'Irrationality of roots'])
        self.assertEqual([f['started'] for f in found], ['2026-09-01T10:00:00.000Z', '2026-10-21T05:00:00.000Z'])
        everything = transcripts.discover('codex', self.HUB, include_subagents=True)
        self.assertEqual([f['kind'] for f in everything], ['subagent', 'user', 'user'])

    def test_empty_homes(self):
        self.assertEqual(transcripts.discover('claude-code', self.HUB), [])
        self.assertEqual(transcripts.discover('codex', self.HUB), [])
        with self.assertRaises(ValueError):
            transcripts.discover('gemini', self.HUB)


class RealTranscriptSmokeTest(unittest.TestCase):
    """Parses the most recent real hub transcripts; prints counts only."""

    def check_service(self, service):
        start = time.monotonic()
        found = transcripts.discover(service, REAL_HUB)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)
        if not found:
            self.skipTest('no recent %s transcripts for %s' % (service, REAL_HUB))
        totals = {'user': 0, 'assistant': 0, 'tool': 0, 'notice': 0}
        report = []
        for entry in found[:3]:
            tr = transcripts.Transcript(service, entry['transcript_path'])
            start = time.monotonic()
            tr.refresh()
            items = tr.items()
            took = time.monotonic() - start
            self.assertLess(took, 1.0, entry['transcript_path'])
            status = tr.status()
            self.assertTrue(status['ok'], status['warning'])
            counts = dict((kind, sum(1 for i in items if i['kind'] == kind)) for kind in totals)
            for kind, value in counts.items():
                totals[kind] += value
            report.append('%.1fMB %.3fs %s' % (status['bytes'] / 1e6, took,
                                               ' '.join('%s=%d' % kv for kv in sorted(counts.items()))))
        sys.stderr.write('\n[real %s] discover %d in %.3fs; %s\n' % (service, len(found), elapsed, '; '.join(report)))
        self.assertGreaterEqual(totals['user'], 1)
        self.assertGreaterEqual(totals['assistant'], 1)

    def test_claude_code(self):
        if not os.environ.get('CLAUDE_CONFIG_DIR') and not os.path.isdir(os.path.join(transcripts.claude_home(), 'projects')):
            self.skipTest('no Claude Code projects directory')
        self.check_service('claude-code')

    def test_codex(self):
        if not os.path.isdir(transcripts.codex_home()):
            self.skipTest('no Codex home')
        self.check_service('codex')


if __name__ == '__main__':
    unittest.main(verbosity=1)
