"""Identify internal Prospector workers without changing their retained evidence.

Shared by capture, spool reads and native discovery. Only worker workspaces or
the opening prompt's exact template heading and introduction qualify; mentions in answers,
titles, tool output and later user prompts do not. Python 3.9 compatible.
"""
import json
import os
import re
import stat
from functools import lru_cache

HEAD_BYTES = 262144
_WORKDIR = re.compile(r'(?:^|/)(?:prospector/[^/]+/rounds/r[0-9]+/[^/]+|'
                      r'prospector-doctor-[0-9]+/(?:codex|claude))/work(?:/|$)')
_INJECTED = ('# AGENTS.md', '<environment_context>', '<user_instructions>',
             '<codex_internal_context', '<subagent_notification', '<recommended_plugins>',
             '<turn_aborted>', '<permissions instructions>')


def _worker_cwd(cwd):
    return isinstance(cwd, str) and bool(_WORKDIR.search(os.path.realpath(cwd)))


def _worker_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        return False
    first, _, rest = prompt.lstrip().partition('\n')
    rest = ' '.join(rest.split())
    return ((first.rstrip().startswith('# Prospector explorer: ')
             and rest.startswith((
                 'You are one of several independent explorers searching for a proof of the target below.',
                 'You are one of several independent explorers developing the mathematical target below.')))
            or (first.rstrip() == '# Prospector assay: try to kill these cards'
                and rest.startswith('Other explorers proposed the claim cards below. Your job is to find, quickly,'))
            or (first.rstrip() == '# Prospector dig: develop this idea into an argument'
                and rest.startswith('The claim card below survived an adversarial assay. Develop it into the most complete argument you can.'))
            or (first.rstrip() == '# Prospector dig: develop one missing implication'
                and rest.startswith("Develop the one missing implication selected in the conductor's note.")))


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(part['text'] for part in content
                       if isinstance(part, dict) and isinstance(part.get('text'), str)
                       and part.get('type') in ('text', 'Text', 'input_text'))
    return ''


@lru_cache(maxsize=512)
def _native_worker(service, path, mtime_ns, size):
    """Read at most 256 KiB, stopping at the first non-injected user prompt.

    The stat identity invalidates the cache when an initially empty transcript
    gains its prompt. Missing or unrecognised metadata keeps the session visible.
    """
    try:
        with open(path, 'rb') as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return None
    for raw in head.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        prompt = ''
        if service == 'claude-code':
            if _worker_cwd(row.get('cwd')):
                return True
            if row.get('type') == 'user' and not row.get('isMeta'):
                message = row.get('message')
                if isinstance(message, dict):
                    prompt = _text(message.get('content'))
        else:
            payload = row.get('payload')
            if not isinstance(payload, dict):
                continue
            kind = row.get('type')
            if kind == 'session_meta' and _worker_cwd(payload.get('cwd')):
                return True
            if kind == 'response_item' and payload.get('role') == 'user':
                prompt = _text(payload.get('content'))
            elif kind == 'event_msg':
                if payload.get('type') == 'user_message':
                    prompt = _text(payload.get('message'))
                elif payload.get('type') == 'item_completed':
                    item = payload.get('item')
                    if isinstance(item, dict) and item.get('type') == 'UserMessage':
                        prompt = _text(item.get('content'))
        if prompt.strip() and not prompt.lstrip().startswith(_INJECTED):
            return _worker_prompt(prompt)
    return None


def is_internal_session(service, cwd=None, prompt=None, transcript_path=None):
    """True only for an identified Prospector worker, never a pushed document."""
    if service not in ('codex', 'claude-code'):
        return False
    if _worker_cwd(cwd):
        return True
    if not isinstance(transcript_path, str) or not transcript_path.endswith('.jsonl'):
        return _worker_prompt(prompt)
    try:
        info = os.stat(transcript_path)
    except OSError:
        return _worker_prompt(prompt)
    if not stat.S_ISREG(info.st_mode):
        return _worker_prompt(prompt)
    native = _native_worker(service, transcript_path, info.st_mtime_ns, info.st_size)
    # A known ordinary opening prompt wins over a later pasted template.
    return native if native is not None else _worker_prompt(prompt)
