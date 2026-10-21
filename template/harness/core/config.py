"""Paths, settings and the access token shared by the hook, server and CLI.

Python 3.9 compatible: the hook imports this module under /usr/bin/python3.
"""
import json
import os
import secrets

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_ROOT = os.path.dirname(HARNESS_DIR)
STATIC_DIR = os.path.join(HARNESS_DIR, 'static')
STATE_DIR = os.environ.get('MATHDESK_STATE') or os.path.join(HUB_ROOT, '.hub', 'harness')
HOST = '127.0.0.1'
try:
    PORT = int(os.environ.get('MATHDESK_PORT') or 8741)
except ValueError:
    PORT = 8741

DEFAULTS = {
    'python': None,          # server interpreter, recorded by `mathdesk.py install`
    'autostart': True,       # hooks start the server when it is not running
    'autoopen': 'math',      # math | always | never: open a background tab if no viewer is connected
    'auto_pdf': True,        # snapshot every final response as a PDF
    'single_dollar': True,   # default for the page's $..$ toggle
    'reviewer_name': 'Codex',  # shown on the notebook's review button
    'consult_model': None,     # codex model for reviews; None uses Codex's default
    'consult_effort': 'high',  # model_reasoning_effort for reviews
}


def path(*parts):
    return os.path.join(STATE_DIR, *parts)


def ensure_state():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    for sub in ('spool', 'pdf', 'texcache'):
        os.makedirs(path(sub), mode=0o700, exist_ok=True)


def settings():
    merged = dict(DEFAULTS)
    try:
        with open(path('settings.json')) as handle:
            stored = json.load(handle)
        merged.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return merged


def save_settings(values):
    ensure_state()
    current = settings()
    current.update({k: v for k, v in values.items() if k in DEFAULTS})
    write_private(path('settings.json'), json.dumps(current, indent=2) + '\n')
    return current


def write_private(target, text):
    """Atomic write readable only by the user."""
    tmp = '%s.tmp-%d' % (target, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.replace(tmp, target)


def token():
    """Return the access token, creating it on first use."""
    ensure_state()
    target = path('token')
    try:
        with open(target) as handle:
            value = handle.read().strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    # Publish atomically without overwriting a concurrent winner, then reread.
    tmp = '%s.tmp-%d-%s' % (target, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(secrets.token_urlsafe(32) + '\n')
    try:
        os.link(tmp, target)
    except FileExistsError:
        pass
    finally:
        os.remove(tmp)
    with open(target) as handle:
        return handle.read().strip()


def base_url():
    return 'http://%s:%d' % (HOST, PORT)


def project_for_cwd(cwd):
    """Project name from a working directory; None at the hub root or outside."""
    if not cwd:
        return None
    real = os.path.realpath(cwd)
    projects = os.path.join(os.path.realpath(HUB_ROOT), 'projects') + os.sep
    if real.startswith(projects):
        return real[len(projects):].split(os.sep, 1)[0] or None
    return None


def inside_hub(cwd):
    if not cwd:
        return False
    root = os.path.realpath(HUB_ROOT)
    real = os.path.realpath(cwd)
    return real == root or real.startswith(root + os.sep)
