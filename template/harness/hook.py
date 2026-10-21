"""MathDesk hook for Claude Code and Codex (UserPromptSubmit and Stop).

Usage in a hook config: /usr/bin/python3 <hub>/harness/hook.py --service claude-code|codex
Reads the hook payload from stdin, spools one event for sessions inside the hub,
and starts the server if it is not running. It never writes stdout (stdout of
UserPromptSubmit would reach the model), never blocks the turn and always exits 0.
"""
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def log(message):
    try:
        from core import config
        config.ensure_state()
        target = config.path('hook.log')
        if os.path.exists(target) and os.path.getsize(target) > 200000:
            os.replace(target, target + '.1')
        with open(target, 'a') as handle:
            handle.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S%z'), message))
    except Exception:
        pass


def server_listening(config):
    try:
        with socket.create_connection((config.HOST, config.PORT), timeout=0.05):
            return True
    except OSError:
        return False


def start_server(config, settings):
    python = settings.get('python') or sys.executable
    with open(config.path('server.log'), 'a') as out:
        subprocess.Popen([python, os.path.join(HERE, 'mathdesk.py'), 'serve'],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         cwd=HERE, close_fds=True, start_new_session=True)


def main(argv):
    if os.environ.get('MATHDESK_SKIP'):
        return  # a review launched by MathDesk itself; its reply is published directly
    service = argv[argv.index('--service') + 1] if '--service' in argv else None
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except ValueError as exc:
        log('unreadable %s payload: %s' % (service, exc))
        return
    from core import config, spool
    event = spool.event_from_hook(service, payload, config.inside_hub, config.project_for_cwd)
    if event is None:
        return
    config.ensure_state()
    spool.write_event(event, config.path('spool'))
    settings = config.settings()
    if settings.get('autostart') and not server_listening(config):
        start_server(config, settings)


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except BaseException as exc:  # a hook must never fail the turn
        try:
            log('hook error: %r' % (exc,))
        except BaseException:
            pass
    os._exit(0)
