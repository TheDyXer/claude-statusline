#!/usr/bin/env python3
"""claude-statusline: Python version (macOS, Linux, or Windows with Python 3).

https://github.com/TheDyXer/claude-statusline

Usage in settings.json:
  python3 /home/<you>/.claude/statusline.py [--label NAME]

Keep in step with statusline.ps1: tests/test_parity.py checks that both print identical output.
"""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime

DOT = '·'

# ANSI color setup
E = '\x1b'
RESET = f'{E}[0m'
DIM = f'{E}[90m'
BOLD_CYAN = f'{E}[1;36m'
GREEN = f'{E}[32m'
YELLOW = f'{E}[33m'
RED = f'{E}[31m'
BADGE = f'{E}[30;48;5;208m'

# low and unknown levels stay uncolored
EFFORT_COLORS = {
    'medium': f'{E}[34m',
    'high': f'{E}[94m',
    'xhigh': f'{E}[35m',
    'max': f'{E}[1;95m',
}

ALIASES = ('fable', 'opus', 'sonnet', 'haiku')
MODEL_ID = re.compile(r'claude-([a-z]+)-([0-9]{1,2})(?:-([0-9]{1,2}))?(?:-[0-9]{8})?')

# How much of the session transcript to read, from the end
TAIL_BYTES = 2 * 1024 * 1024
COMMAND_OUTPUT = '<local-command-stdout>'
ADVISOR_OUTPUT = COMMAND_OUTPUT + 'Advisor '
KEEPS = re.compile(r'keeps (.+?) as its advisor model')

TTL = re.compile(r'([0-9]+)([smh])')
TTL_UNITS = {'s': 1, 'm': 60, 'h': 3600}

# STATUSLINE_NOW (unix seconds) pins the clock for tests
NOW = int(os.environ['STATUSLINE_NOW']) if os.environ.get('STATUSLINE_NOW') else int(time.time())


def raw_color(pct):
    if pct < 50:
        return GREEN
    if pct < 80:
        return YELLOW
    return RED


def pace_color(used, resets_at, window):
    """Color by projected usage at reset if the current rate holds.

    90%+ is always red; the first 10% of a window uses raw thresholds.
    """
    shown = round(used)
    if shown >= 90:
        return RED
    if resets_at <= 0:
        return raw_color(shown)
    elapsed = min(1.0, (window - (resets_at - NOW)) / window)
    if elapsed < 0.1:
        return raw_color(shown)
    projected = used / elapsed
    if projected < 80:
        return GREEN
    if projected <= 100:
        return YELLOW
    return RED


def format_countdown(resets_at):
    secs_left = resets_at - NOW
    if secs_left <= 0:
        return None
    if secs_left < 60:
        return '<1m'
    mins = secs_left // 60
    if mins >= 1440:
        return f'{mins // 1440}d {(mins % 1440) // 60}h'
    if mins >= 60:
        return f'{mins // 60}h {mins % 60:02d}m'
    return f'{mins}m'


def format_tokens(n):
    """850, 1k, 15.5k, 200k, 1.2M (truncated, not rounded)."""
    n = int(n)
    if n < 1000:
        return str(n)
    t, unit = (n // 100, 'k') if n < 1_000_000 else (n // 100_000, 'M')
    if t % 10 == 0:
        return f'{t // 10}{unit}'
    return f'{t // 10}.{t % 10}{unit}'


def format_cache(pc):
    """When the prompt cache goes cold; None hides the part.

    Yellow in the last 20% of the cache's lifetime, "cold" once it has expired.
    """
    if not isinstance(pc, dict) or pc.get('caching_observed') is False:
        return None
    expires = pc.get('expires_at')
    is_number = isinstance(expires, (int, float)) and not isinstance(expires, bool)
    left = int(math.floor(expires)) - NOW if is_number else 0
    if pc.get('warm') is not True or left <= 0:
        return f'{DIM}cache{RESET} {RED}cold{RESET}'
    m = TTL.fullmatch(pc['ttl']) if isinstance(pc.get('ttl'), str) else None
    ttl = int(m[1]) * TTL_UNITS[m[2]] if m else 0
    color = YELLOW if left * 5 < ttl else GREEN
    when = datetime.fromtimestamp(NOW + left).strftime('%H:%M')
    return f'{DIM}cache{RESET} {color}({when} {DOT} {format_countdown(NOW + left)}){RESET}'


def env_truthy(name):
    return os.environ.get(name, '').strip(' \t\r\n').lower() in ('1', 'true', 'yes', 'on')


def read_settings_file(path):
    """Parsed settings file, or None if it's missing, unreadable or not a JSON object."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            settings = json.load(f)
    except Exception:
        return None
    return settings if isinstance(settings, dict) else None


def read_transcript_tail(path):
    """The last TAIL_BYTES of the transcript as lines, oldest first; None if unreadable.

    A line cut off by the start of the window is blanked. Reading one byte before the
    window tells a cut-off line from one that starts exactly there.
    """
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - TAIL_BYTES)
            begin = start - 1 if start > 0 else 0
            f.seek(begin)
            data = f.read(size - begin)
    except Exception:
        return None
    lines = data.decode('utf-8', 'replace').split('\n')
    if start > 0:
        lines[0] = ''
    return lines


def transcript_advisor(path):
    """The advisor this session last used, from its transcript.

    Returns a model name, '' for off, or None if nothing in the last TAIL_BYTES decides it.
    The newest decisive entry wins: every main-thread reply records the advisor it was sent
    with (advisorModel, left out when off), and /advisor output covers the time before the
    next reply. The /advisor texts are from Claude Code 2.1.286.
    """
    lines = read_transcript_tail(path)
    for line in reversed(lines or []):
        line = line.rstrip('\r')
        if not line.startswith('{') or ('"type":"assistant"' not in line and ADVISOR_OUTPUT not in line):
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        message = entry.get('message') if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        if entry.get('type') == 'assistant':
            if (entry.get('isSidechain') is True or entry.get('isAbortedMidStream') is True
                    or message.get('model') == '<synthetic>'):
                continue
            value = entry.get('advisorModel')
            return value if isinstance(value, str) and value else ''
        content = message.get('content')
        if entry.get('type') == 'user' and isinstance(content, str) and content.startswith(ADVISOR_OUTPUT):
            text = content[len(COMMAND_OUTPUT):].split('</local-command-stdout>')[0]
            if text.startswith('Advisor disabled'):
                return ''
            m = KEEPS.search(text)
            if m:
                return m[1]
            if 'will not activate' in text or 'will activate when' in text:
                return ''
            if text.startswith('Advisor set to '):
                return text[len('Advisor set to '):].split('\n')[0].strip(' \t\r')
    return None


def session_advisor(data):
    """The advisor this session uses; None or '' is off.

    Claude Code doesn't send the advisor to status line scripts, and settings.json only holds
    the last choice saved from any session. So the session's own transcript comes first, and
    the settings files are the fallback (a new session before its first reply, or after /clear).
    """
    if env_truthy('CLAUDE_CODE_DISABLE_ADVISOR_TOOL'):
        return None
    path = data.get('transcript_path')
    if isinstance(path, str) and path:
        found = transcript_advisor(path)
        if found is not None:
            return found
    return advisor_setting(data)


def advisor_setting(data):
    """advisorModel in Claude Code's settings order; None means off.

    The first file that has the key decides, even if its value is null.
    """
    paths = []
    workspace = data.get('workspace')
    project = workspace.get('project_dir') if isinstance(workspace, dict) else None
    if isinstance(project, str) and project:
        paths += [os.path.join(project, '.claude', 'settings.local.json'),
                  os.path.join(project, '.claude', 'settings.json')]
    config = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')
    paths.append(os.path.join(config, 'settings.json'))
    for path in paths:
        settings = read_settings_file(path)
        if settings is None or 'advisorModel' not in settings:
            continue
        value = settings['advisorModel']
        if isinstance(value, str) and value.strip(' \t\r\n'):
            return value.strip(' \t\r\n')
        return None
    return None


def format_model_id(value):
    """fable -> Fable, claude-opus-5-5 -> Opus 5.5, anything else as-is."""
    low = value.lower()
    m = MODEL_ID.fullmatch(low)
    if low in ALIASES:
        family, version = low, ''
    elif m:
        family, version = m[1], f' {m[2]}' + (f'.{m[3]}' if m[3] else '')
    else:
        return value
    return family[:1].upper() + family[1:] + version


def format_limit(name, limit, window, time_format):
    if not isinstance(limit, dict) or limit.get('used_percentage') is None:
        return None
    used = float(limit['used_percentage'])
    resets_at = int(math.floor(float(limit['resets_at']))) if limit.get('resets_at') else 0
    color = pace_color(used, resets_at, window)
    seg = f'{DIM}{name}{RESET} {color}{round(used)}%{RESET}'
    if resets_at > 0:
        when = datetime.fromtimestamp(resets_at).strftime(time_format)
        left = format_countdown(resets_at)
        if left:
            when += f' {DOT} {left}'
        seg += f' {DIM}({when}){RESET}'
    return seg


def build(data, label):
    parts = []

    # 1. Model display name
    model = data.get('model')
    if isinstance(model, dict) and model.get('display_name'):
        parts.append(f'{BOLD_CYAN}{model["display_name"]}{RESET}')

    # 2. Effort level
    effort = data.get('effort')
    if isinstance(effort, dict) and effort.get('level'):
        level = str(effort['level'])
        c = EFFORT_COLORS.get(level.lower())
        val = f'{c}{level}{RESET}' if c else level
        parts.append(f'{DIM}effort:{RESET} {val}')

    # 3. Advisor (Claude Code doesn't send it, so read it from the transcript or settings)
    advisor = session_advisor(data)
    val = f'{BOLD_CYAN}{format_model_id(advisor)}{RESET}' if advisor else f'{DIM}off{RESET}'
    parts.append(f'{DIM}advisor:{RESET} {val}')

    # 4. Context window usage
    cw = data.get('context_window')
    if isinstance(cw, dict) and cw.get('used_percentage') is not None:
        pct = round(float(cw['used_percentage']))
        seg = f'{DIM}Ctx:{RESET} {raw_color(pct)}{pct}%{RESET}'
        used, total = cw.get('total_input_tokens'), cw.get('context_window_size')
        if used is not None and total is not None and float(total) > 0:
            seg += f' {DIM}({format_tokens(used)}/{format_tokens(total)}){RESET}'
        parts.append(seg)

    # 5. Prompt cache: when it goes cold
    seg = format_cache(data.get('prompt_cache'))
    if seg:
        parts.append(seg)

    # 6 & 7. Rate limits (5-hour session + 7-day weekly)
    rl = data.get('rate_limits')
    if isinstance(rl, dict):
        for seg in (format_limit('5h', rl.get('five_hour'), 18000, '%H:%M'),
                    format_limit('wk', rl.get('seven_day'), 604800, '%a %H:%M')):
            if seg:
                parts.append(seg)

    line = f'{DIM} | {RESET}'.join(parts)
    if label:
        tag = f'{BADGE} {label} {RESET}'
        line = f'{tag} {line}' if line else tag
    return line


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--label')
    args = parser.parse_args()
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        data = None
    line = build(data if isinstance(data, dict) else {}, args.label)
    sys.stdout.buffer.write((line + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
