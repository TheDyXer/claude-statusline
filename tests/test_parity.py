#!/usr/bin/env python3
"""Parity test: statusline.ps1 and statusline.py must print byte-identical lines.

Usage: python tests/test_parity.py [--show]
  --show  also print each rendered line with the color codes stripped

Uses pwsh if installed, otherwise Windows PowerShell. Set PS_EXE to pick one.
Without any PowerShell, only the Python-side checks run.

Every run points CLAUDE_CONFIG_DIR at a folder of test settings (empty by default), so the
advisor part never depends on your real ~/.claude/settings.json.
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = os.path.join(ROOT, 'statusline.ps1')
PY = os.path.join(ROOT, 'statusline.py')

N = 1790424000  # fixed "now" (2026-09-26 12:00 UTC)
H, D = 3600, 86400
BADGE = '\x1b[30;48;5;208m work \x1b[0m'
DIM, CYAN, R = '\x1b[90m', '\x1b[1;36m', '\x1b[0m'

TMP = tempfile.mkdtemp(prefix='statusline-test-')
atexit.register(shutil.rmtree, TMP, True)
NO_CONFIG = os.path.join(TMP, 'no-config')  # never created, so there's no settings.json


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


def setting(value):
    return json.dumps({'advisorModel': value})


NO_FILE = object()  # transcript_path set, but the file doesn't exist


def advisor(name, user=None, local=None, project=None, env=None, base=None, transcript=None):
    """A case with its own settings files, given as raw text (None = no file).

    transcript is the raw text of the session transcript (see jl); it's passed as
    transcript_path in the input.
    """
    root = os.path.join(TMP, re.sub(r'[^a-z0-9]+', '-', name.lower()))
    files = {('config', 'settings.json'): user,
             ('project', '.claude', 'settings.local.json'): local,
             ('project', '.claude', 'settings.json'): project,
             ('session.jsonl',): None if transcript is NO_FILE else transcript}
    for rel, text in files.items():
        if text is not None:
            path = os.path.join(root, *rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
    payload = dict(base or {'model': {'display_name': 'Opus 5.5'}})
    payload['workspace'] = {'project_dir': os.path.join(root, 'project')}
    if transcript is not None:
        payload['transcript_path'] = os.path.join(root, 'session.jsonl')
    return name, json.dumps(payload), {'CLAUDE_CONFIG_DIR': os.path.join(root, 'config'), **(env or {})}


def jl(*entries):
    """Transcript lines the way Claude Code writes them: compact JSON, one per line."""
    return ''.join(json.dumps(e, separators=(',', ':'), ensure_ascii=False) + '\n' for e in entries)


def reply(advisor_model=None, model='claude-opus-5-5', **extra):
    """An assistant entry; advisorModel is only there when an advisor was attached."""
    entry = {'type': 'assistant', 'message': {'model': model, 'role': 'assistant',
                                              'content': [{'type': 'text', 'text': 'ok ✨ ő'}]}}
    if advisor_model:
        entry['advisorModel'] = advisor_model
    entry.update(extra)
    return entry


def command_output(text):
    """What /advisor leaves in the transcript."""
    return {'type': 'user', 'message': {'role': 'user',
                                        'content': f'<local-command-stdout>{text}</local-command-stdout>'}}


def tool_result(text):
    return {'type': 'user', 'message': {'role': 'user',
                                        'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': text}]}}


TAIL = 2 * 1024 * 1024  # how much of the transcript the scripts read, from the end


def filler(size):
    """Exactly `size` bytes of complete transcript lines that decide nothing."""
    base = len(jl(tool_result('')))
    lines = []
    while size - (base + 1000) >= base:
        lines.append(tool_result('x' * 1000))
        size -= base + 1000
    lines.append(tool_result('x' * (size - base)))
    return jl(*lines)


def window(offset):
    """An Opus reply that starts `offset` bytes after the start of the 2 MB window.

    Negative: the window starts inside the line before it. 0: exactly at its start.
    Positive: inside the reply, so it's cut off and can't decide.
    """
    line = jl(reply('claude-opus-5-5'))
    return jl(tool_result('y' * 100)) + line + filler(TAIL - len(line.encode('utf-8')) + offset)
KEEPS = ('Advisor set to Fable 5.1\nThe current conversation keeps Opus 5.5 as its advisor model until /clear '
         'or /compact, so the tool it has already declared stays unchanged; turning the advisor on or off '
         'applies right away.')
WEAKER = ('Advisor set to Sonnet 5\nSonnet 5 is less capable than the current main model (Opus 5.5), so the '
          'advisor will not activate. Choose a more capable model.')
UNSUPPORTED = ('Advisor set to Opus 5.5\nHaiku 3 (claude-3-haiku) does not support the advisor. It will '
               'activate when you switch to a supported main model.')


OFF = {'CLAUDE_CODE_DISABLE_ADVISOR_TOOL': '1'}

ADVISOR_CASES = [
    advisor('advisor no settings files'),
    advisor('advisor key absent', user='{"theme": "dark"}'),
    advisor('advisor fable', user=setting('fable')),
    advisor('advisor opus', user=setting('opus')),
    advisor('advisor sonnet', user=setting('sonnet')),
    advisor('advisor claude-opus-5-5', user=setting('claude-opus-5-5')),
    advisor('advisor claude-fable-5-1', user=setting('claude-fable-5-1')),
    advisor('advisor claude-sonnet-5', user=setting('claude-sonnet-5')),
    advisor('advisor dated haiku id', user=setting('claude-haiku-4-5-20251001')),
    advisor('advisor uppercase alias', user=setting('FABLE')),
    advisor('advisor padded alias', user=setting(' fable ')),
    advisor('advisor unknown name', user=setting('my-gateway-model')),
    advisor('advisor null', user=setting(None)),
    advisor('advisor empty string', user=setting('')),
    advisor('advisor number', user=setting(5)),
    advisor('advisor wrong key case', user='{"AdvisorModel": "opus"}'),
    advisor('advisor malformed user file', user='{"advisorModel": "opus",'),
    advisor('advisor user file with BOM', user='﻿' + setting('opus')),
    advisor('advisor top-level array', user='[{"advisorModel": "opus"}]'),
    advisor('advisor local beats user', user=setting('fable'), local=setting('opus')),
    advisor('advisor project beats user', user=setting('fable'), project=setting('sonnet')),
    advisor('advisor local beats project', project=setting('sonnet'), local=setting('claude-opus-5-5')),
    advisor('advisor local null turns off', user=setting('fable'), local=setting(None)),
    advisor('advisor project without key', user=setting('fable'), project='{"theme": "dark"}'),
    advisor('advisor malformed local skipped', user=setting('fable'), local='not json'),
    advisor('advisor disabled by env 1', user=setting('fable'), env=OFF),
    advisor('advisor disabled by env true', user=setting('fable'), env={'CLAUDE_CODE_DISABLE_ADVISOR_TOOL': 'True'}),
    advisor('advisor env 0 keeps it', user=setting('fable'), env={'CLAUDE_CODE_DISABLE_ADVISOR_TOOL': '0'}),
    advisor('advisor in full line', user=setting('fable'), base=json.loads(full())),
    # The session transcript beats settings; settings are the fallback
    advisor('transcript opus session', user=setting('fable'), transcript=jl(reply('claude-opus-5-5'))),
    advisor('transcript advisor off in session', user=setting('fable'), transcript=jl(reply())),
    advisor('transcript skips synthetic', user=setting('fable'),
            transcript=jl(reply('claude-opus-5-5'), reply(model='<synthetic>'))),
    advisor('transcript skips aborted', user=setting('fable'),
            transcript=jl(reply('claude-opus-5-5'), reply(isAbortedMidStream=True))),
    advisor('transcript skips sidechain', user=setting('fable'),
            transcript=jl(reply('claude-opus-5-5'), reply(isSidechain=True))),
    advisor('transcript /advisor set to', user=setting('fable'),
            transcript=jl(reply('claude-fable-5-1'), command_output('Advisor set to Opus 5.5'))),
    advisor('transcript /advisor disabled', user=setting('fable'),
            transcript=jl(reply('claude-fable-5-1'), command_output('Advisor disabled'))),
    advisor('transcript /advisor keeps note', user=setting('fable'),
            transcript=jl(reply('claude-opus-5-5'), command_output(KEEPS))),
    advisor('transcript /advisor will not activate', user=setting('fable'),
            transcript=jl(reply('claude-fable-5-1'), command_output(WEAKER))),
    advisor('transcript /advisor will activate when', user=setting('fable'),
            transcript=jl(reply('claude-fable-5-1'), command_output(UNSUPPORTED))),
    advisor('transcript reply after /advisor wins', user=setting('opus'),
            transcript=jl(command_output('Advisor set to Opus 5.5'), reply('claude-fable-5-1'))),
    advisor('transcript tool result ignored', user=setting('opus'),
            transcript=jl(reply('claude-fable-5-1'),
                          tool_result('<local-command-stdout>Advisor set to Opus 5.5</local-command-stdout>'))),
    advisor('transcript half-written last line', user=setting('fable'),
            transcript=jl(reply('claude-opus-5-5')) + '{"type":"assistant","advisorModel":"claude-fab'),
    advisor('transcript CRLF', user=setting('fable'), transcript=jl(reply('claude-opus-5-5')).replace('\n', '\r\n')),
    advisor('transcript window starts before entry', user=setting('fable'), transcript=window(-20)),
    advisor('transcript window starts at entry', user=setting('fable'), transcript=window(0)),
    advisor('transcript window cuts entry', user=setting('fable'), transcript=window(1)),
    advisor('transcript missing file', user=setting('fable'), transcript=NO_FILE),
    advisor('transcript empty file', user=setting('fable'), transcript=''),
    advisor('transcript nothing decisive', user=setting('fable'),
            transcript=jl(tool_result('hi'), command_output('Something else'))),
    advisor('transcript env disable wins', user=setting('fable'), transcript=jl(reply('claude-opus-5-5')), env=OFF),
]

# Expected advisor for every advisor case: a script that ignored the transcript or settings
# would still match the other script, so parity alone can't catch it
EXPECT_ADVISOR = {
    'advisor no settings files': 'off', 'advisor key absent': 'off', 'advisor fable': 'Fable',
    'advisor opus': 'Opus', 'advisor sonnet': 'Sonnet', 'advisor claude-opus-5-5': 'Opus 5.5',
    'advisor claude-fable-5-1': 'Fable 5.1', 'advisor claude-sonnet-5': 'Sonnet 5',
    'advisor dated haiku id': 'Haiku 4.5', 'advisor uppercase alias': 'Fable', 'advisor padded alias': 'Fable',
    'advisor unknown name': 'my-gateway-model', 'advisor null': 'off', 'advisor empty string': 'off',
    'advisor number': 'off', 'advisor wrong key case': 'off', 'advisor malformed user file': 'off',
    'advisor user file with BOM': 'Opus', 'advisor top-level array': 'off', 'advisor local beats user': 'Opus',
    'advisor project beats user': 'Sonnet', 'advisor local beats project': 'Opus 5.5',
    'advisor local null turns off': 'off', 'advisor project without key': 'Fable',
    'advisor malformed local skipped': 'Fable', 'advisor disabled by env 1': 'off',
    'advisor disabled by env true': 'off', 'advisor env 0 keeps it': 'Fable', 'advisor in full line': 'Fable',
    'transcript opus session': 'Opus 5.5', 'transcript advisor off in session': 'off',
    'transcript skips synthetic': 'Opus 5.5', 'transcript skips aborted': 'Opus 5.5',
    'transcript skips sidechain': 'Opus 5.5', 'transcript /advisor set to': 'Opus 5.5',
    'transcript /advisor disabled': 'off', 'transcript /advisor keeps note': 'Opus 5.5',
    'transcript /advisor will not activate': 'off', 'transcript /advisor will activate when': 'off',
    'transcript reply after /advisor wins': 'Fable 5.1', 'transcript tool result ignored': 'Fable 5.1',
    'transcript half-written last line': 'Opus 5.5', 'transcript CRLF': 'Opus 5.5',
    'transcript window starts before entry': 'Opus 5.5', 'transcript window starts at entry': 'Opus 5.5',
    'transcript window cuts entry': 'Fable',
    'transcript missing file': 'Fable', 'transcript empty file': 'Fable', 'transcript nothing decisive': 'Fable',
    'transcript env disable wins': 'off',
}

# Exact bytes, so both scripts can't be wrong the same way and still match
MODEL = f'{CYAN}Opus 5.5{R}{DIM} | {R}'
EXPECT = {
    'advisor fable': f'{MODEL}{DIM}advisor:{R} {CYAN}Fable{R}\n',
    'advisor claude-opus-5-5': f'{MODEL}{DIM}advisor:{R} {CYAN}Opus 5.5{R}\n',
    'advisor key absent': f'{MODEL}{DIM}advisor:{R} {DIM}off{R}\n',
    'transcript opus session': f'{MODEL}{DIM}advisor:{R} {CYAN}Opus 5.5{R}\n',
    'transcript advisor off in session': f'{MODEL}{DIM}advisor:{R} {DIM}off{R}\n',
}

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
# (name, payload, extra env); only the advisor cases need their own settings
CASES = [(name, payload, {}) for name, payload in CASES] + ADVISOR_CASES

# Expected colors, so the test catches both scripts being wrong in the same way.
COLOR_NAMES = {'32': 'green', '33': 'yellow', '31': 'red', '34': 'blue', '94': 'bright-blue',
               '35': 'magenta', '1;95': 'bold-bright-magenta', '1;36': 'bold-cyan', '90': 'gray'}
EXPECTED_COLORS = {
    'full payload': ['effort blue', 'advisor gray', 'ctx green', '5h red', 'wk yellow'],
    'advisor in full line': ['effort blue', 'advisor bold-cyan', 'ctx green', '5h red', 'wk yellow'],
    'advisor fable': ['advisor bold-cyan'], 'advisor key absent': ['advisor gray'],
    'transcript opus session': ['advisor bold-cyan'], 'transcript advisor off in session': ['advisor gray'],
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
                          ('advisor', r'advisor:\x1b\[0m \x1b\[([0-9;]+)m[A-Za-z]'),
                          ('ctx', r'Ctx:\x1b\[0m \x1b\[([0-9;]+)m\d'),
                          ('5h', r'5h\x1b\[0m \x1b\[([0-9;]+)m\d'),
                          ('wk', r'wk\x1b\[0m \x1b\[([0-9;]+)m\d')):
        m = re.search(pattern, line)
        if m:
            found.append(f'{part} {COLOR_NAMES.get(m.group(1), "plain") if m.group(1) else "plain"}')
    return found


def run(cmd, payload, extra_env=None):
    env = dict(os.environ, STATUSLINE_NOW=str(N), CLAUDE_CONFIG_DIR=NO_CONFIG)
    env.pop('CLAUDE_CODE_DISABLE_ADVISOR_TOOL', None)
    env.update(extra_env or {})
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

    for name, payload, extra_env in CASES:
        b = run(py_cmd, payload, extra_env)
        if ps_cmd:
            a = run(ps_cmd, payload, extra_env)
            ok = a == b and b.endswith(b'\n') and b.count(b'\n') == 1
            check(f'parity: {name}', ok, f'      ps1: {a!r}\n      py : {b!r}', strip(b))
        if name in EXPECTED_COLORS:
            got = colors_in(b.decode('utf-8'))
            missing = [c for c in EXPECTED_COLORS[name] if c not in got]
            check(f'colors: {name}', not missing, f'      expected {missing}, got {got}', strip(b))
        if name in EXPECT:
            want = EXPECT[name].encode('utf-8')
            check(f'exact: {name}', b == want, f'      want: {want!r}\n      got : {b!r}', strip(b))
        if name in EXPECT_ADVISOR:
            m = re.search(r'advisor: (.*?)(?: \||$)', strip(b))
            got = m[1] if m else None
            check(f'advisor: {name}', got == EXPECT_ADVISOR[name],
                  f'      want: {EXPECT_ADVISOR[name]!r}\n      got : {got!r}', strip(b))

    # The advisor part sits between effort and Ctx
    line = strip(run(py_cmd, CASES[0][1]))
    check('advisor placement', 'effort: medium | advisor: off | Ctx: 8%' in line, f'      got: {line}', line)

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
