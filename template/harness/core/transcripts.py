"""Native transcript adapters for Claude Code and Codex (builder A).

Reads the CLIs' own JSONL transcripts strictly read-only and turns them into
ordered conversation items for the page:

    {"kind": "user" | "assistant" | "tool" | "notice", "ts": iso | None,
     "text": str, "name": str | None, "id": str | None}

Formats handled (verified against real files, October 2026):

* Claude Code 2.1.28x-2.1.30x: ~/.claude/projects/<slug(cwd)>/<session>.jsonl.
  One content block per assistant line; one API message spans several lines
  sharing message.id with apiBlockIndex 0..n.  Genuine prompts carry
  origin.kind == "human"; older lines without origin fields fall back to
  prefix exclusions.
* Codex rollouts ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, three
  generations: event_msg/item_completed (0.150+), event_msg user_message /
  agent_message (<= 0.157), and bare response_item messages as a last resort.

Python 3.9 compatible, stdlib only.  Never writes anywhere.
"""
import glob
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

ADAPTER_VERSION = {"claude-code": "claude-jsonl/1", "codex": "codex-rollout/1"}

GIST_MAX = 160
TITLE_MAX = 120
_READ_CHUNK = 1 << 22
_HEAD_FINGERPRINT = 64
_DISCOVER_BUDGET = 0.85      # seconds of wall time per discover() call
_SQLITE_BUDGET = 0.3

__all__ = ['ADAPTER_VERSION', 'Transcript', 'discover', 'claude_home', 'codex_home']


# ---------------------------------------------------------------- helpers

def claude_home():
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')


def codex_home():
    return os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')


def _service(name):
    if name in ('claude-code', 'claude'):
        return 'claude-code'
    if name == 'codex':
        return 'codex'
    raise ValueError('unknown service %r' % (name,))


def _one_line(text, limit=GIST_MAX, mark_more=True):
    """First non-empty line, whitespace collapsed, at most `limit` characters.

    A trailing ellipsis marks dropped lines unless mark_more is false.
    """
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ''
    line = ' '.join(lines[0].split())
    if len(lines) > 1 and mark_more:
        line += ' …'
    if len(line) > limit:
        line = line[:limit - 1].rstrip() + '…'
    return line


def _rel(path, cwd):
    """Shorten an absolute path to cwd-relative when it lies inside cwd."""
    if not isinstance(path, str) or not cwd or not path.startswith('/'):
        return path
    base = cwd.rstrip('/') + '/'
    if path.startswith(base) and len(path) > len(base):
        return path[len(base):]
    return path


def _iso_from_epoch(seconds):
    if seconds is None:
        return None
    try:
        stamp = datetime.fromtimestamp(float(seconds), timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (stamp.microsecond // 1000)


def _first_short_string(mapping, limit=200):
    if not isinstance(mapping, dict):
        return ''
    fallback = ''
    for value in mapping.values():
        if isinstance(value, str) and value.strip():
            if len(value) <= limit:
                return value
            if not fallback:
                fallback = value
    return fallback


def _inside(path, root_real):
    if not path:
        return False
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


def _item(kind, text, ts=None, name=None, ident=None):
    return {'kind': kind, 'ts': ts, 'text': text, 'name': name, 'id': ident}


# ---------------------------------------------------------------- Claude Code

_CLAUDE_KNOWN = frozenset([
    'user', 'assistant', 'attachment', 'system', 'summary', 'progress',
    'last-prompt', 'ai-title', 'custom-title', 'mode', 'permission-mode',
    'atis-latch', 'cost-state', 'queue-operation', 'file-history-snapshot',
    'file-history-delta', 'api-request', 'api-request-shape', 'api-request-blob',
    'tag', 'agent-name', 'agent-color', 'agent-setting', 'pr-link',
    'worktree-state', 'content-replacement', 'attribution-snapshot', 'continued-in',
])
_CLAUDE_GIST_KEYS = {
    'Bash': 'command', 'Read': 'file_path', 'Edit': 'file_path', 'MultiEdit': 'file_path',
    'Write': 'file_path', 'NotebookEdit': 'notebook_path', 'NotebookRead': 'notebook_path',
    'Grep': 'pattern', 'Glob': 'pattern', 'Agent': 'description', 'Task': 'description',
    'WebSearch': 'query', 'WebFetch': 'url', 'ToolSearch': 'query', 'Skill': 'skill',
    'LS': 'path', 'BashOutput': 'bash_id', 'KillShell': 'shell_id', 'TaskStop': 'task_id',
}
_PATH_KEYS = frozenset(['file_path', 'notebook_path', 'path'])
_CMD_NAME_RE = re.compile(r'<command-name>\s*(.*?)\s*</command-name>', re.S)
_CMD_ARGS_RE = re.compile(r'<command-args>(.*?)</command-args>', re.S)
_LEGACY_TAG_RE = re.compile(r'^<([A-Za-z][\w-]*)(?:\s[^>]*)?>.*</\1>', re.S)
_LEGACY_PREFIXES = ('Caveat: The messages below were generated by the user while running local commands',
                    'This session is being continued from a previous conversation')


def _claude_tool_gist(name, args, cwd):
    if not isinstance(args, dict):
        return _one_line(args if isinstance(args, str) else '')
    key = _CLAUDE_GIST_KEYS.get(name)
    value = args.get(key) if key else None
    if not isinstance(value, str) or not value.strip():
        value = _first_short_string(args)
        key = next((k for k, v in args.items() if v is value), None)
    if key in _PATH_KEYS:
        value = _rel(value, cwd)
    return _one_line(value)


def _claude_user(line):
    """Classify a Claude `user` line: ('prompt', text) | ('notice', text) | ('tool_result', None) | (None, None)."""
    message = line.get('message') or {}
    content = message.get('content') if isinstance(message, dict) else None
    images = 0
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'tool_result':
                return ('tool_result', None)
            if kind == 'text' and isinstance(block.get('text'), str):
                parts.append(block['text'])
            elif kind in ('image', 'document'):
                images += 1
        text = '\n\n'.join(p for p in parts if p)
    else:
        return (None, None)
    if line.get('isMeta') or line.get('isCompactSummary') or line.get('isVisibleInTranscriptOnly'):
        return (None, None)
    origin = line.get('origin')
    has_origin_fields = isinstance(origin, dict) or 'promptSource' in line or 'turnOrigin' in line
    if isinstance(origin, dict):
        if origin.get('kind') != 'human':
            return (None, None)
    elif line.get('promptSource') in ('system', 'sdk'):
        return (None, None)
    elif 'promptSource' not in line and 'turnOrigin' in line and line.get('turnOrigin') != 'human':
        return (None, None)
    stripped = text.lstrip()
    if stripped.startswith('[Request interrupted'):
        return ('notice', 'interrupted')
    if stripped.startswith('<command-name>') or stripped.startswith('<command-message>'):
        match = _CMD_NAME_RE.search(stripped)
        if not match:
            return (None, None)
        args = _CMD_ARGS_RE.search(stripped)
        command = match.group(1).strip()
        if args and args.group(1).strip():
            command += ' ' + args.group(1).strip()
        return ('notice', _one_line(command))
    if stripped.startswith('<local-command-'):
        return (None, None)
    if not has_origin_fields:
        if _LEGACY_TAG_RE.match(stripped) or stripped.startswith(_LEGACY_PREFIXES):
            return (None, None)
    if not text.strip() and not images:
        return (None, None)
    if images:
        marker = '[%d image%s attached]' % (images, '' if images == 1 else 's')
        text = (text + '\n\n' + marker) if text.strip() else marker
    return ('prompt', text)


class _ClaudeParser:
    def __init__(self):
        self.out = []
        self.seen = set()
        self.open = None            # {'mid', 'item', 'parts'} for the assistant text being joined
        self.unknown_types = {}
        self.unknown = 0
        self.unknown_lines = 0
        self.tool_results = 0
        self.version = None
        self.cwd = None
        self.session_id = None
        self.title = None
        self.custom_title = False
        self.lineno = 0

    def items(self):
        return self.out

    def feed(self, line):
        kind = line.get('type')
        uid = line.get('uuid')
        if uid:
            if uid in self.seen:
                return True
            self.seen.add(uid)
        if kind not in _CLAUDE_KNOWN:
            self.unknown += 1
            self.unknown_lines += 1
            label = str(kind)[:60]
            if label not in self.unknown_types:
                self.unknown_types[label] = 0
                self.out.append(_item('notice', 'unknown line type: %s' % label,
                                        line.get('timestamp'), 'unknown', uid or 'line:%d' % self.lineno))
                self.open = None
            self.unknown_types[label] += 1
            return False
        if line.get('version'):
            self.version = line['version']
        if line.get('cwd'):
            self.cwd = line['cwd']
        if line.get('sessionId') and not self.session_id:
            self.session_id = line['sessionId']
        if kind in ('ai-title', 'custom-title'):
            title = line.get('aiTitle') or line.get('customTitle') or line.get('title')
            if isinstance(title, str) and title.strip() and (kind == 'custom-title' or not self.custom_title):
                self.title = _one_line(title, TITLE_MAX, False)
                self.custom_title = self.custom_title or kind == 'custom-title'
            return True
        if line.get('isSidechain'):
            return True             # legacy in-file subagent traffic
        if kind == 'continued-in':
            target = line.get('continuedInSessionId')
            self.open = None
            self.out.append(_item('notice', 'continued in session %s' % (target or '?'),
                                  line.get('timestamp'), 'continued', target or 'line:%d' % self.lineno))
            return True
        if kind == 'assistant':
            self._assistant(line)
        elif kind == 'user':
            self._user(line)
        elif kind == 'system':
            if line.get('subtype') == 'compact_boundary':
                self.open = None
                self.out.append(_item('notice', 'context compacted', line.get('timestamp'),
                                        'compaction', uid))
        return True

    def _user(self, line):
        verdict, text = _claude_user(line)
        if verdict == 'tool_result':
            self.tool_results += 1
            return
        if verdict is None:
            return
        self.open = None
        if verdict == 'prompt':
            self.out.append(_item('user', text, line.get('timestamp'), None, line.get('uuid')))
        else:
            name = 'interrupted' if text == 'interrupted' else 'command'
            self.out.append(_item('notice', text, line.get('timestamp'), name, line.get('uuid')))

    def _assistant(self, line):
        message = line.get('message') or {}
        if not isinstance(message, dict):
            return
        mid = message.get('id') or line.get('requestId')
        blocks = message.get('content')
        if isinstance(blocks, str):
            blocks = [{'type': 'text', 'text': blocks}]
        if not isinstance(blocks, list):
            return
        ts = line.get('timestamp')
        index = line.get('apiBlockIndex')
        if line.get('isApiErrorMessage'):
            text = ' '.join(b.get('text', '') for b in blocks if isinstance(b, dict))
            self.open = None
            self.out.append(_item('notice', _one_line(text) or 'API error', ts, 'api-error',
                                    line.get('uuid')))
            return
        cwd = line.get('cwd') or self.cwd
        for pos, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = block.get('type')
            order = (index if isinstance(index, int) else 0, pos)
            if btype == 'text':
                text = block.get('text')
                if not isinstance(text, str) or not text.strip():
                    continue
                current = self.open
                if (current is not None and mid is not None and current['mid'] == mid
                        and self.out and self.out[-1] is current['item']):
                    current['parts'].append((order, text))
                    current['parts'].sort(key=lambda part: part[0])
                    current['item']['text'] = '\n\n'.join(part[1] for part in current['parts'])
                else:
                    item = _item('assistant', text, ts, None, line.get('uuid'))
                    self.out.append(item)
                    self.open = {'mid': mid, 'item': item, 'parts': [(order, text)]}
            elif btype in ('tool_use', 'server_tool_use'):
                self.open = None
                name = block.get('name') or 'tool'
                self.out.append(_item('tool', _claude_tool_gist(name, block.get('input'), cwd),
                                        ts, name, block.get('id') or line.get('uuid')))
            # thinking / redacted_thinking and anything else: omitted, does not break a join


# ---------------------------------------------------------------- Codex

_CODEX_KNOWN = frozenset([
    'session_meta', 'event_msg', 'response_item', 'compacted', 'turn_context',
    'world_state', 'token_usage_record', 'inter_agent_communication_metadata',
])
_CODEX_EVENTS_KNOWN = frozenset([
    'item_completed', 'item_started', 'item_updated', 'task_started', 'task_complete',
    'turn_aborted', 'token_count', 'thread_settings_applied', 'user_message',
    'agent_message', 'agent_reasoning', 'agent_reasoning_raw_content', 'context_compacted',
    'thread_goal_updated', 'thread_rolled_back', 'web_search_begin', 'web_search_end',
    'patch_apply_begin', 'patch_apply_end', 'exec_command_begin', 'exec_command_end',
    'mcp_tool_call_begin', 'mcp_tool_call_end', 'sub_agent_activity', 'turn_diff',
    'plan_update', 'error', 'stream_error', 'warning', 'background_event',
    'view_image_tool_call', 'entered_review_mode', 'exited_review_mode',
])
_CODEX_ITEMS_KNOWN = frozenset([
    'UserMessage', 'AgentMessage', 'Reasoning', 'CommandExecution', 'FileChange',
    'SubAgentActivity', 'CollabAgentToolCall', 'McpToolCall', 'Extension', 'ImageView',
    'ContextCompaction', 'FunctionCallOutput', 'WebSearch', 'Plan', 'TodoList',
])
_CODEX_INJECTED_PREFIXES = (
    '<environment_context>', '<user_instructions>', '# AGENTS.md', '<codex_internal_context',
    '<subagent_notification', '<recommended_plugins>', '<turn_aborted>', '<permissions instructions>',
)
_CODEX_GENUINE_KINDS = frozenset(['user.text', 'user.image'])
_CODEX_PHASE = {'commentary': 'commentary', 'final_answer': 'final', 'final': 'final'}
_PATCH_FILE_RE = re.compile(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$', re.M)
_GENERIC_TAG_RE = re.compile(r'^<[A-Za-z][\w-]*[\s>]')


def _codex_kind(meta):
    source = meta.get('thread_source')
    if isinstance(source, str) and source:
        return source
    origin = meta.get('source')
    if isinstance(origin, dict) and 'subagent' in origin:
        sub = origin.get('subagent')
        if isinstance(sub, dict) and sub.get('other') == 'guardian':
            return 'guardian_review'
        return 'subagent'
    return 'user'


_QUESTION_REPLY_RE = re.compile(r'^\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*$', re.S)


def _codex_question_reply(text):
    """Readable form of a typed answer to a Codex question, or None if `text` is not one."""
    match = _QUESTION_REPLY_RE.match(text)
    if not match:
        return None
    try:
        answers = json.loads(match.group(1))
    except ValueError:
        return match.group(1)
    if not isinstance(answers, list):
        return match.group(1)
    blocks = []
    for entry in answers:
        if not isinstance(entry, dict):
            continue
        question = entry.get('question')
        answer = entry.get('answer')
        if isinstance(answer, list):
            answer = ', '.join(str(a) for a in answer)
        quoted = '\n'.join('> ' + ln for ln in str(question).splitlines()) + '\n\n' if question else ''
        blocks.append(quoted + ('' if answer is None else str(answer)))
    return '\n\n'.join(blocks) or match.group(1)


def _codex_injected(text):
    return text.lstrip().startswith(_CODEX_INJECTED_PREFIXES)


def _codex_command(command):
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if len(parts) >= 3 and parts[1] in ('-lc', '-c'):
            return parts[2]
        return ' '.join(parts)
    return command if isinstance(command, str) else ''


def _codex_text_parts(content, text_key='text'):
    if isinstance(content, str):
        return content, 0
    texts, images = [], 0
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get(text_key), str):
            texts.append(part[text_key])
        elif 'image' in str(part.get('type', '')):
            images += 1
    return ''.join(texts), images


def _with_images(text, images):
    if not images:
        return text
    marker = '[%d image%s attached]' % (images, '' if images == 1 else 's')
    return (text.rstrip() + '\n\n' + marker) if text.strip() else marker


def _patch_paths(patch, cwd):
    paths = [_rel(p.strip(), cwd) for p in _PATCH_FILE_RE.findall(patch or '')]
    return ', '.join(paths)


def _codex_call_gist(name, args, cwd):
    """(tool name, gist) for an old-generation response_item call."""
    if name == 'apply_patch':
        text = args if isinstance(args, str) else (args or {}).get('input', '')
        return 'edit', _one_line(_patch_paths(text, cwd))
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            args = parsed
        else:
            return name, _one_line(args)
    if not isinstance(args, dict):
        return name, ''
    if name in ('exec_command', 'shell', 'local_shell', 'container.exec'):
        command = args.get('cmd', args.get('command'))
        return 'shell', _one_line(_codex_command(command))
    if name == 'update_plan':
        plan = args.get('plan') or []
        return name, '%d steps' % len(plan) if isinstance(plan, list) else ''
    for key in ('message', 'task_name', 'target', 'query', 'path', 'url', 'chars'):
        if isinstance(args.get(key), str) and args[key].strip():
            return name, _one_line(_rel(args[key], cwd))
    return name, _one_line(_first_short_string(args))


class _CodexParser:
    STREAMS = ('items', 'events', 'responses')

    def __init__(self):
        self.meta = None
        self.kind = 'user'
        self.cwd = None
        self.version = None
        self.last_ordinal = None
        self.duplicates = 0
        self.unknown = 0
        self.unknown_lines = 0
        self.unknown_types = {}
        self.unknown_subtypes = {}
        self.streams = {'items': [], 'events': [], 'responses': []}
        self.mode = None            # 'items' | 'events' once the generation is evident
        self.lineno = 0
        self.ordinal = None

    def _lid(self):
        """Stable id for an item without a native one: the ordinal, else the line number."""
        if self.ordinal is not None:
            return 'ord:%d' % self.ordinal
        return 'line:%d' % self.lineno

    # -- stream bookkeeping
    def items(self):
        if self.mode:
            return self.streams[self.mode]
        return self.streams['responses']

    def _add(self, stream, item):
        target = self.streams.get(stream)
        if target is None:
            return
        carry = getattr(self, '_carry', None)
        if stream == 'items' and carry:
            key = (item['kind'], item['text'])
            if key in carry:
                carry.remove(key)
                return
            self._carry = None
        target.append(item)

    def _add_all(self, item):
        for target in self.streams.values():
            if target is not None:
                target.append(item)

    def _decide(self, mode):
        if mode == 'items' and self.mode != 'items':
            # A resumed old rollout switches from event records to item_completed:
            # keep the history already read from events (new rollouts have none).
            if self.streams.get('events'):
                self.streams['items'] = list(self.streams['events'])
                # The turn in progress may appear in both forms; the first new
                # items that repeat the carried tail are dropped once each.
                self._carry = [(i['kind'], i['text']) for i in self.streams['items'][-6:]]
            self.mode = 'items'
            self.streams['events'] = None
            self.streams['responses'] = None
        elif mode == 'events' and self.mode is None:
            self.mode = 'events'
            self.streams['responses'] = None

    def _subtype_unknown(self, label):
        self.unknown += 1
        self.unknown_subtypes[label] = self.unknown_subtypes.get(label, 0) + 1

    # -- lines
    def feed(self, line):
        ordinal = line.get('ordinal')
        self.ordinal = ordinal if isinstance(ordinal, int) else None
        if isinstance(ordinal, int):
            if self.last_ordinal is not None and ordinal <= self.last_ordinal:
                self.duplicates += 1
                return True
            self.last_ordinal = ordinal
        kind = line.get('type')
        payload = line.get('payload')
        if not isinstance(payload, dict):
            payload = {}
        ts = line.get('timestamp')
        if kind not in _CODEX_KNOWN:
            self.unknown += 1
            self.unknown_lines += 1
            label = str(kind)[:60]
            if label not in self.unknown_types:
                self.unknown_types[label] = 0
                self._add_all(_item('notice', 'unknown line type: %s' % label, ts, 'unknown', self._lid()))
            self.unknown_types[label] += 1
            return False
        if kind == 'session_meta':
            if self.meta is None:
                self.meta = payload
                self.kind = _codex_kind(payload)
                self.cwd = payload.get('cwd')
                self.version = payload.get('cli_version')
        elif kind == 'event_msg':
            self._event(payload, ts)
        elif kind == 'response_item':
            self._response(payload, ts)
        elif kind == 'compacted':
            self._add('responses', _item('notice', 'context compacted', ts, 'compaction', self._lid()))
        elif kind == 'turn_context':
            if not self.cwd and payload.get('cwd'):
                self.cwd = payload['cwd']
        return True

    def _event(self, payload, ts):
        sub = payload.get('type')
        if sub not in _CODEX_EVENTS_KNOWN:
            self._subtype_unknown('event_msg/%s' % str(sub)[:40])
            return
        if sub == 'item_completed':
            item = payload.get('item')
            if isinstance(item, dict):
                self._decide('items')
                self._completed(item, ts)
        elif sub == 'user_message':
            self._decide('events')
            message = payload.get('message')
            text = message if isinstance(message, str) else ''
            images = len(payload.get('images') or []) + len(payload.get('local_images') or [])
            if self.kind == 'user' and (text.strip() or images) and not _codex_injected(text):
                reply = _codex_question_reply(text)
                if reply is not None:
                    self._add('events', _item('user', reply, ts, 'answer', self._lid()))
                else:
                    self._add('events', _item('user', _with_images(text, images), ts, None, self._lid()))
        elif sub == 'agent_message':
            self._decide('events')
            message = payload.get('message')
            if isinstance(message, str) and message.strip():
                self._add('events', _item('assistant', message, ts, _CODEX_PHASE.get(payload.get('phase')),
                                          self._lid()))
        elif sub == 'turn_aborted':
            reason = payload.get('reason')
            text = 'interrupted' if reason in (None, 'interrupted') else 'turn aborted: %s' % _one_line(reason, 80)
            self._add_all(_item('notice', text, ts, 'interrupted', payload.get('turn_id') or self._lid()))
        elif sub == 'context_compacted':
            self._add('events', _item('notice', 'context compacted', ts, 'compaction', self._lid()))
        elif sub == 'thread_rolled_back':
            turns = payload.get('num_turns')
            self._add_all(_item('notice', 'rolled back %s turn(s)' % turns, ts, 'rollback', self._lid()))

    def _completed(self, item, ts):
        itype = item.get('type')
        ident = item.get('id') or self._lid()
        cwd = self.cwd
        if itype not in _CODEX_ITEMS_KNOWN:
            self._subtype_unknown('item/%s' % str(itype)[:40])
            return
        if itype == 'UserMessage':
            text, images = _codex_text_parts(item.get('content'))
            if not (text.strip() or images) or _codex_injected(text):
                return
            if not item.get('client_id') and self.kind != 'user':
                return              # task text handed to a subagent / guardian, not typed
            reply = _codex_question_reply(text)
            if reply is not None:
                self._add('items', _item('user', reply, ts, 'answer', ident))
            else:
                self._add('items', _item('user', _with_images(text, images), ts, None, ident))
        elif itype == 'AgentMessage':
            text, _ = _codex_text_parts(item.get('content'))
            if text.strip():
                self._add('items', _item('assistant', text, ts, _CODEX_PHASE.get(item.get('phase')), ident))
        elif itype == 'CommandExecution':
            self._add('items', _item('tool', _one_line(_codex_command(item.get('command'))), ts, 'shell', ident))
        elif itype == 'FileChange':
            changes = item.get('changes')
            paths = list(changes) if isinstance(changes, dict) else []
            if isinstance(changes, list):
                paths = [c.get('path', '') for c in changes if isinstance(c, dict)]
            gist = ', '.join(_rel(p, cwd) for p in paths if p)
            self._add('items', _item('tool', _one_line(gist), ts, 'edit', ident))
        elif itype == 'McpToolCall':
            label = '%s.%s' % (item.get('server') or 'mcp', item.get('tool') or '?')
            args = item.get('arguments')
            extra = _first_short_string(args) if isinstance(args, dict) else ''
            self._add('items', _item('tool', _one_line((label + ' ' + extra).strip()), ts, 'mcp', ident))
        elif itype == 'CollabAgentToolCall':
            receivers = item.get('receiver_agents') or item.get('receiver_thread_ids') or []
            names = []
            for rec in receivers if isinstance(receivers, list) else []:
                names.append(rec if isinstance(rec, str) else str((rec or {}).get('agent_path')
                                                                  or (rec or {}).get('name') or rec))
            gist = ('%s %s' % (item.get('tool') or '', ', '.join(names))).strip()
            self._add('items', _item('tool', _one_line(gist), ts, 'agent', ident))
        elif itype == 'SubAgentActivity':
            gist = '%s %s' % (item.get('kind') or '', item.get('agent_path') or item.get('agent_thread_id') or '')
            self._add('items', _item('tool', _one_line(gist.strip()), ts, 'subagent', ident))
        elif itype == 'Extension':
            action = item.get('action') if isinstance(item.get('action'), dict) else {}
            queries = action.get('queries') if isinstance(action.get('queries'), list) else []
            gist = (action.get('query') or (queries[0] if queries else None) or action.get('url')
                    or action.get('pattern') or item.get('query') or '')
            self._add('items', _item('tool', _one_line(gist), ts, item.get('kind') or 'extension', ident))
        elif itype == 'ImageView':
            self._add('items', _item('tool', _one_line(_rel(item.get('path'), cwd)), ts, 'view_image', ident))
        elif itype == 'ContextCompaction':
            self._add('items', _item('notice', 'context compacted', ts, 'compaction', ident))
        # Reasoning, FunctionCallOutput, Plan...: omitted

    def _response(self, payload, ts):
        ptype = payload.get('type')
        ident = payload.get('call_id') or payload.get('id') or self._lid()
        if ptype == 'message':
            role = payload.get('role')
            if role == 'assistant':
                text, _ = _codex_text_parts(payload.get('content'))
                if text.strip():
                    self._add('responses', _item('assistant', text, ts,
                                                 _CODEX_PHASE.get(payload.get('phase')), ident))
            elif role == 'user' and self.kind == 'user':
                meta = payload.get('internal_chat_message_metadata_passthrough') or {}
                kinds = meta.get('content_item_kinds') if isinstance(meta, dict) else None
                if kinds and not set(kinds) <= _CODEX_GENUINE_KINDS:
                    return
                text, images = _codex_text_parts(payload.get('content'))
                stripped = text.lstrip()
                if not (text.strip() or images) or _codex_injected(text) or _GENERIC_TAG_RE.match(stripped):
                    return
                self._add('responses', _item('user', _with_images(text, images), ts, None, ident))
        elif ptype in ('function_call', 'custom_tool_call'):
            args = payload.get('arguments') if ptype == 'function_call' else payload.get('input')
            name, gist = _codex_call_gist(payload.get('name') or 'tool', args, self.cwd)
            item = _item('tool', gist, ts, name, ident)
            self._add('events', item)
            self._add('responses', item)
        elif ptype in ('local_shell_call', 'web_search_call'):
            action = payload.get('action') if isinstance(payload.get('action'), dict) else {}
            if ptype == 'local_shell_call':
                name, gist = 'shell', _one_line(_codex_command(action.get('command')))
            else:
                queries = action.get('queries') if isinstance(action.get('queries'), list) else []
                name = 'web.search'
                gist = _one_line(action.get('query') or (queries[0] if queries else None)
                                 or action.get('url') or action.get('pattern') or '')
            item = _item('tool', gist, ts, name, ident)
            self._add('events', item)
            self._add('responses', item)


# ---------------------------------------------------------------- Transcript

class Transcript:
    """Incremental reader for one native transcript file (read-only, thread-safe)."""

    def __init__(self, service, path):
        self.service = _service(service)
        self.path = path
        self._lock = threading.RLock()
        self._refreshed = False
        self._reset()

    def _reset(self):
        self._offset = 0            # bytes consumed, including the carried partial line
        self._partial = b''
        self._identity = None       # (st_dev, st_ino)
        self._head = b''
        self._lines = 0
        self._malformed = 0
        self._resets = getattr(self, '_resets', 0)
        self._error = None
        self._parser = _ClaudeParser() if self.service == 'claude-code' else _CodexParser()

    # -- reading
    def refresh(self):
        with self._lock:
            self._refreshed = True
            try:
                info = os.stat(self.path)
            except OSError as exc:
                self._error = 'cannot read transcript: %s' % (exc.strerror or exc)
                return
            identity = (info.st_dev, info.st_ino)
            if self._identity is not None and (identity != self._identity or info.st_size < self._offset):
                self._resets += 1
                self._reset()
            try:
                with open(self.path, 'rb') as handle:
                    if self._offset and info.st_size != self._offset and self._head:
                        if handle.read(len(self._head)) != self._head:
                            self._resets += 1
                            self._reset()
                    self._identity = identity
                    self._error = None
                    if info.st_size == self._offset:
                        return
                    handle.seek(self._offset)
                    while True:
                        chunk = handle.read(_READ_CHUNK)
                        if not chunk:
                            break
                        if len(self._head) < _HEAD_FINGERPRINT and self._offset < _HEAD_FINGERPRINT:
                            self._head += chunk[:_HEAD_FINGERPRINT - self._offset]
                        self._offset += len(chunk)
                        data = self._partial + chunk if self._partial else chunk
                        lines = data.split(b'\n')
                        self._partial = lines.pop()
                        for raw in lines:
                            self._feed(raw)
            except OSError as exc:
                self._error = 'cannot read transcript: %s' % (exc.strerror or exc)

    def _feed(self, raw):
        if not raw.strip():
            return
        self._lines += 1
        try:
            line = json.loads(raw)
        except ValueError:
            self._malformed += 1
            return
        if not isinstance(line, dict):
            self._malformed += 1
            return
        try:
            self._parser.lineno = self._lines
            self._parser.feed(line)
        except Exception:           # one odd line must never break the stream
            self._malformed += 1

    # -- results
    def items(self):
        with self._lock:
            if not self._refreshed:
                self.refresh()
            return [dict(item) for item in self._parser.items()]

    def status(self):
        with self._lock:
            parser = self._parser
            unknown_types = dict(parser.unknown_types)
            if self.service == 'codex':
                unknown_types.update(parser.unknown_subtypes)
            warnings = []
            ok = self._error is None
            if self._error:
                warnings.append(self._error)
            recognized = self._lines - self._malformed - parser.unknown_lines
            if self._lines and recognized <= 0:
                ok = False
                warnings.append('no recognizable lines; %s may not match this format'
                                % ADAPTER_VERSION[self.service])
            elif self._lines >= 20 and self._malformed * 10 > self._lines:
                ok = False
            if self._malformed:
                warnings.append('%d malformed line%s' % (self._malformed, '' if self._malformed == 1 else 's'))
            if unknown_types:
                names = sorted(unknown_types)
                warnings.append('unknown line types: %s' % ', '.join(names[:6] + (['...'] if len(names) > 6 else [])))
            result = {
                'adapter': ADAPTER_VERSION[self.service],
                'ok': ok,
                'warning': '; '.join(warnings) or None,
                'lines': self._lines,
                'malformed': self._malformed,
                'unknown': parser.unknown,
                'bytes': self._offset - len(self._partial),
                'items': len(parser.items()),
                'version': parser.version,
                'resets': self._resets,
                'path': self.path,
            }
            if self.service == 'claude-code':
                result['session_id'] = parser.session_id
                result['title'] = parser.title
                result['tool_results'] = parser.tool_results
                result['subagents'] = self._subagent_count()
            else:
                result['session_id'] = (parser.meta or {}).get('id')
                result['kind'] = parser.kind
                result['generation'] = parser.mode or 'responses'
                result['duplicates'] = parser.duplicates
            return result

    def _subagent_count(self):
        base = self.path[:-len('.jsonl')] if self.path.endswith('.jsonl') else self.path
        try:
            return sum(1 for n in os.listdir(os.path.join(base, 'subagents')) if n.endswith('.jsonl'))
        except OSError:
            return 0


# ---------------------------------------------------------------- discovery

def _slug(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


_TAIL_MARKERS = (b'"type":"custom-title"', b'"type":"ai-title"', b'"type":"last-prompt"')


def _tail_lines(handle, size, start, steps=(65536, 524288)):
    """The last line carrying each title marker, found near the end of the file.

    Searches the last 64 kB, then the last 512 kB, for title-bearing lines
    without parsing the bulky transcript lines around them.
    """
    found = {}
    for span in steps:
        begin = max(start, size - span)
        if begin >= size:
            break
        handle.seek(begin)
        data = handle.read(size - begin)
        for marker in _TAIL_MARKERS:
            if marker in found:
                continue
            at = data.rfind(marker)
            if at < 0:
                continue
            left = data.rfind(b'\n', 0, at)
            if left < 0 and begin > start:
                continue            # the line starts before this window
            right = data.find(b'\n', at)
            found[marker] = data[left + 1:right if right >= 0 else len(data)]
        if _TAIL_MARKERS[1] in found or begin == start:
            break
    return [found[m] for m in _TAIL_MARKERS if m in found]


def _loads(raw):
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _claude_summary(path, size, head=65536):
    with open(path, 'rb') as handle:
        first = handle.read(head)
        head_lines = first.split(b'\n')
        if size > len(first):
            head_lines.pop()        # incomplete
        tail_lines = _tail_lines(handle, size, len(first)) if size > len(first) else []
    info = {'cwd': None, 'session': None, 'started': None, 'prompt': None,
            'ai_title': None, 'custom_title': None, 'last_prompt': None, 'entrypoint': None}
    for phase, lines in (('head', head_lines), ('tail', tail_lines)):
        for raw in lines:
            line = _loads(raw)
            if line is None:
                continue
            if line.get('cwd') and not info['cwd']:
                info['cwd'] = line['cwd']
            if line.get('entrypoint') and not info['entrypoint']:
                info['entrypoint'] = line['entrypoint']
            if line.get('sessionId') and not info['session']:
                info['session'] = line['sessionId']
            kind = line.get('type')
            if phase == 'head' and line.get('timestamp') and not info['started']:
                info['started'] = line['timestamp']
            if kind == 'ai-title' and isinstance(line.get('aiTitle'), str):
                info['ai_title'] = line['aiTitle']
            elif kind == 'custom-title' and isinstance(line.get('customTitle'), str):
                info['custom_title'] = line['customTitle']
            elif kind == 'last-prompt' and isinstance(line.get('lastPrompt'), str):
                info['last_prompt'] = line['lastPrompt']
            elif (phase == 'head' and kind == 'user' and info['prompt'] is None
                  and not line.get('isSidechain')):
                verdict, text = _claude_user(line)
                if verdict == 'prompt':
                    info['prompt'] = text
    return info


def _discover_claude(hub_root, since_days, limit, include_subagents, deadline):
    projects = os.path.join(claude_home(), 'projects')
    root_real = os.path.realpath(hub_root)
    prefixes = tuple(sorted({_slug(os.path.abspath(hub_root)), _slug(root_real)}))
    cutoff = time.time() - since_days * 86400
    candidates = []
    try:
        dirs = os.listdir(projects)
    except OSError:
        return []
    for name in dirs:
        if not name.startswith(prefixes):
            continue
        folder = os.path.join(projects, name)
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith('.jsonl'):
                continue
            path = os.path.join(folder, entry)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if info.st_mtime >= cutoff and info.st_size > 0:
                candidates.append((info.st_mtime, info.st_size, path))
    candidates.sort(reverse=True)
    results = []
    cwd_cache = {}
    for mtime, size, path in candidates:
        if len(results) >= limit or time.monotonic() > deadline:
            break
        try:
            summary = _claude_summary(path, size)
        except OSError:
            continue
        cwd = summary['cwd']
        if cwd not in cwd_cache:
            cwd_cache[cwd] = _inside(cwd, root_real)
        if not cwd_cache[cwd]:
            continue
        entrypoint = summary['entrypoint'] or ''
        kind = 'sdk' if entrypoint.startswith('sdk') else 'user'
        if kind != 'user' and not include_subagents:
            continue
        title = summary['custom_title'] or summary['ai_title'] or summary['prompt'] or summary['last_prompt']
        results.append({
            'service': 'claude-code',
            'session_id': os.path.basename(path)[:-len('.jsonl')],
            'transcript_path': path,
            'cwd': cwd,
            'started': summary['started'] or _iso_from_epoch(mtime),
            'updated': _iso_from_epoch(mtime),
            'title': _one_line(title, TITLE_MAX, False) or None,
            'kind': kind,
        })
    return results


def _codex_db_path(home):
    best, best_n = None, -1
    for path in glob.glob(os.path.join(home, 'state_*.sqlite')):
        match = re.search(r'state_(\d+)\.sqlite$', path)
        if match and int(match.group(1)) > best_n:
            best, best_n = path, int(match.group(1))
    return best


def _codex_from_sqlite(home, root_real, since_days, limit, include_subagents, deadline):
    db = _codex_db_path(home)
    if not db:
        return None
    sql_deadline = min(deadline, time.monotonic() + _SQLITE_BUDGET)
    con = None
    try:
        con = sqlite3.connect('file:%s?mode=ro' % quote(db), uri=True, timeout=0.1)
        con.execute('PRAGMA query_only=1')
        con.set_progress_handler(lambda: 1 if time.monotonic() > sql_deadline else 0, 1000)
        columns = {row[1] for row in con.execute('PRAGMA table_info(threads)')}
        if not {'id', 'rollout_path', 'cwd', 'updated_at'} <= columns:
            return None
        wanted = ['id', 'rollout_path', 'cwd', 'updated_at']
        for optional in ('updated_at_ms', 'created_at', 'created_at_ms', 'name', 'title',
                         'first_user_message', 'thread_source', 'source', 'archived'):
            if optional in columns:
                wanted.append(optional)
        cutoff = int(time.time() - since_days * 86400)
        rows = con.execute('SELECT %s FROM threads WHERE updated_at >= ? ORDER BY updated_at DESC'
                           % ', '.join(wanted), (cutoff,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if con is not None:
            con.close()
    results = []
    cwd_cache = {}
    for values in rows:
        if len(results) >= limit or time.monotonic() > deadline:
            break
        row = dict(zip(wanted, values))
        cwd = row.get('cwd')
        if cwd not in cwd_cache:
            cwd_cache[cwd] = _inside(cwd, root_real)
        if not cwd_cache[cwd]:
            continue
        kind = row.get('thread_source')
        if not kind:
            source = row.get('source') or ''
            try:
                source = json.loads(source) if source.startswith('{') else source
            except ValueError:
                pass
            kind = _codex_kind({'source': source})
        if kind != 'user' and not include_subagents:
            continue
        path = row.get('rollout_path')
        if not path or not os.path.exists(path):
            continue
        created = row.get('created_at_ms')
        created = created / 1000.0 if created else row.get('created_at')
        updated = row.get('updated_at_ms')
        updated = updated / 1000.0 if updated else row.get('updated_at')
        title = row.get('name') or row.get('title') or row.get('first_user_message')
        results.append({
            'service': 'codex',
            'session_id': row['id'],
            'transcript_path': path,
            'cwd': cwd,
            'started': _iso_from_epoch(created),
            'updated': _iso_from_epoch(updated),
            'title': _one_line(title, TITLE_MAX, False) or None,
            'kind': kind,
        })
    return results


def _codex_index_titles(home, deadline):
    titles = {}
    try:
        with open(os.path.join(home, 'session_index.jsonl'), 'rb') as handle:
            for raw in handle:
                line = _loads(raw)
                if line and line.get('id') and isinstance(line.get('thread_name'), str):
                    titles[line['id']] = line['thread_name']
                if time.monotonic() > deadline:
                    break
    except OSError:
        pass
    return titles


def _codex_from_files(home, root_real, since_days, limit, include_subagents, deadline):
    cutoff = time.time() - since_days * 86400
    first_day = (datetime.fromtimestamp(cutoff) - timedelta(days=1)).strftime('%Y/%m/%d')
    sessions = os.path.join(home, 'sessions')
    candidates = []
    folders = []
    for year in _listdir_sorted(sessions):
        for month in _listdir_sorted(os.path.join(sessions, year)):
            for day in _listdir_sorted(os.path.join(sessions, year, month)):
                if '%s/%s/%s' % (year, month, day) >= first_day:
                    folders.append(os.path.join(sessions, year, month, day))
    folders.append(os.path.join(home, 'archived_sessions'))
    for folder in folders:
        for entry in _listdir_sorted(folder):
            if not (entry.startswith('rollout-') and entry.endswith('.jsonl')):
                continue
            path = os.path.join(folder, entry)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if info.st_mtime >= cutoff:
                candidates.append((info.st_mtime, path))
    candidates.sort(reverse=True)
    titles = _codex_index_titles(home, deadline)
    results = []
    cwd_cache = {}
    for mtime, path in candidates:
        if len(results) >= limit or time.monotonic() > deadline:
            break
        try:
            with open(path, 'rb') as handle:
                first = handle.readline(1 << 20)
        except OSError:
            continue
        line = _loads(first)
        meta = (line or {}).get('payload') if (line or {}).get('type') == 'session_meta' else None
        if not isinstance(meta, dict):
            continue
        cwd = meta.get('cwd')
        if cwd not in cwd_cache:
            cwd_cache[cwd] = _inside(cwd, root_real)
        if not cwd_cache[cwd]:
            continue
        kind = _codex_kind(meta)
        if kind != 'user' and not include_subagents:
            continue
        ident = meta.get('id') or meta.get('session_id')
        results.append({
            'service': 'codex',
            'session_id': ident,
            'transcript_path': path,
            'cwd': cwd,
            'started': meta.get('timestamp') or line.get('timestamp'),
            'updated': _iso_from_epoch(mtime),
            'title': _one_line(titles.get(ident), TITLE_MAX, False) or None,
            'kind': kind,
        })
    return results


def _listdir_sorted(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def discover(service, hub_root, since_days=14, limit=60, include_subagents=False):
    """Recent native sessions whose cwd is inside hub_root, most recent first:
       [{"session_id", "transcript_path", "cwd", "started", "updated", "title", "kind", "service"}]

    Claude `kind` is "user" or "sdk" (claude -p); Codex `kind` is the thread
    source ("user", "subagent", "guardian_review").  Non-user sessions are
    returned only with include_subagents=True.  Bounded to about 1 s.
    """
    service = _service(service)
    deadline = time.monotonic() + _DISCOVER_BUDGET
    root_real = os.path.realpath(hub_root)
    if service == 'claude-code':
        results = _discover_claude(hub_root, since_days, limit, include_subagents, deadline)
    else:
        home = codex_home()
        results = _codex_from_sqlite(home, root_real, since_days, limit, include_subagents, deadline)
        if results is None:
            results = _codex_from_files(home, root_real, since_days, limit, include_subagents, deadline)
    results.sort(key=lambda r: r.get('updated') or '', reverse=True)
    return results[:limit]
