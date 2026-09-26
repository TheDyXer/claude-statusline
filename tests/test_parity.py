#!/usr/bin/env python3
"""Parity test: statusline.ps1 and statusline.py must print byte-identical lines.

Usage: python tests/test_parity.py [--show]
  --show  also print each rendered line with the color codes stripped

Uses pwsh if installed, otherwise Windows PowerShell. Set PS_EXE to pick one.
Without any PowerShell, only the Python-side checks run.
"""
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, 'statusline.ps1')
PY = os.path.join(ROOT, 'statusline.py')

N = 1790424000  # fixed "now" (2026-09-26 12:00 UTC)
H, D = 3600, 86400
BADGE = '\x1b[30;48;5;208m work \x1b[0m'


def full(**over):
    d = {
        'model': {'display_name': 'Opus 5.5'},
        'effort': {'level': 'medium'},
        'context_window': {'total_input_tokens': 15500, 'context_window_size': 200000, 'used_percentage': 8},
        'rate_limits': {
            'five_hour': {'used_percentage': 62, 'resets_at': N + 7530},
            'seven_day': {'used_percentage': 41, 'resets_at': N + 3 * D + 14 * H + 120},
        },
    }
    d.update(over)
    return json.dumps(d)


def limits(five=None, week=None):
    rl = {}
    if five is not None:
        rl['five_hour'] = five
    if week is not None:
        rl['seven_day'] = week
    return json.dumps({'model': {'display_name': 'Opus 5.5'}, 'rate_limits': rl})


def ctx(pct, used=None, total=None):
    cw = {'used_percentage': pct}
    if used is not None:
        cw['total_input_tokens'] = used
    if total is not None:
        cw['context_window_size'] = total
    return json.dumps({'model': {'display_name': 'Opus 5.5'}, 'context_window': cw})


CASES = [
    ('full payload', full()),
    ('session start', json.dumps({'model': {'display_name': 'Opus 5.5'},
                                  'context_window': {'total_input_tokens': 0, 'context_window_size': 200000,
                                                     'used_percentage': None, 'current_usage': None}})),
    ('empty object', '{}'),
    ('invalid json', 'not json'),
    ('unicode model', json.dumps({'model': {'display_name': 'Ópus ✨'}})),
    *[(f'effort {lv}', full(effort={'level': lv})) for lv in ('low', 'medium', 'high', 'xhigh', 'max', 'turbo', 'High')],
    ('ctx 49.6 tokens 999/1000', ctx(49.6, 999, 1000)),
    ('ctx 79.4 tokens 15550/1M', ctx(79.4, 15550, 1000000)),
    ('ctx 80 tokens 1.25M/1M', ctx(80, 1250000, 1000000)),
    ('ctx 23.5 round-half-even', ctx(23.5, 999999, 200000)),
    ('ctx 24.5 round-half-even', ctx(24.5, 1000, 200000)),
    ('ctx size 0', ctx(10, 500, 0)),
    ('ctx tokens null', ctx(10)),
    ('5h grace raw green', limits({'used_percentage': 3, 'resets_at': N + 18000 - 300})),
    ('5h grace raw yellow', limits({'used_percentage': 55, 'resets_at': N + 18000 - 300})),
    ('5h pace yellow', limits({'used_percentage': 45, 'resets_at': N + 9000})),
    ('5h pace red', limits({'used_percentage': 45, 'resets_at': N + 14400})),
    ('5h pace green at 75%', limits({'used_percentage': 75, 'resets_at': N + 900})),
    ('5h 90 always red', limits({'used_percentage': 90, 'resets_at': N + 900})),
    ('5h 89.5 rounds to 90', limits({'used_percentage': 89.5, 'resets_at': N + 900})),
    ('5h no resets_at', limits({'used_percentage': 60})),
    ('5h reset passed', limits({'used_percentage': 30, 'resets_at': N - 10})),
    ('5h reset beyond window', limits({'used_percentage': 30, 'resets_at': N + 20000})),
    ('5h <1m', limits({'used_percentage': 30, 'resets_at': N + 30})),
    ('5h 45m', limits({'used_percentage': 30, 'resets_at': N + 2730})),
    ('5h 1h 00m', limits({'used_percentage': 30, 'resets_at': N + 3610})),
    ('5h float resets_at', limits({'used_percentage': 30.4, 'resets_at': N + 7530.7})),
    ('wk 1d 0h', limits(week={'used_percentage': 20, 'resets_at': N + D + 30})),
    ('wk 6d 23h', limits(week={'used_percentage': 5, 'resets_at': N + 6 * D + 23 * H + 1800})),
    ('wk 23h 05m', limits(week={'used_percentage': 20, 'resets_at': N + 23 * H + 330})),
    ('wk day 6 at 60% green', limits(week={'used_percentage': 60, 'resets_at': N + D})),
    ('wk day 1 at 45% red', limits(week={'used_percentage': 45, 'resets_at': N + 6 * D})),
    ('wk only (no 5h)', limits(week={'used_percentage': 41, 'resets_at': N + 3 * D})),
]

# Expected colors, so the test catches both scripts being wrong in the same way.
COLOR_NAMES = {'32': 'green', '33': 'yellow', '31': 'red', '34': 'blue', '94': 'bright-blue',
               '35': 'magenta', '1;95': 'bold-bright-magenta'}
EXPECTED_COLORS = {
    'full payload': ['effort blue', 'ctx green', '5h red', 'wk yellow'],
    'effort low': ['effort plain'], 'effort medium': ['effort blue'], 'effort high': ['effort bright-blue'],
    'effort xhigh': ['effort magenta'], 'effort max': ['effort bold-bright-magenta'],
    'effort turbo': ['effort plain'], 'effort High': ['effort bright-blue'],
    'ctx 49.6 tokens 999/1000': ['ctx yellow'], 'ctx 79.4 tokens 15550/1M': ['ctx yellow'],
    'ctx 80 tokens 1.25M/1M': ['ctx red'], 'ctx 23.5 round-half-even': ['ctx green'],
    '5h grace raw green': ['5h green'], '5h grace raw yellow': ['5h yellow'],
    '5h pace yellow': ['5h yellow'], '5h pace red': ['5h red'], '5h pace green at 75%': ['5h green'],
    '5h 90 always red': ['5h red'], '5h 89.5 rounds to 90': ['5h red'], '5h no resets_at': ['5h yellow'],
    '5h reset passed': ['5h green'], '5h reset beyond window': ['5h green'],
    'wk 1d 0h': ['wk green'], 'wk day 6 at 60% green': ['wk green'], 'wk day 1 at 45% red': ['wk red'],
}


def colors_in(line):
    found = []
    for part, pattern in (('effort', r'effort:\x1b\[0m (?:\x1b\[([0-9;]+)m)?[A-Za-z]'),
                          ('ctx', r'Ctx:\x1b\[0m \x1b\[([0-9;]+)m\d'),
                          ('5h', r'5h\x1b\[0m \x1b\[([0-9;]+)m\d'),
                          ('wk', r'wk\x1b\[0m \x1b\[([0-9;]+)m\d')):
        m = re.search(pattern, line)
        if m:
            found.append(f'{part} {COLOR_NAMES.get(m.group(1), "plain") if m.group(1) else "plain"}')
    return found


def run(cmd, payload):
    env = dict(os.environ, STATUSLINE_NOW=str(N))
    return subprocess.run(cmd, input=payload.encode('utf-8'), capture_output=True, env=env, timeout=30).stdout


def strip(b):
    return re.sub(r'\x1b\[[0-9;]*m', '', b.decode('utf-8', 'replace')).rstrip('\n')


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    show = '--show' in sys.argv
    ps_exe = os.environ.get('PS_EXE') or shutil.which('pwsh') or shutil.which('powershell')
    ps_cmd = [ps_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', PS1] if ps_exe else None
    py_cmd = [sys.executable, PY]
    print(f'python: {sys.executable}\npowershell: {ps_exe or "not found (parity checks skipped)"}\n')

    failures = 0
    total = 0

    def check(name, ok, detail, rendered):
        nonlocal failures, total
        total += 1
        failures += not ok
        print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  ->  {rendered}' if show else ''))
        if not ok:
            print(detail)

    for name, payload in CASES:
        b = run(py_cmd, payload)
        if ps_cmd:
            a = run(ps_cmd, payload)
            ok = a == b and b.endswith(b'\n') and b.count(b'\n') == 1
            check(f'parity: {name}', ok, f'      ps1: {a!r}\n      py : {b!r}', strip(b))
        if name in EXPECTED_COLORS:
            got = colors_in(b.decode('utf-8'))
            missing = [c for c in EXPECTED_COLORS[name] if c not in got]
            check(f'colors: {name}', not missing, f'      expected {missing}, got {got}', strip(b))

    for name, payload in (('badge + full payload', CASES[0][1]), ('badge alone', '{}')):
        plain = run(py_cmd, payload).decode('utf-8').rstrip('\n')
        want = (f'{BADGE} {plain}' if plain else BADGE) + '\n'
        got_py = run(py_cmd + ['--label', 'work'], payload).decode('utf-8')
        check(f'badge py: {name}', got_py == want, f'      want: {want!r}\n      got : {got_py!r}', strip(got_py.encode()))
        if ps_cmd:
            got_ps = run(ps_cmd + ['-Label', 'work'], payload).decode('utf-8')
            check(f'badge ps1: {name}', got_ps == want, f'      want: {want!r}\n      got : {got_ps!r}', strip(got_ps.encode()))

    print(f'\n{total - failures}/{total} passed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
