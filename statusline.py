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

    # 3. Context window usage
    cw = data.get('context_window')
    if isinstance(cw, dict) and cw.get('used_percentage') is not None:
        pct = round(float(cw['used_percentage']))
        seg = f'{DIM}Ctx:{RESET} {raw_color(pct)}{pct}%{RESET}'
        used, total = cw.get('total_input_tokens'), cw.get('context_window_size')
        if used is not None and total is not None and float(total) > 0:
            seg += f' {DIM}({format_tokens(used)}/{format_tokens(total)}){RESET}'
        parts.append(seg)

    # 4 & 5. Rate limits (5-hour session + 7-day weekly)
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
