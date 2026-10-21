"""Tests for the hook, spool and server (parent-owned core).

Run: python3 harness/tests/test_core.py   (also under /usr/bin/python3 for the hook path)
Uses a temporary state directory and a free port; never touches .hub/harness.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.dirname(HARNESS)
sys.path.insert(0, HARNESS)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


STATE = tempfile.mkdtemp(prefix='mathdesk-test-')
PORT = free_port()
os.environ['MATHDESK_STATE'] = STATE
os.environ['MATHDESK_PORT'] = str(PORT)

from core import config, spool  # noqa: E402

INSIDE = os.path.join(HUB, 'projects', 'demo')


def run_hook(payload, service='claude-code', env=None):
    full = dict(os.environ, MATHDESK_STATE=STATE, MATHDESK_PORT=str(PORT), **(env or {}))
    return subprocess.run(['/usr/bin/python3', os.path.join(HARNESS, 'hook.py'), '--service', service],
                          input=payload if isinstance(payload, str) else json.dumps(payload),
                          capture_output=True, text=True, timeout=20, env=full)


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def write_text(path, text):
    with open(path, 'w') as handle:
        handle.write(text)


def spooled():
    out = []
    for root, _, files in os.walk(os.path.join(STATE, 'spool')):
        out += [os.path.join(root, f) for f in files if f.endswith('.json') and not f.startswith('.')]
    return out


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.ensure_state()
        config.save_settings({'autostart': False})

    def test_outside_hub_ignored(self):
        before = len(spooled())
        result = run_hook({'hook_event_name': 'Stop', 'cwd': '/tmp', 'session_id': 's',
                           'last_assistant_message': 'x'})
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertEqual(len(spooled()), before)

    def test_prompt_and_final_spooled(self):
        before = len(spooled())
        for payload in ({'hook_event_name': 'UserPromptSubmit', 'cwd': INSIDE, 'session_id': 'h1',
                         'prompt': 'Is $x$ ok?', 'prompt_id': 'p1'},
                        {'hook_event_name': 'Stop', 'cwd': INSIDE, 'session_id': 'h1', 'prompt_id': 'p1',
                         'last_assistant_message': 'Yes, $x^2$.'}):
            result = run_hook(payload)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        files = spooled()
        self.assertEqual(len(files), before + 2)
        events = [read_json(f) for f in files]
        self.assertTrue(any(e['kind'] == 'prompt' and e['project'] == 'demo' for e in events))
        mode = os.stat(files[0]).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_bad_input_never_fails(self):
        for payload in ('not json', '', '[]', '{"hook_event_name": "Stop"}'):
            result = run_hook(payload)
            self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_skip_env(self):
        before = len(spooled())
        run_hook({'hook_event_name': 'Stop', 'cwd': INSIDE, 'session_id': 'k',
                  'last_assistant_message': 'x'}, env={'MATHDESK_SKIP': '1'})
        self.assertEqual(len(spooled()), before)

    def test_concurrent_hooks(self):
        before = len(spooled())
        threads = [threading.Thread(target=run_hook, args=({'hook_event_name': 'Stop', 'cwd': INSIDE,
                                                            'session_id': 'c%d' % i, 'turn_id': 't',
                                                            'last_assistant_message': 'same text'}, 'codex'))
                   for i in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(spooled()), before + 16)

    def test_hook_latency(self):
        started = time.time()
        run_hook({'hook_event_name': 'Stop', 'cwd': INSIDE, 'session_id': 'lat', 'last_assistant_message': 'x'})
        self.assertLess(time.time() - started, 0.5)


class SpoolTests(unittest.TestCase):
    def test_dedupe_only_with_turn_id(self):
        folder = tempfile.mkdtemp()
        base = {'cwd': INSIDE, 'session_id': 's', 'last_assistant_message': 'same', 'hook_event_name': 'Stop'}
        for payload in (dict(base, turn_id='t1'), dict(base, turn_id='t1'), dict(base), dict(base)):
            spool.write_event(spool.event_from_hook('codex', payload, config.inside_hub, config.project_for_cwd), folder)
            time.sleep(0.002)
        index = spool.SpoolIndex(folder)
        index.scan()
        # The repeated (turn t1) delivery is dropped; identical answers without a turn id are kept.
        self.assertEqual(index.seq, 3)
        self.assertEqual(spool.SpoolIndex(folder).scan().__len__(), 3)  # restart recovers the same set

    def test_partial_and_corrupt_files_ignored(self):
        folder = tempfile.mkdtemp()
        day = os.path.join(folder, '20261021')
        os.makedirs(day)
        write_text(os.path.join(day, '.tmp-x.json'), '{')
        write_text(os.path.join(day, '1-1-x.json'), '{broken')
        write_text(os.path.join(day, '2-1-y.json'), json.dumps({'v': 1}))
        index = spool.SpoolIndex(folder)
        index.scan()
        self.assertEqual(index.seq, 0)

    def test_has_math(self):
        self.assertTrue(spool.has_math('let $x$'))
        self.assertTrue(spool.has_math('\\[a\\]'))
        self.assertFalse(spool.has_math('costs $ 5'))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.save_settings({'autoopen': 'never', 'auto_pdf': False, 'autostart': False})
        cls.proc = subprocess.Popen([sys.executable, os.path.join(HARNESS, 'mathdesk.py'), 'serve'],
                                    env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', PORT), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.1)
        cls.token = config.token()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.proc.stderr.close()

    def request(self, path, cookie=True, host=None, method='GET', body=None, headers=None):
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (PORT, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if cookie:
            req.add_header('Authorization', 'Bearer ' + (cookie if isinstance(cookie, str) else self.token))
        if host:
            req.add_header('Host', host)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def test_auth_host_and_origin(self):
        self.assertEqual(self.request('/api/status', cookie=False)[0], 401)
        self.assertEqual(self.request('/api/status', cookie='wrong-token')[0], 401)
        req = urllib.request.Request('http://127.0.0.1:%d/api/status' % PORT, headers={'Cookie': 'mathdesk=' + self.token})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(caught.exception.code, 401)   # cookies are not credentials
        self.assertEqual(self.request('/api/status', host='evil.example:%d' % PORT)[0], 421)
        self.assertEqual(self.request('/api/status')[0], 200)
        self.assertEqual(self.request('/api/pins', method='POST', body={'action': 'add'})[0], 403)
        origin = {'X-MathDesk': '1', 'Origin': 'http://evil.example', 'Content-Type': 'application/json'}
        self.assertEqual(self.request('/api/pins', method='POST', body={'action': 'add'}, headers=origin)[0], 403)

    def test_page_needs_no_credentials_but_data_does(self):
        self.assertEqual(self.request('/', cookie=False)[0], 200)
        self.assertEqual(self.request('/print/turn/0123456789abcdef0123', cookie=False)[0], 200)
        for path in ('/api/sessions', '/pdf/notebook', '/library/L195.pdf', '/tex/' + '0' * 64 + '.svg'):
            self.assertEqual(self.request(path, cookie=False)[0], 401, path)

    def test_security_headers(self):
        status, _, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
        self.assertIsNone(headers.get('Access-Control-Allow-Origin'))

    def test_static_traversal(self):
        for path in ('/static/../core/server.py', '/static/%2e%2e/core/config.py', '/static/vendor/../../hook.py'):
            self.assertEqual(self.request(path, cookie=False)[0], 404, path)
        self.assertEqual(self.request('/static/app.js', cookie=False)[0], 200)

    def test_document_paths_refused(self):
        for rel in ('../etc/passwd', '/etc/passwd', '.hub/harness/token', 'hub.json'):
            status = self.request('/api/doc?path=' + urllib.request.quote(rel))[0]
            self.assertIn(status, (404, 503), rel)

    def test_pins_round_trip(self):
        headers = {'X-MathDesk': '1', 'Origin': 'http://127.0.0.1:%d' % PORT, 'Content-Type': 'application/json'}
        status, body, _ = self.request('/api/pins', method='POST', headers=headers,
                                       body={'action': 'add', 'item': {'title': 't', 'text': 'Let $x$.'}})
        self.assertEqual(status, 200)
        pins = json.loads(body)['pins']
        self.assertEqual(pins[-1]['text'], 'Let $x$.')
        status, body, _ = self.request('/api/pins', method='POST', headers=headers,
                                       body={'action': 'remove', 'id': pins[-1]['id']})
        self.assertEqual(len(json.loads(body)['pins']), len(pins) - 1)

    def test_live_event_and_session(self):
        status, body, _ = self.request('/api/sessions')
        seq = json.loads(body)['seq']
        result = run_hook({'hook_event_name': 'Stop', 'cwd': INSIDE, 'session_id': 'live-1',
                           'last_assistant_message': 'Rendered $\\chi(\\mathcal O_X)=1$.'}, 'codex')
        self.assertEqual(result.returncode, 0)
        started = time.time()
        status, body, _ = self.request('/api/wait?since=%d&timeout=5' % seq)
        self.assertLess(time.time() - started, 2.0)
        events = json.loads(body)['events']
        self.assertTrue(any(e['session_id'] == 'live-1' and e['math'] for e in events))
        status, body, _ = self.request('/api/session/codex/live-1')
        self.assertEqual(status, 200)
        self.assertIn('chi', json.loads(body)['items'][-1]['text'])

    def test_print_nonce_is_read_only_and_scoped(self):
        from core import server
        # Mint a nonce the way the server does, through a second Desk-less path: use the API surface.
        status, body, _ = self.request('/api/status')
        self.assertEqual(status, 200)
        nonce = 'n' * 32
        # Unknown nonces are rejected outright.
        self.assertEqual(self.request('/api/event/' + '0' * 20, cookie=nonce)[0], 401)
        self.assertTrue(server.print_allowed('/api/session/codex/x'))
        self.assertTrue(server.print_allowed('/api/doc'))
        for path in ('/api/docs', '/api/consult', '/pdf/notebook', '/api/search', '/file'):
            self.assertFalse(server.print_allowed(path), path)

    def test_transcript_path_check(self):
        from core.server import allowed_transcript
        self.assertFalse(allowed_transcript('claude-code', '/etc/passwd'))
        self.assertFalse(allowed_transcript('codex', os.path.expanduser('~/.codex/auth.json')))
        self.assertFalse(allowed_transcript('claude-code', os.path.expanduser('~/.claude/projects/../settings.jsonl')))
        self.assertTrue(allowed_transcript('claude-code', os.path.expanduser('~/.claude/projects/x/y.jsonl')))


class InstallTests(unittest.TestCase):
    """Hook installation edits copies only (paths are monkeypatched)."""

    def setUp(self):
        import mathdesk
        self.md = mathdesk
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'settings.json')
        phone = {'type': 'command', 'command': '/usr/bin/python3 /x/notify_phone.py', 'timeout': 15}
        write_text(self.path, json.dumps({'model': 'opus', 'hooks': {'Stop': [
            {'matcher': '', 'hooks': [phone]}]}}))
        self.phone = phone

    def test_install_idempotent_and_uninstall_exact(self):
        self.md.install_into(self.path, 'codex')
        self.assertIsNone(self.md.install_into(self.path, 'codex'))   # second run changes nothing
        data = read_json(self.path)
        self.assertEqual(data['model'], 'opus')
        self.assertEqual(data['hooks']['Stop'][0]['hooks'], [self.phone])
        # A user may move our command into a shared group; uninstall must keep the neighbour.
        data['hooks']['Stop'][0]['hooks'].append(dict(type='command', command=self.md.hook_command('codex')))
        data['hooks']['Stop'] = data['hooks']['Stop'][:1]
        write_text(self.path, json.dumps(data))
        self.md.install_into(self.path, 'codex', remove=True)
        after = read_json(self.path)
        self.assertEqual(after['hooks']['Stop'], [{'matcher': '', 'hooks': [self.phone]}])
        self.assertNotIn('UserPromptSubmit', after['hooks'])

    def test_symlink_mode_and_missing_dir(self):
        real = os.path.join(self.dir, 'dotfiles-settings.json')
        os.rename(self.path, real)
        os.chmod(real, 0o600)
        os.symlink(real, self.path)
        self.md.install_into(self.path, 'claude-code')
        self.assertTrue(os.path.islink(self.path))                   # the dotfile link survives
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)      # and so does the mode
        self.assertIn('Stop', read_json(real)['hooks'])
        missing = os.path.join(self.dir, 'no-such-dir', 'hooks.json')
        self.assertTrue(self.md.install_into(missing, 'codex').startswith('skipped'))

    def test_uninstall_leaves_no_empty_lists(self):
        self.md.install_into(self.path, 'codex', remove=True)       # nothing of ours installed
        self.assertNotIn('UserPromptSubmit', read_json(self.path)['hooks'])

    def test_backups_never_overwrite(self):
        first = self.md._backup(self.path)
        write_text(self.path, '{"changed": true}')
        second = self.md._backup(self.path)
        self.assertNotEqual(first, second)
        self.assertIn('opus', open(first).read())


class ConsultTests(unittest.TestCase):
    def test_failure_releases_the_slot(self):
        from core import consult
        fake = os.path.join(tempfile.mkdtemp(), 'codex')
        write_text(fake, '#!/bin/sh\necho "Logged in using an API key"\nexit 1\n')
        os.chmod(fake, 0o755)
        original = consult.codex_path
        consult.codex_path = lambda: fake
        try:
            jobs = consult.Consultations(name='Tester')
            jobs.start('packet text', 'demo')
            for _ in range(100):
                if jobs.status()['state'] != 'running':
                    break
                time.sleep(0.05)
            status = jobs.status()
            self.assertEqual(status['state'], 'failed')
            self.assertIn('ChatGPT', status['error'])     # API-key logins are refused
            jobs.start('packet text', 'again')              # the slot is free again
        finally:
            consult.codex_path = original

    def test_log_is_capped_while_running(self):
        import io
        from core import consult
        path = os.path.join(tempfile.mkdtemp(), 'log')
        consult._drain(io.BytesIO(b'x' * 5000), path, 1000)
        data = open(path, 'rb').read()
        self.assertTrue(data.startswith(b'x' * 1000) and b'4000 further bytes' in data)
        self.assertLess(len(data), 1100)

    def test_api_keys_removed_from_child(self):
        from core import consult
        os.environ['CODEX_API_KEY'] = 'x'
        try:
            env = consult.child_env()
        finally:
            del os.environ['CODEX_API_KEY']
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['MATHDESK_SKIP'], '1')


if __name__ == '__main__':
    unittest.main(verbosity=1)
