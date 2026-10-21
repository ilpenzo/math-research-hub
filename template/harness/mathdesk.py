"""MathDesk: rendered mathematics for Claude Code and Codex sessions in the hub.

  python3 harness/mathdesk.py open        start if needed and open the page
  python3 harness/mathdesk.py status      server, hooks and capability report
  python3 harness/mathdesk.py doctor      status plus end-to-end checks
  python3 harness/mathdesk.py install     add the hooks to Claude Code and Codex (backups kept)
  python3 harness/mathdesk.py uninstall   remove exactly those hooks
  python3 harness/mathdesk.py push FILE|- [--title T] [--session S]
                                          publish Markdown/TeX to the desk (agents may use this)
  python3 harness/mathdesk.py start|stop|serve
"""
import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import config  # noqa: E402

HOOK = os.path.join(HERE, 'hook.py')
CLAUDE_SETTINGS = os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'),
                               'settings.json')
CODEX_HOOKS = os.path.join(os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex'), 'hooks.json')
EVENTS = ('UserPromptSubmit', 'Stop')


def hook_command(service):
    return '/usr/bin/python3 %s --service %s' % (HOOK, service)


def listening():
    try:
        with socket.create_connection((config.HOST, config.PORT), timeout=0.2):
            return True
    except OSError:
        return False


def api(route, timeout=5):
    request = urllib.request.Request(config.base_url() + route, headers={
        'Authorization': 'Bearer ' + config.token()})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def start(quiet=False):
    if listening():
        return True
    config.ensure_state()
    settings = config.settings()
    python = settings.get('python') or sys.executable
    with open(config.path('server.log'), 'a') as out:
        subprocess.Popen([python, os.path.join(HERE, 'mathdesk.py'), 'serve'], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, cwd=HERE, close_fds=True, start_new_session=True)
    for _ in range(50):
        if listening():
            if not quiet:
                print('MathDesk started at %s' % config.base_url())
            return True
        time.sleep(0.1)
    print('MathDesk did not start; see %s' % config.path('server.log'))
    return False


def stop():
    try:
        with open(config.path('server.pid')) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        print('No server pid file.' + (' Something else holds port %d.' % config.PORT if listening() else ''))
        return
    # A stale pid file can name an unrelated process: check its command line first.
    command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True,
                             text=True).stdout
    if not command.strip().endswith(os.path.join(HERE, 'mathdesk.py') + ' serve'):
        print('The pid file is stale (process %d is not MathDesk); removing it.' % pid)
        try:
            os.remove(config.path('server.pid'))
        except OSError:
            pass
        return
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        print('Server was not running.')
        return
    for _ in range(30):
        if not listening():
            print('MathDesk stopped.')
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGTERM)
    print('MathDesk terminated.')


def open_page(route=''):
    if start(quiet=True):
        # The token travels in the fragment, which browsers never send over HTTP.
        url = '%s/#t=%s%s' % (config.base_url(), config.token(), '&' + route if route else '')
        subprocess.call(['/usr/bin/open', url])
        print('Opened %s/' % config.base_url())


# ---- hook installation -------------------------------------------------------

def _load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _backup(path):
    """Copy to a new, never-overwritten backup file."""
    if not os.path.exists(path):
        return None
    stamp = time.strftime('%Y%m%dT%H%M%S')
    for n in range(1000):
        target = '%s.mathdesk-backup-%s%s' % (path, stamp, '-%d' % n if n else '')
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with open(path, 'rb') as source, os.fdopen(fd, 'wb') as out:
            shutil.copyfileobj(source, out)
        shutil.copystat(path, target)
        return target
    raise RuntimeError('could not choose a backup name for %s' % path)


def _is_ours(hook):
    return HOOK in (hook.get('command') or '')


def _ours(group):
    return any(_is_ours(h) for h in group.get('hooks', []))


def _write_json(path, data):
    """Atomic write through symlinks (dotfile managers), keeping the file's mode."""
    real = os.path.realpath(path)
    mode = os.stat(real).st_mode & 0o777 if os.path.exists(real) else 0o600
    tmp = real + '.mathdesk-tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def install_into(path, service, remove=False):
    if not os.path.isdir(os.path.dirname(os.path.realpath(path))):
        return 'skipped: %s does not exist' % os.path.dirname(path)
    data = _load(path)
    hooks = data.setdefault('hooks', {})
    changed = False
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not remove:
            hooks[event] = groups
        present = [g for g in groups if _ours(g)]
        if remove and present:
            # Remove only our entries; other hooks sharing a group stay with the group's metadata.
            kept = []
            for group in groups:
                if _ours(group):
                    group = dict(group, hooks=[h for h in group.get('hooks', []) if not _is_ours(h)])
                    if not group['hooks']:
                        continue
                kept.append(group)
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
            changed = True
        elif not remove and not present:
            groups.append({'hooks': [{'type': 'command', 'command': hook_command(service), 'timeout': 10}]})
            changed = True
    if not hooks:
        data.pop('hooks', None)
    if changed:
        backup = _backup(path)
        _write_json(path, data)
        return backup or '(new file)'
    return None


def install(remove=False):
    if not remove:
        config.save_settings({'python': sys.executable})
    for path, service in ((CLAUDE_SETTINGS, 'claude-code'), (CODEX_HOOKS, 'codex')):
        backup = install_into(path, service, remove)
        if backup and backup.startswith('skipped'):
            print('%s (%s)' % (backup, service))
        elif backup:
            print('%s %s (backup %s)' % ('Removed from' if remove else 'Installed in', path, backup))
        else:
            print('%s: no change needed' % path)
    if not remove:
        print('Codex asks you to review and trust new hooks the next time it starts.')
        print('Server interpreter: %s' % sys.executable)


def hooks_present(path):
    hooks = _load(path).get('hooks', {})
    return {event: any(_ours(g) for g in hooks.get(event, [])) for event in EVENTS}


# ---- reporting -----------------------------------------------------------------

def report(deep=False):
    ok = True
    print('MathDesk at %s' % config.base_url())
    print('  hub root        %s' % config.HUB_ROOT)
    print('  state           %s' % config.STATE_DIR)
    for label, path in (('Claude Code', CLAUDE_SETTINGS), ('Codex', CODEX_HOOKS)):
        state = hooks_present(path)
        good = all(state.values())
        ok &= good
        print('  hooks %-10s %s' % (label, 'installed' if good else 'missing: ' + ', '.join(
            e for e, v in state.items() if not v)))
    if listening():
        try:
            info = api('/api/status')
            print('  server          running (pid %s, up %ss, %s events, python %s)'
                  % (info['pid'], info['uptime'], info['events'], info['python']))
            print('  hub data        %s' % ('ok' if info['hub'] else 'unavailable'))
            print('  transcripts     %s' % ('ok %s' % info.get('adapters') if info['transcripts'] else 'unavailable'))
            print('  PDF (Chrome)    %s' % (info['chrome'] or 'unavailable'))
            print('  LaTeX snippets  %s' % ('ok' if info['latex'] else 'unavailable'))
            for warning in info.get('warnings') or []:
                print('  warning         %s' % warning)
                ok = False
        except Exception as exc:
            print('  server          not answering correctly: %s' % exc)
            ok = False
    else:
        print('  server          not running (the next hub turn, or `mathdesk.py open`, starts it)')
    if deep:
        ok &= doctor_checks()
    return ok


def doctor_checks():
    ok = True
    print('Checks:')
    started = time.time()
    payload = json.dumps({'hook_event_name': 'Stop', 'cwd': '/', 'session_id': 'doctor',
                          'last_assistant_message': 'outside the hub'})
    result = subprocess.run(['/usr/bin/python3', HOOK, '--service', 'claude-code'], input=payload,
                            capture_output=True, text=True, timeout=10)
    elapsed = time.time() - started
    good = result.returncode == 0 and not result.stdout
    ok &= good
    print('  hook (outside hub, /usr/bin/python3): exit %d, stdout %d bytes, %.0f ms %s'
          % (result.returncode, len(result.stdout), elapsed * 1000, 'ok' if good else 'FAIL'))
    for tool in ('latex', 'dvisvgm', 'sandbox-exec'):
        found = shutil.which(tool) or (os.path.exists('/Library/TeX/texbin/' + tool) and tool)
        print('  %-14s %s' % (tool, found or 'missing'))
    return ok


def push(source, title, session):
    from core import spool
    text = sys.stdin.read() if source == '-' else open(source, encoding='utf-8').read()
    if not text.strip():
        print('Nothing to push.')
        return 1
    if not re.match(r'^[A-Za-z0-9._:-]{1,64}$', session):
        print('Session names use letters, digits and . _ : - only.')
        return 1
    title = title or (os.path.basename(source) if source != '-' else None)
    event = spool.push_event(text, title, session, os.getcwd())
    config.ensure_state()
    spool.write_event(event, config.path('spool'))
    if start(quiet=True):
        print('%s/#/t/%s' % (config.base_url(), event['id']))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['serve', 'start', 'stop', 'restart', 'status', 'doctor', 'open',
                                            'install', 'uninstall', 'push'])
    parser.add_argument('source', nargs='?', help='push: a file, or - for stdin')
    parser.add_argument('--title')
    parser.add_argument('--session', default='pushed')
    args = parser.parse_args(argv)
    if args.command == 'push':
        return push(args.source or '-', args.title, args.session)
    if args.command == 'serve':
        from core import server
        return server.serve()
    if args.command == 'start':
        return 0 if start() else 1
    if args.command == 'stop':
        stop()
        return 0
    if args.command == 'restart':
        stop()
        time.sleep(0.3)
        return 0 if start() else 1
    if args.command == 'open':
        open_page()
        return 0
    if args.command in ('install', 'uninstall'):
        install(remove=args.command == 'uninstall')
        return 0
    return 0 if report(deep=args.command == 'doctor') else 1


if __name__ == '__main__':
    sys.exit(main())
