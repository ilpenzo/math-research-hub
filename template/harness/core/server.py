"""MathDesk HTTP server: local only, token-authenticated, read-mostly.

The page, /print/ pages and /static/ carry no data and need no credentials.
Every data route needs `Authorization: Bearer TOKEN`. The page keeps the token
in origin-scoped localStorage (it arrives once in the URL fragment, which is
never sent over HTTP). No cookies are used: browsers do not scope cookies by
port, so a link to another localhost port could leak one. Print jobs get
short-lived read-only nonces instead of the token.

Routes:
  /                         the page
  /print/...                the same page in print mode (used by the PDF worker)
  /api/status               health, versions, capabilities
  /api/wait?since=N         long-poll for new hook events
  /api/sessions             sessions from hook events plus discovered transcripts
  /api/session/SVC/ID       one conversation (transcript items, or hook events)
  /api/event/ID             one hook event with its text
  /api/ids, /api/record/ID, /api/search, /api/docs, /api/doc?path=
  /api/pins (GET, POST)     the notebook
  /api/tex (POST)           sandboxed LaTeX for one formula; /tex/KEY.svg
  /api/consult (GET, POST)  independent Codex review of the notebook packet (explicit click only)
  /pdf/turn/ID, /pdf/session/SVC/ID, /pdf/doc?path=, /pdf/notebook
  /library/ID.pdf           a retained library PDF
"""
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, spool

VERSION = '1.0'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
# A print nonce may read only what a print page needs, and never write.
PRINT_ROUTES = ('/api/doc', '/api/pins', '/api/ids', '/api/tex-cached', '/api/print-failed')
PRINT_PREFIXES = ('/api/event/', '/api/session/', '/tex/')


def print_allowed(path):
    return path in PRINT_ROUTES or path.startswith(PRINT_PREFIXES)
SESSION_RE = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
KEY_RE = re.compile(r'^[0-9a-f]{64}$')


def optional(name):
    """Import a builder module, keeping the server alive if it is missing or broken."""
    try:
        module = __import__('core.' + name, fromlist=[name])
        return module, None
    except Exception as exc:  # report, never crash the page
        return None, '%s unavailable: %s' % (name, exc)


class Desk:
    """Server state shared by request threads."""

    def __init__(self):
        config.ensure_state()
        self.token = config.token()
        self.settings = config.settings()
        self.started = time.time()
        self.index = spool.SpoolIndex(config.path('spool'))
        self.warnings = []
        self.transcripts_mod, w1 = optional('transcripts')
        self.hubdata_mod, w2 = optional('hubdata')
        self.export_mod, w3 = optional('export')
        self.warnings += [w for w in (w1, w2, w3) if w]
        self.hub = None
        if self.hubdata_mod:
            try:
                self.hub = self.hubdata_mod.Hub(config.HUB_ROOT)
            except Exception as exc:
                self.warnings.append('hub data unavailable: %s' % exc)
        self.pdf = None
        if self.export_mod:
            try:
                self.pdf = self.export_mod.PdfWorker(config.path('pdf'), config.path('chrome-profile'))
                self.pdf.mode = 'cdp'   # only CDP waits for READY and refuses FAILED pages
            except Exception as exc:
                self.warnings.append('PDF worker unavailable: %s' % exc)
        self.consult = None
        try:
            from .consult import Consultations
            self.consult = Consultations(self.settings.get('consult_model'), self.settings.get('consult_effort'),
                                         self.settings.get('reviewer_name'))
        except Exception as exc:
            self.warnings.append('reviews unavailable: %s' % exc)
        self.nonces = {}            # print nonce -> (expiry, pdf key)
        self.print_failed = set()   # pdf keys whose print page reported a failure
        self.transcripts = {}
        self.transcripts_lock = threading.Lock()
        self.discovered = ([], 0.0)
        self.viewer_seen = 0.0
        self.waiters = 0              # open /api/wait requests: a page is watching
        self.last_open = 0.0
        self.pins_lock = threading.Lock()
        # At startup only events from the last 30 s (the hook that started us) count as new.
        self.booting = True
        self.boot_cutoff = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time() - 30))
        self.index.listeners.append(self.on_event)
        self.index.scan()
        self.booting = False
        threading.Thread(target=self.reconcile_pdfs, daemon=True).start()

    def reconcile_pdfs(self):
        """After a restart, queue snapshots that are missing (server down or crash mid-print)."""
        if not (self.settings.get('auto_pdf') and self.pdf):
            return
        finals = [e for e in self.index.events if e['kind'] == 'final'][-60:]
        for event in finals:
            key = 'turn-' + event['id']
            if self.pdf.status(key).get('state') in ('none', 'failed'):
                self.pdf.submit(key, self.print_url('/print/turn/' + event['id'], key))

    # ---- background work
    def watch(self):
        while True:
            try:
                self.index.scan()
            except Exception:
                traceback.print_exc()
            time.sleep(0.25)

    def on_event(self, event):
        if event['kind'] != 'final' or (self.booting and event['ts'] < self.boot_cutoff):
            return
        if self.settings.get('auto_pdf') and self.pdf:
            key = 'turn-' + event['id']
            self.pdf.submit(key, self.print_url('/print/turn/' + event['id'], key))
        mode = self.settings.get('autoopen')
        wants = mode == 'always' or (mode == 'math' and spool.has_math(event['text']))
        now = time.time()
        if wants and self.waiters == 0 and now - self.viewer_seen > 12 and now - self.last_open > 60:
            self.last_open = now
            target = '%s/#t=%s&/s/%s/%s' % (config.base_url(), self.token,
                                            event['service'], event['session_id'])
            subprocess.Popen(['/usr/bin/open', '-g', target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    def print_url(self, route, key):
        """URL for the PDF worker, with a read-only nonce limited by print_allowed()."""
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        self.nonces = {n: v for n, v in self.nonces.items() if v[0] > now}
        self.nonces[nonce] = (now + 3 * 3600, key)   # queued jobs may wait; see PRINT_ROUTES
        return '%s%s#t=%s' % (config.base_url(), route, nonce)

    # ---- sessions
    def transcript(self, service, path):
        if not self.transcripts_mod or not path or not allowed_transcript(service, path):
            return None
        with self.transcripts_lock:
            reader = self.transcripts.get(path)
            if reader is None:
                reader = self.transcripts[path] = self.transcripts_mod.Transcript(service, path)
        reader.refresh()
        return reader

    def discover(self):
        found, stamp = self.discovered
        if time.time() - stamp < 20 or not self.transcripts_mod:
            return found
        found = []
        for service in ('claude-code', 'codex'):
            try:
                for entry in self.transcripts_mod.discover(service, config.HUB_ROOT):
                    entry['service'] = service
                    found.append(entry)
            except Exception as exc:
                self.warnings.append('discovery failed for %s: %s' % (service, exc))
        self.discovered = (found, time.time())
        return found

    def sessions(self):
        table = {(s['service'], s['session_id']): dict(s, source='hooks') for s in self.index.sessions()}
        for entry in self.discover():
            key = (entry['service'], entry.get('session_id'))
            if not entry.get('session_id'):
                continue
            if key in table:
                table[key]['title'] = table[key].get('title') or entry.get('title')
                table[key]['transcript_path'] = table[key].get('transcript_path') or entry.get('transcript_path')
                continue
            table[key] = {
                'service': entry['service'], 'session_id': entry['session_id'],
                'project': config.project_for_cwd(entry.get('cwd')), 'cwd': entry.get('cwd'),
                'transcript_path': entry.get('transcript_path'), 'title': entry.get('title'),
                'started': entry.get('started'), 'updated': entry.get('updated') or entry.get('started'),
                'finals': 0, 'prompts': 0, 'source': 'history', 'kind': entry.get('kind')}
        return sorted(table.values(), key=lambda s: s.get('updated') or '', reverse=True)

    def session(self, service, session_id):
        info = next((s for s in self.sessions()
                     if s['service'] == service and s['session_id'] == session_id), None)
        if info is None:
            return None
        events = self.index.session_events(service, session_id)
        finals = {}   # text hash -> finals in arrival order, consumed one-to-one
        for event in events:
            if event['kind'] == 'final':
                finals.setdefault(event['text_sha256'], []).append(event)
        items, status = None, None
        reader = self.transcript(service, info.get('transcript_path'))
        if reader is not None:
            try:
                items = [dict(item) for item in reader.items()]
                status = reader.status()
            except Exception as exc:
                status = {'ok': False, 'warning': 'transcript adapter failed: %s' % exc}
                items = None
        if not items:
            items = [{'kind': 'user' if e['kind'] == 'prompt' else 'assistant', 'ts': e['ts'],
                      'text': e['text'], 'name': None, 'id': e['id']} for e in events]
            status = dict(status or {}, fallback=True)
        matched = set()
        for item in items:
            if item.get('kind') != 'assistant':
                continue
            digest = hashlib.sha256(item['text'].encode('utf-8', 'replace')).hexdigest()
            queue = finals.get(digest)
            if queue:
                event = queue.pop(0)
                matched.add(event['id'])
                item['event'] = self.event_card(event)
        # Final responses missing from the transcript (lag or adapter gap) are appended.
        for event in events:
            if event['kind'] == 'final' and event['id'] not in matched and not status.get('fallback'):
                items.append({'kind': 'assistant', 'ts': event['ts'], 'text': event['text'],
                              'name': 'hook', 'id': event['id'], 'event': self.event_card(event)})
        return {'session': info, 'items': items, 'status': status}

    def event_card(self, event):
        card = spool.summary(event)
        if self.pdf:
            card['pdf'] = self.pdf.status('turn-' + event['id'])
        return card

    # ---- pins
    def pins(self):
        try:
            with open(config.path('pins.json'), encoding='utf-8') as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def update_pins(self, body):
        with self.pins_lock:
            pins = self.pins()
            action = body.get('action')
            if action == 'add':
                item = body.get('item') or {}
                text = str(item.get('text') or '')[:400000]
                if not text.strip():
                    raise ValueError('empty pin')
                pins.append({
                    'id': hashlib.sha256(('%s%s' % (time.time(), text)).encode()).hexdigest()[:16],
                    'title': str(item.get('title') or '')[:200], 'text': text,
                    'source': {k: str(v)[:300] for k, v in (item.get('source') or {}).items()},
                    'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'pinned': spool.now_iso(), 'note': ''})
            elif action == 'remove':
                pins = [p for p in pins if p['id'] != body.get('id')]
            elif action == 'note':
                for p in pins:
                    if p['id'] == body.get('id'):
                        p['note'] = str(body.get('note') or '')[:20000]
            elif action == 'move':
                ids = [p['id'] for p in pins]
                k, step = body.get('id'), int(body.get('step') or 0)
                if k in ids and step in (-1, 1):
                    i = ids.index(k)
                    j = max(0, min(len(pins) - 1, i + step))
                    pins[i], pins[j] = pins[j], pins[i]
            else:
                raise ValueError('unknown action')
            config.write_private(config.path('pins.json'), json.dumps(pins, ensure_ascii=False, indent=1))
            return pins


def hub_pdf(rel):
    """Absolute path of a PDF inside the hub (never under .hub), or None."""
    if not rel or '\x00' in rel or os.path.isabs(rel) or not rel.lower().endswith('.pdf'):
        return None
    root = os.path.realpath(config.HUB_ROOT)
    real = os.path.realpath(os.path.join(root, rel))
    if not real.startswith(root + os.sep) or real.startswith(os.path.join(root, '.hub') + os.sep):
        return None
    if any(part.startswith('.') for part in os.path.relpath(real, root).split(os.sep)):
        return None
    return real if os.path.isfile(real) else None


def allowed_transcript(service, path):
    real = os.path.realpath(path)
    if not real.endswith('.jsonl'):
        return False
    if service == 'claude-code':
        base = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
        roots = [os.path.join(base, 'projects')]
    else:
        base = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
        roots = [os.path.join(base, 'sessions'), os.path.join(base, 'archived_sessions')]
    return any(real.startswith(os.path.realpath(r) + os.sep) for r in roots)


class Handler(BaseHTTPRequestHandler):
    server_version = 'MathDesk/' + VERSION
    sys_version = ''
    desk = None  # set by serve()

    def log_message(self, fmt, *args):
        if args and str(args[1]).startswith(('4', '5')):
            sys.stderr.write('%s %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    # ---- responses
    def send(self, status, body, ctype='application/json; charset=utf-8', headers=None):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        if not ctype.startswith(('text/css', 'application/javascript', 'font/')):
            self.send_header('Cache-Control', 'no-store')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def json(self, value, status=200):
        self.send(status, json.dumps(value, ensure_ascii=False))

    def fail(self, status, message):
        self.json({'error': message}, status)

    def file(self, path, ctype=None, headers=None):
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except OSError:
            return self.fail(404, 'not found')
        ctype = ctype or mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self.send(200, data, ctype, headers)

    # ---- security
    def host_ok(self):
        return self.headers.get('Host') in ('127.0.0.1:%d' % config.PORT, 'localhost:%d' % config.PORT)

    def credential(self):
        """'token', 'nonce' (read-only print job) or None."""
        header = self.headers.get('Authorization') or ''
        if not header.startswith('Bearer '):
            return None
        value = header[7:].strip()
        if _same(value, self.desk.token):
            return 'token'
        entry = self.desk.nonces.get(value)
        if entry and entry[0] > time.time():
            self.nonce_key = entry[1]
            return 'nonce'
        return None

    def post_ok(self):
        origin = self.headers.get('Origin')
        allowed = ('http://127.0.0.1:%d' % config.PORT, 'http://localhost:%d' % config.PORT)
        return self.headers.get('X-MathDesk') == '1' and origin in allowed

    def body(self, limit=1000000):
        length = int(self.headers.get('Content-Length') or 0)
        if length > limit:
            raise ValueError('request too large')
        return json.loads(self.rfile.read(length) or b'{}')

    # ---- dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.route('GET')
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self.fail(500, 'internal error: %s' % exc)

    def do_POST(self):
        try:
            self.route('POST')
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self.fail(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.fail(500, 'internal error: %s' % exc)

    def route(self, method):
        if not self.host_ok():
            return self.fail(421, 'unexpected Host header')
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path, urllib.parse.parse_qs(url.query)
        if path.startswith('/static/'):
            return self.static(path[len('/static/'):])
        if path == '/favicon.ico':
            return self.file(os.path.join(config.STATIC_DIR, 'favicon.svg'), 'image/svg+xml')
        if method == 'GET' and (path == '/' or path.startswith('/print/')):
            return self.file(os.path.join(config.STATIC_DIR, 'index.html'), 'text/html')
        self.nonce_key = None
        cred = self.credential()
        if cred is None:
            return self.fail(401, 'locked: run `python3 harness/mathdesk.py open`')
        if method == 'POST':
            if cred != 'token' or not self.post_ok():
                return self.fail(403, 'request refused')
            return self.post(path)
        if cred == 'nonce' and not print_allowed(path):
            return self.fail(403, 'print credentials cannot read this')
        if cred == 'nonce' and path == '/api/print-failed':
            self.desk.print_failed.add(self.nonce_key)
            return self.json({'ok': True})
        return self.get(path, query)

    def static(self, rel):
        base = os.path.realpath(config.STATIC_DIR)
        target = os.path.realpath(os.path.join(base, rel))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            return self.fail(404, 'not found')
        ctype = {'.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2',
                 '.json': 'application/json', '.svg': 'image/svg+xml'}.get(os.path.splitext(target)[1])
        self.file(target, ctype, {'Cache-Control': 'no-cache'})

    def get(self, path, query):
        desk = self.desk
        q = lambda k, d=None: (query.get(k) or [d])[0]
        if path == '/api/status':
            return self.json(status(desk))
        if path == '/api/wait':
            since = max(0, int(q('since', '0') or 0))
            with desk.pins_lock:
                desk.waiters += 1
            try:
                events, seq = desk.index.wait(since, min(25.0, float(q('timeout', '25'))))
            finally:
                with desk.pins_lock:
                    desk.waiters -= 1
                desk.viewer_seen = time.time()
            return self.json({'seq': seq, 'events': events})
        if path == '/api/sessions':
            return self.json({'sessions': desk.sessions(), 'seq': desk.index.seq})
        m = re.match(r'^/api/session/(claude-code|codex|desk)/([^/]+)$', path)
        if m:
            sid = urllib.parse.unquote(m.group(2))
            if not SESSION_RE.match(sid):
                return self.fail(400, 'bad session id')
            data = desk.session(m.group(1), sid)
            return self.json(data) if data else self.fail(404, 'unknown session')
        m = re.match(r'^/api/event/([0-9a-f]{20})$', path)
        if m:
            event = desk.index.get(m.group(1))
            if not event:
                return self.fail(404, 'unknown event')
            return self.json(dict(desk.event_card(event), text=event['text']))
        if path == '/api/pins':
            return self.json({'pins': desk.pins()})
        if path == '/api/consult':
            return self.json(desk.consult.status() if desk.consult else {'state': 'unavailable'})
        if path == '/api/tex-cached':
            # Formulas the user already typeset with LaTeX are reused everywhere (no compile here).
            if not desk.export_mod:
                return self.fail(503, 'LaTeX export unavailable')
            key = desk.export_mod.tex_key(q('tex') or '', q('display') == '1', load_macros())
            if key and os.path.isfile(os.path.join(config.path('texcache'), key + '.svg')):
                return self.json({'ok': True, 'url': '/tex/%s.svg' % key})
            return self.json({'ok': False})
        if path.startswith('/api/') and path.split('/')[2] in ('ids', 'record', 'search', 'docs', 'doc'):
            return self.hub_api(path, q)
        m = re.match(r'^/tex/([0-9a-f]{64})\.svg$', path)
        if m:
            return self.file(os.path.join(config.path('texcache'), m.group(1) + '.svg'), 'image/svg+xml',
                             {'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"})
        if path.startswith('/pdf/'):
            return self.pdf(path, q)
        if path == '/file':
            target = hub_pdf(q('path') or '')
            if not target:
                return self.fail(404, 'only PDF files inside the hub can be opened')
            return self.file(target, 'application/pdf',
                             {'Content-Disposition': 'inline; filename="%s"' % os.path.basename(target)})
        m = re.match(r'^/library/([A-Za-z0-9-]{2,24})\.pdf$', path)
        if m and desk.hub:
            target = desk.hub.library_pdf(m.group(1))
            if target:
                return self.file(target, 'application/pdf',
                                 {'Content-Disposition': 'inline; filename="%s.pdf"' % m.group(1)})
            return self.fail(404, 'no retained PDF for %s' % m.group(1))
        return self.fail(404, 'not found')

    def hub_api(self, path, q):
        try:
            return self._hub_api(path, q)
        except Exception as exc:
            if type(exc).__name__ == 'HubBusy':
                return self.fail(503, str(exc))
            raise

    def _hub_api(self, path, q):
        hub = self.desk.hub
        if hub is None:
            return self.fail(503, 'hub data unavailable')
        if path == '/api/ids':
            return self.json({'regex': hub.id_regex(), 'ids': hub.known_ids()})
        if path.startswith('/api/record/'):
            ident = urllib.parse.unquote(path[len('/api/record/'):])
            record = hub.record(ident) if re.match(r'^[A-Za-z0-9:-]{2,80}$', ident) else None
            return self.json(record) if record else self.fail(404, 'unknown id')
        if path == '/api/search':
            text = (q('q') or '').strip()[:200]
            if not text:
                return self.json({'results': []})
            return self.json({'results': hub.search(text, q('project') or None, 30)})
        if path == '/api/docs':
            return self.json({'documents': hub.documents(q('project') or None), 'projects': hub.projects()})
        if path == '/api/doc':
            doc = hub.read_document(q('path') or '')
            return self.json(doc) if doc else self.fail(404, 'document not available')
        return self.fail(404, 'not found')

    def pdf(self, path, q):
        desk = self.desk
        if not desk.pdf:
            return self.fail(503, 'PDF export needs Google Chrome or Brave')
        m = re.match(r'^/pdf/turn/([0-9a-f]{20})$', path)
        if m:
            event = desk.index.get(m.group(1))
            if not event:
                return self.fail(404, 'unknown event')
            key, route, name = 'turn-' + event['id'], '/print/turn/' + event['id'], 'response-' + event['id']
        elif re.match(r'^/pdf/session/(claude-code|codex|desk)/[A-Za-z0-9._:-]{1,128}$', path):
            _, _, _, service, sid = path.split('/')
            data = desk.session(service, sid)
            if not data:
                return self.fail(404, 'unknown session')
            version = hashlib.sha256(json.dumps([i.get('text') for i in data['items']]).encode()).hexdigest()[:16]
            key, route, name = 'session-%s-%s' % (sid, version), '/print/session/%s/%s' % (service, sid), 'session-' + sid[:12]
        elif path == '/pdf/doc':
            rel = q('path') or ''
            doc = desk.hub.read_document(rel) if desk.hub else None
            if not doc:
                return self.fail(404, 'document not available')
            version = hashlib.sha256(doc['text'].encode()).hexdigest()[:16]
            key = 'doc-%s-%s' % (hashlib.sha256(rel.encode()).hexdigest()[:16], version)
            route = '/print/doc?' + urllib.parse.urlencode({'path': rel})
            name = os.path.splitext(os.path.basename(rel))[0]
        elif path == '/pdf/notebook':
            version = hashlib.sha256(json.dumps(desk.pins()).encode()).hexdigest()[:16]
            key, route, name = 'notebook-' + version, '/print/notebook', 'notebook'
        else:
            return self.fail(404, 'not found')
        result = desk.pdf.status(key)
        if key in desk.print_failed and result.get('state') == 'ready':
            _remove(result.get('path'))      # a snapshot of a page that failed to render
            result = {'state': 'none'}
        desk.print_failed.discard(key)
        if result.get('state') != 'ready':
            result = desk.pdf.render_now(key, desk.print_url(route, key), timeout=45)
        if key in desk.print_failed:
            _remove(result.get('path'))
            return self.fail(500, 'PDF failed: the page could not be rendered')
        if result.get('state') != 'ready':
            return self.fail(500, 'PDF failed: %s' % (result.get('error') or 'unknown'))
        disposition = 'attachment' if q('download') else 'inline'
        return self.file(result['path'], 'application/pdf',
                         {'Content-Disposition': '%s; filename="%s.pdf"' % (disposition, re.sub(r'[^\w.-]', '_', name))})

    def post(self, path):
        desk = self.desk
        if path == '/api/pins':
            return self.json({'pins': desk.update_pins(self.body())})
        if path == '/api/consult':
            if not desk.consult:
                return self.fail(503, 'reviews unavailable')
            body = self.body(400000)
            if body.get('action') == 'cancel':
                return self.json(desk.consult.cancel())
            return self.json(desk.consult.start(body.get('packet'), body.get('title')))
        if path == '/api/tex':
            if not desk.export_mod:
                return self.fail(503, 'LaTeX export unavailable')
            body = self.body(20000)
            result = desk.export_mod.tex_to_svg(str(body.get('tex') or ''), bool(body.get('display')),
                                                load_macros(), config.path('texcache'))
            if result.get('ok'):
                result['url'] = '/tex/%s.svg' % result['key']
            result.pop('svg_path', None)
            return self.json(result)
        if path == '/api/settings':
            body = self.body(4000)
            allowed = {k: body[k] for k in ('autoopen', 'auto_pdf', 'single_dollar') if k in body}
            desk.settings = config.save_settings(allowed)
            return self.json(desk.settings)
        return self.fail(404, 'not found')


def _remove(path):
    try:
        if path:
            os.remove(path)
    except OSError:
        pass


def _same(a, b):
    import hmac
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def load_macros():
    try:
        with open(os.path.join(config.STATIC_DIR, 'macros.json'), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def status(desk):
    chrome = desk.export_mod.chrome_path() if desk.export_mod else None
    return {
        'version': VERSION, 'pid': os.getpid(), 'uptime': round(time.time() - desk.started),
        'python': sys.version.split()[0], 'hub_root': config.HUB_ROOT, 'events': desk.index.seq,
        'settings': desk.settings, 'warnings': desk.warnings[-10:], 'hub': desk.hub is not None,
        'chrome': chrome, 'latex': bool(_which('latex')) and bool(_which('dvisvgm')),
        'transcripts': bool(desk.transcripts_mod),
        'adapters': getattr(desk.transcripts_mod, 'ADAPTER_VERSION', None),
    }


def _which(name):
    for folder in os.environ.get('PATH', '').split(os.pathsep) + ['/Library/TeX/texbin']:
        candidate = os.path.join(folder, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Lets a restart bind while old connections sit in TIME_WAIT; macOS still
    # refuses a second listener on the same address.
    allow_reuse_address = True


def serve():
    os.environ['PATH'] = os.environ.get('PATH', '') + os.pathsep + '/Library/TeX/texbin'
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        httpd = Server((config.HOST, config.PORT), Handler)
    except OSError as exc:
        sys.stderr.write('MathDesk: port %d busy (%s); another server is probably running.\n'
                         % (config.PORT, exc))
        return 1
    desk = Desk()
    Handler.desk = desk
    config.write_private(config.path('server.pid'), '%d\n' % os.getpid())
    threading.Thread(target=desk.watch, daemon=True).start()
    sys.stderr.write('MathDesk %s serving %s (pid %d)\n' % (VERSION, config.base_url(), os.getpid()))
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if desk.consult:
            desk.consult.cancel()   # never leave a review running without its server
        try:
            os.remove(config.path('server.pid'))
        except OSError:
            pass
    return 0
