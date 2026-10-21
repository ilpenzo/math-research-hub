"""Hook events: one JSON file per event, written atomically, indexed by the server.

The hook side (event_from_hook, write_event) must stay fast and Python 3.9
compatible. The server side (SpoolIndex) rescans the spool, so a restart
recovers every event.
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

SERVICES = ('claude-code', 'codex', 'desk')   # desk: documents pushed with `mathdesk.py push`
MAX_TEXT = 2000000
MATH_RE = re.compile(r'\$\$|\$[^\s$]|\\\(|\\\[|\\begin\{')


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def has_math(text):
    return bool(MATH_RE.search(text or ''))


def event_from_hook(service, payload, inside, project_of):
    """Build an event from a hook payload, or None when there is nothing to keep.

    `inside(cwd)` and `project_of(cwd)` come from config so tests can point
    them at another root.
    """
    if service not in SERVICES or not isinstance(payload, dict):
        return None
    cwd = payload.get('cwd') or ''
    if not inside(cwd):
        return None
    name = payload.get('hook_event_name')
    if name == 'Stop':
        kind = 'final'
        text = payload.get('last_assistant_message')
        if not isinstance(text, str) or not text.strip():
            text = last_assistant_from_transcript(service, payload.get('transcript_path'))
    elif name == 'UserPromptSubmit':
        kind, text = 'prompt', payload.get('prompt')
    else:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    text = text[:MAX_TEXT]
    session = str(payload.get('session_id') or 'unknown')
    turn = payload.get('turn_id') or payload.get('prompt_id')
    stamp = now_iso()
    digest = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
    ident = hashlib.sha256('\0'.join(
        [service, session, str(turn), kind, stamp, digest]).encode()).hexdigest()[:20]
    return {
        'v': 1, 'id': ident, 'kind': kind, 'service': service,
        'session_id': session, 'turn_id': str(turn) if turn else None,
        'cwd': cwd, 'project': project_of(cwd),
        'transcript_path': payload.get('transcript_path') or None,
        'ts': stamp, 'text': text, 'text_sha256': digest,
        'model': payload.get('model') if isinstance(payload.get('model'), str) else None,
    }


def push_event(text, title=None, session='pushed', cwd=None):
    """An event for a document published to the desk by a person or an agent."""
    text = text[:MAX_TEXT]
    stamp = now_iso()
    digest = hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()
    ident = hashlib.sha256('\0'.join(['desk', session, stamp, digest]).encode()).hexdigest()[:20]
    return {'v': 1, 'id': ident, 'kind': 'final', 'service': 'desk', 'session_id': session,
            'turn_id': None, 'cwd': cwd, 'project': None, 'transcript_path': None, 'ts': stamp,
            'text': text, 'text_sha256': digest, 'model': None, 'title': (title or '')[:200] or None}


def write_event(event, spool_dir):
    day = os.path.join(spool_dir, event['ts'][:10].replace('-', ''))
    os.makedirs(day, mode=0o700, exist_ok=True)
    name = '%d-%d-%s.json' % (time.time_ns(), os.getpid(), event['id'][:8])
    tmp = os.path.join(day, '.tmp-' + name)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(event, handle, ensure_ascii=False)
    os.replace(tmp, os.path.join(day, name))
    return name


def last_assistant_from_transcript(service, transcript, window=262144):
    """Fallback when a Stop payload lacks the final text: the last assistant text in the tail.

    Tries a 256 KiB tail, then 8 MiB, so a single large final record is still recovered.
    """
    if not transcript or not os.path.isfile(transcript):
        return None
    for size in (window, 8 * 1024 * 1024):
        text = _scan_tail(service, transcript, size)
        if text or os.path.getsize(transcript) <= size:
            return text
    return None


def _scan_tail(service, transcript, window):
    try:
        with open(transcript, 'rb') as handle:
            start = max(0, os.path.getsize(transcript) - window)
            handle.seek(start)
            lines = handle.read().splitlines()
    except OSError:
        return None
    if start:
        lines = lines[1:]   # the first line may be cut by the seek
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if service == 'claude-code' and row.get('type') == 'assistant':
            parts = (row.get('message') or {}).get('content') or []
            text = '\n\n'.join(p.get('text', '') for p in parts
                               if isinstance(p, dict) and p.get('type') == 'text')
            if text.strip():
                return text
        if service == 'codex':
            payload = row.get('payload') or {}
            if payload.get('type') == 'task_complete' and payload.get('last_agent_message'):
                return payload['last_agent_message']
            if payload.get('type') == 'agent_message' and payload.get('message'):
                return payload['message']
            if payload.get('type') == 'message' and payload.get('role') == 'assistant':
                text = '\n\n'.join(p.get('text', '') for p in payload.get('content') or []
                                   if isinstance(p, dict) and p.get('type') == 'output_text')
                if text.strip():
                    return text
    return None


def summary(event):
    """Event without its text, for lists and long-poll notifications."""
    out = {k: v for k, v in event.items() if k != 'text'}
    first = next((line.strip() for line in event['text'].splitlines() if line.strip()), '')
    out['preview'] = first[:200]
    out['math'] = has_math(event['text'])
    out['chars'] = len(event['text'])
    return out


class SpoolIndex:
    """In-memory index of the spool directory, safe for concurrent readers."""

    def __init__(self, spool_dir):
        self.spool_dir = spool_dir
        self.lock = threading.Condition()
        self.events = []          # in arrival order; position + 1 is the sequence number
        self.by_id = {}
        self.seen_files = set()
        self.dedupe = set()
        self.listeners = []

    @property
    def seq(self):
        return len(self.events)

    def scan(self):
        """Pick up new files; returns the newly indexed events."""
        fresh = []
        try:
            days = sorted(d for d in os.listdir(self.spool_dir) if d.isdigit())
        except OSError:
            return fresh
        for day in days:
            folder = os.path.join(self.spool_dir, day)
            try:
                names = sorted(n for n in os.listdir(folder)
                               if n.endswith('.json') and not n.startswith('.'))
            except OSError:
                continue
            for name in names:
                key = day + '/' + name
                if key in self.seen_files:
                    continue
                self.seen_files.add(key)
                try:
                    with open(os.path.join(folder, name), encoding='utf-8') as handle:
                        event = json.load(handle)
                except (OSError, ValueError):
                    continue
                if not self._valid(event):
                    continue
                fresh.append(event)
        if fresh:
            fresh.sort(key=lambda e: e['ts'])
            with self.lock:
                added = []
                for event in fresh:
                    mark = (event['service'], event['session_id'], event.get('turn_id'),
                            event['kind'], event['text_sha256'])
                    if event['id'] in self.by_id or (event.get('turn_id') and mark in self.dedupe):
                        continue
                    self.dedupe.add(mark)
                    self.events.append(event)
                    event['seq'] = len(self.events)
                    self.by_id[event['id']] = event
                    added.append(event)
                if added:
                    self.lock.notify_all()
            for listener in self.listeners:
                for event in added:
                    listener(event)
            return added
        return fresh

    @staticmethod
    def _valid(event):
        return (isinstance(event, dict) and event.get('v') == 1
                and event.get('service') in SERVICES
                and event.get('kind') in ('final', 'prompt')
                and isinstance(event.get('text'), str) and isinstance(event.get('id'), str)
                and isinstance(event.get('ts'), str) and isinstance(event.get('session_id'), str))

    def wait(self, since, timeout):
        """Block until there are events after `since` or the timeout passes."""
        deadline = time.monotonic() + timeout
        with self.lock:
            while self.seq <= since:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.lock.wait(left)
            return [summary(e) for e in self.events[since:]], self.seq

    def get(self, ident):
        with self.lock:
            return self.by_id.get(ident)

    def session_events(self, service, session_id):
        with self.lock:
            return [e for e in self.events
                    if e['service'] == service and e['session_id'] == session_id]

    def sessions(self):
        """Sessions known from hook events, most recently active first."""
        table = {}
        with self.lock:
            for event in self.events:
                key = (event['service'], event['session_id'])
                entry = table.get(key)
                if entry is None:
                    entry = table[key] = {
                        'service': event['service'], 'session_id': event['session_id'],
                        'project': event.get('project'), 'cwd': event.get('cwd'),
                        'transcript_path': event.get('transcript_path'),
                        'started': event['ts'], 'title': None, 'finals': 0, 'prompts': 0}
                entry['updated'] = event['ts']
                entry['last_seq'] = event['seq']
                entry['transcript_path'] = event.get('transcript_path') or entry['transcript_path']
                if event['kind'] == 'prompt':
                    entry['prompts'] += 1
                    if entry['title'] is None:
                        entry['title'] = summary(event)['preview'][:120]
                else:
                    entry['finals'] += 1
                    entry['last_final'] = event['id']
                    if event.get('title'):
                        entry['title'] = event['title']
        return sorted(table.values(), key=lambda s: s['updated'], reverse=True)
