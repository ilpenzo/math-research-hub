"""Independent review of a frozen notebook packet by Codex, on request.

One job at a time, launched only by an explicit click and confirmation in the
page, and cancellable. Codex runs read-only and ephemeral, ignoring the user's
config.toml (no MCP servers or user hooks), on the user's ChatGPT login: API-key
variables are removed and the login is checked before launch. It works in an
empty private folder holding only the packet; its read-only sandbox does not
stop it reading other files, so the instructions say not to. The process
group is killed on timeout, cancel or any failure. The packet, its sha256 and
the (size-capped) reply and log are kept in .hub/harness/consult/, and the
reply is published to the desk.
"""
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import config, spool

INSTRUCTIONS = """You are {name}, asked for an independent review from MathDesk.
The packet below holds frozen excerpts with provenance, plus the conductor's
notes, which are kept separate. Review the packet alone: do not read other
local files or earlier verdicts, and name any outside result you rely on
instead of looking it up locally. Do not edit files. Check
each stated hypothesis and every step. Report the earliest genuine gap or
error first, with the exact excerpt and passage, then other issues by
severity. Separate what is proved, what is plausible and what is unsupported.
Agreement with earlier verdicts is not evidence; do not assume any. Use TeX
with \\( \\) and \\[ \\] delimiters for mathematics.

"""
MAX_PACKET = 200000
MAX_REPLY = 300000   # characters kept from the reply
MAX_LOG = 200000     # bytes of Codex output kept in the log
TIMEOUT = 45 * 60
API_KEY_VARS = ('OPENAI_API_KEY', 'CODEX_API_KEY')


def codex_path():
    for candidate in (shutil.which('codex'), '/opt/homebrew/bin/codex', '/usr/local/bin/codex'):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def child_env():
    env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}
    env['MATHDESK_SKIP'] = '1'
    return env


def subscription_login(codex):
    """True only when Codex reports a ChatGPT (subscription) login."""
    try:
        out = subprocess.run([codex, 'login', 'status'], capture_output=True, text=True,
                             timeout=20, env=child_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return 'chatgpt' in (out.stdout + out.stderr).lower()


class Consultations:
    def __init__(self, model=None, effort='high', name='Codex'):
        self.model, self.effort, self.name = model, effort or 'high', name or 'Codex'
        self.lock = threading.Lock()
        self.job = None    # {'id','title','state','started','finished','event','error','sha256'}
        self.proc = None

    def status(self):
        with self.lock:
            return dict(self.job) if self.job else {'state': 'idle'}

    def start(self, packet, title):
        packet = str(packet or '')
        if not packet.strip():
            raise ValueError('the packet is empty')
        if len(packet) > MAX_PACKET:
            raise ValueError('the packet is larger than %d characters' % MAX_PACKET)
        codex = codex_path()
        if not codex:
            raise ValueError('the codex CLI was not found')
        with self.lock:
            if self.job and self.job['state'] == 'running':
                raise ValueError('a review is already running')
            self.job = {'id': spool.now_iso(), 'title': (title or 'Notebook')[:120], 'state': 'running',
                        'started': spool.now_iso(), 'finished': None, 'event': None, 'error': None,
                        'chars': len(packet), 'sha256': hashlib.sha256(packet.encode('utf-8')).hexdigest()}
            job = self.job
        threading.Thread(target=self._run, args=(codex, packet, job), daemon=True).start()
        return dict(job)

    def cancel(self):
        with self.lock:
            proc, job = self.proc, self.job
            if not job or job['state'] != 'running':
                return {'state': job['state'] if job else 'idle'}
            job['cancelled'] = True
        if proc is not None:
            _kill_group(proc)
        return {'state': 'cancelling'}

    def _run(self, codex, packet, job):
        error, event, proc, scratch = None, None, None, None
        try:
            if not subscription_login(codex):
                raise RuntimeError('Codex is not logged in with ChatGPT; refusing to use API credentials')
            work = config.path('consult', time.strftime('%Y%m%dT%H%M%S') + '-' + job['sha256'][:8])
            os.makedirs(work, mode=0o700, exist_ok=True)
            config.write_private(os.path.join(work, 'packet.md'), packet)
            # The reviewer works in an empty private folder holding only the packet,
            # outside the hub, so it starts from the frozen excerpts alone.
            scratch = tempfile.mkdtemp(prefix='mathdesk-review-')
            shutil.copy(os.path.join(work, 'packet.md'), os.path.join(scratch, 'packet.md'))
            out, log_path = os.path.join(scratch, 'reply.md'), os.path.join(work, 'codex.log')
            command = [codex, 'exec', '--ignore-user-config'] + (['-m', self.model] if self.model else []) + [
                '-c', 'model_reasoning_effort=%s' % self.effort, '-s', 'read-only',
                '-C', scratch, '--skip-git-repo-check', '--ephemeral', '-o', out, '-']
            with self.lock:   # a cancel during the login check must stop the launch
                if job.get('cancelled'):
                    raise RuntimeError('cancelled')
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, env=child_env(), cwd=scratch,
                                        start_new_session=True)
                self.proc = proc
            drain = threading.Thread(target=_drain, args=(proc.stdout, log_path, MAX_LOG), daemon=True)
            drain.start()
            try:
                proc.stdin.write((INSTRUCTIONS.format(name=self.name) + packet).encode('utf-8'))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                error = 'the review timed out after %d minutes' % (TIMEOUT // 60)
            drain.join(5)
            if job.get('cancelled'):
                error = 'cancelled'
            elif proc.returncode not in (0, None) and not error:
                error = 'codex exited with status %d (log: %s)' % (proc.returncode, log_path)
            reply, cut = _read_capped(out, MAX_REPLY)
            if reply.strip():
                if cut:
                    reply += '\n\n*[The reply was longer than %d characters and is truncated here.]*' % MAX_REPLY
                config.write_private(os.path.join(work, 'reply.md'), reply)
            if reply.strip() and not job.get('cancelled'):
                header = ('*Independent review by %s (%s, %s effort, read-only) of the notebook packet "%s" '
                          '(sha256 %s), requested %s.*\n\n') % (self.name, self.model or 'default model', self.effort,
                                                                job['title'], job['sha256'][:16], job['started'])
                event = spool.push_event(header + reply, '%s review: %s' % (self.name, job['title']), 'reviews',
                                         config.HUB_ROOT)
                spool.write_event(event, config.path('spool'))
        except Exception as exc:  # the slot must always be released
            error = error or str(exc)
        finally:
            if proc is not None and proc.poll() is None:
                _kill_group(proc)       # never leave a review running without its job
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
            with self.lock:
                self.proc = None
                job['state'] = 'done' if event and not error else 'failed'
                job['error'] = error or (None if event else 'no reply was produced')
                job['finished'] = spool.now_iso()
                job['event'] = event['id'] if event else None


def _drain(stream, path, limit):
    """Copy the child's output to the log, keeping at most `limit` bytes on disk."""
    kept, dropped = 0, 0
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as log:
        for chunk in iter(lambda: stream.read(65536), b''):
            room = max(0, limit - kept)
            log.write(chunk[:room])
            kept += min(room, len(chunk))
            dropped += max(0, len(chunk) - room)
        if dropped:
            log.write(b'\n[%d further bytes of output were not kept]\n' % dropped)


def _read_capped(path, limit):
    """(text, truncated) for at most `limit` characters of a UTF-8 file."""
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            text = handle.read(limit + 1)
    except OSError:
        return '', False
    return text[:limit], len(text) > limit


def _kill_group(proc, grace=3.0):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

