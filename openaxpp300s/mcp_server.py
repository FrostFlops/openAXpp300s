"""MCP server for the AX PP300S bench power supply: Model Context Protocol over stdio, standard library only.

A thin shell over the openaxpp300s CLI. Every tool runs one CLI command in a child process (the openaxpp300s package
next to this server, with the same home folder), so the CLI's validation, bench limits, safe ordering, port lock and
error codes apply unchanged, and a hung serial driver never takes the server down. Logs and sequences run as
background jobs (detached children); their output stays in state/mcp_jobs/<job>/, so a restarted server still sees
them, and they keep running if the client disconnects. The port is taken per transaction, so foreground tools keep
working while a job runs.

Run:          python -m openaxpp300s.mcp_server      (or the openaxpp300s-mcp script)
Claude Code:  claude mcp add -s user openaxpp300s -- openaxpp300s-mcp
"""
import json
import math
import os
import subprocess
import sys
import threading
import time

from . import __version__, state
from .paths import PKG_PARENT, data_dir, ensure, home, state_dir

PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
STRUCTURED_SINCE = '2025-06-18'
DEFAULT_WALL_S = 30.0
JOB_START_WAIT_S = 10.0
STOP_WAIT_S = 10.0
TAIL_BYTES = 256 * 1024

BOOT = ('import sys; sys.path[:] = [%r] + [p for p in sys.path if p not in ("", ".")]; '
        'from openaxpp300s.cli import main; sys.exit(main())' % PKG_PARENT)

INSTRUCTIONS = """Control an AX PP300S buck-boost bench power supply (0-30 V, 0-10.1 A, 300 W) over its USB serial port.
Start with `status`: measured output voltage/current/power, input voltage, temperature, CV/CC, output on/off, the
values this tool sent last (the supply cannot report its settings) and the bench limits if the user set any.
`set` sends VSET, ISET, OVP, OCP, DELAY, REV, OPP, OTP and CV/CC priority in a safe order (it never knowingly trips a
protection; with the output on it waits until the output follows VSET). `output` switches the output on or off and
follows it. `param_list` writes the five parameter lists of the Chinese screen (VSET/ISET/OVP/OCP only act on the
Digital/Waveform screens; the screen is chosen on the supply, not over serial). `display_colors` sets the Digital
screen colours. `watch` samples the status for a while and returns statistics. `log_start` logs to a CSV/JSONL file in
the background, `sequence_start` runs timed voltage/current steps; follow both with `job_status`, end with `job_stop`.
`selftest` is read-only; `ports` finds the supply.
Values are SI units (V, A, W, C). Errors carry a code and a hint: NOT_FOUND (supply not on any USB serial port), BUSY
(port held by another program such as the vendor PC app), NO_REPLY (supply off or in its bootloader),
UNSAFE_OPERATION (over the bench limits or would trip a protection), NOT_APPLIED (acknowledged but the output did not
follow: only detectable with the output on, e.g. the supply shows the Chinese screen).
Every frame, status queries included, makes the supply rewrite one EEPROM cell (the setting under its front-panel
cursor; no wear levelling): poll slowly (watch/log default 1 s) and suggest selecting the DATA box on the supply for
long logs.
Safety: a power supply can damage what is connected. Before switching the output on or raising voltage/current, know
what is connected (ask the user if the bench limits and their note do not say). The bench limits (CLI `openaxpp300s
limits`) are the user's; never ask to raise them yourself without the user's OK. Do not deliberately trip
protections (OVP below the output etc.): the supply sounds an alarm that needs a key press on the front panel, and a
remote ON switches the output on again even then. Settings sent over serial survive a power cycle only if their field
was under the cursor (VSET by default); OPP/OTP/priority only when the settings menu is left on the supply."""


def _schema(props, required=()):
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


def _num(desc, lo, hi):
    return {'type': 'number', 'minimum': lo, 'maximum': hi, 'description': desc}


def _wall(default=DEFAULT_WALL_S):
    return {'type': 'number', 'minimum': 1, 'maximum': 3600,
            'description': 'time limit for the call in s (default %g)' % default}


COLOR_ELEMENTS = ['vout', 'iout', 'pout', 'aux_back', 'aux_font', 'bar_back', 'bar_label', 'bar_value',
                  'bar_onoff', 'bar_select']

TOOLS = [
    {'name': 'status', 'title': 'Power supply status',
     'description': 'Measured output (vout_v, iout_a, pout_w), input voltage vin_v, temperature temp_c, mode '
                    '(CV / CC / OFF), output_on, plus last_sent (values this tool sent; the supply cannot report its '
                    'settings, and the front panel may have changed them) and limits (bench limits set by the user).',
     'inputSchema': _schema({'max_wall': _wall()}), 'annotations': {'readOnlyHint': True}},
    {'name': 'set', 'title': 'Change settings',
     'description': 'Send settings in a safe order (protections that stay above the output first, setpoints, then '
                    'protections that must wait for the output to come down). With the output on, waits until the '
                    'output follows a new voltage (settled). Refused (UNSAFE_OPERATION): values over the bench '
                    'limits, OVP closer than max(0.2 V, 1.5 %) above VSET (given now or as sent last), an OVP/OCP '
                    'at or below what the terminals show now, OPP/OTP at the present power/temperature. VSET/ISET/'
                    'OVP/OCP/DELAY/REV act on the Digital/Waveform screens only: with the output on a VSET that the '
                    'output does not follow gives NOT_APPLIED; with the output off nothing can be verified, so check '
                    'vout_v after switching on.',
     'inputSchema': _schema({
         'voltage': _num('VSET in V, 10 mV steps', 0, 30.0),
         'current': _num('ISET (current limit) in A, 10 mA steps', 0.01, 10.1),
         'ovp': _num('over-voltage protection in V; must be >= VSET + max(0.2 V, 1.5 %)', 0.5, 30.5),
         'ocp': _num('over-current protection in A (trips after 100 ms at or above it); keep it above ISET',
                     0.1, 10.5),
         'delay': {'type': 'integer', 'minimum': 0, 'maximum': 9999,
                   'description': 'DELAY s: ON switches the output on this much later (0 = off)'},
         'rev': {'type': 'integer', 'minimum': 0, 'maximum': 9999,
                 'description': 'REV s: after ON the output toggles every REV s (0 = off)'},
         'opp': {'type': 'integer', 'minimum': 10, 'maximum': 310, 'description': 'over-power protection W, '
                                                                                  'multiple of 10'},
         'otp': {'type': 'integer', 'enum': [60, 70, 80, 90, 100], 'description': 'over-temperature protection C'},
         'priority': {'type': 'string', 'enum': ['cv', 'cc'], 'description': 'CV first or CC first'},
         'wait': {'type': 'boolean', 'description': 'wait for the output to follow a new VSET (default true)'},
         'max_wall': _wall()})},
    {'name': 'output', 'title': 'Switch the output',
     'description': 'Switch the output on (honours DELAY/REV; waits until the voltage is up: ~1.1 s soft start, '
                    'about 1.5 s in all; with DELAY or REV over 20 s it returns right after the frame) or '
                    'off (waits until it is down). With a bench voltage limit set, an output that rises above it is '
                    'switched off again at once (UNSAFE_OPERATION). The voltage that appears is whatever is in force '
                    'on the supply (on the Chinese screen: the selected parameter list): check status.vout_v. Know '
                    'what is connected before switching on. Do not use while the supply\'s settings menu is open.',
     'inputSchema': _schema({'on': {'type': 'boolean'},
                             'wait': {'type': 'boolean', 'description': 'follow the output (default true)'},
                             'wait_timeout': _num('s to follow it (default soft start + DELAY/REV + 1.5)', 0.1, 3600),
                             'max_wall': _wall()}, ['on'])},
    {'name': 'param_list', 'title': 'Chinese-screen parameter lists',
     'description': 'Write one of the five parameter lists (0..4) used by the Chinese screen: voltage, current, OVP, '
                    'OCP, cut-off current (output off after the current stays below it for about 6 s; 0 = off) and '
                    'cut-off time (output off when the accumulated on-time reaches it; fields not given are 0). '
                    'select makes it the active list, sent after the values; it is refused when the list\'s values '
                    'are unknown to this tool and a bench voltage limit is set or the output is on. These registers '
                    'act only while the supply shows the Chinese screen.',
     'inputSchema': _schema({'list': {'type': 'integer', 'minimum': 0, 'maximum': 4},
                             'select': {'type': 'boolean'},
                             'voltage': _num('V', 0, 30.0), 'current': _num('A', 0.01, 10.1),
                             'ovp': _num('V', 0.5, 30.5), 'ocp': _num('A', 0.1, 10.5),
                             'cut_current': _num('A (0 = off)', 0, 10.0),
                             'cut_time': {'type': 'string', 'description': '"1h30m", "45m", "50s", "H:M:S" or '
                                          '"off" (99:60:60); hours 0..99, minutes/seconds 0..60; 0:0:0 refused'},
                             'max_wall': _wall()}, ['list'])},
    {'name': 'display_colors', 'title': 'Display colours',
     'description': 'Set colours of the Digital screen (firmware >= 1.4.4; saved in the supply\'s EEPROM). colors: '
                    '{element: "#RRGGBB" | "0xRGB565" | name}; elements: ' + ', '.join(COLOR_ELEMENTS) +
                    '. reset: true sends the vendor defaults.',
     'inputSchema': _schema({'colors': {'type': 'object', 'additionalProperties': {'type': 'string'}},
                             'reset': {'type': 'boolean'}, 'max_wall': _wall()})},
    {'name': 'watch', 'title': 'Sample the output',
     'description': 'Query the status every `every` s for `seconds` (or `count` samples) and return statistics '
                    '(min/max/mean of vout/iout/pout/vin/temp, modes seen, Ah/Wh) and the last sample. For longer '
                    'runs or a file use log_start.',
     'inputSchema': _schema({'seconds': _num('duration in s (default 5)', 0.1, 600),
                             'count': {'type': 'integer', 'minimum': 1, 'maximum': 10000},
                             'every': _num('interval s (default 1)', 0.05, 60), 'max_wall': _wall(660)})},
    {'name': 'log_start', 'title': 'Start a background log',
     'description': 'Log the status to a CSV (or JSONL) file in the background: pc_time, local_time, vout_v, iout_a, '
                    'pout_w, vin_v, temp_c, mode, output_on, cumulative ah and wh. Returns a job id; follow with '
                    'job_status, end with job_stop. Other tools keep working meanwhile.',
     'inputSchema': _schema({'out': {'type': 'string', 'description': 'file path (default: data/log_<time>.csv)'},
                             'format': {'type': 'string', 'enum': ['csv', 'jsonl']},
                             'every': _num('s between samples (default 1)', 0.05, 3600),
                             'duration': {'type': 'string', 'description': '30s, 10m, 2h or forever (default '
                                                                           'forever)'}})},
    {'name': 'sequence_start', 'title': 'Run a voltage/current sequence',
     'description': 'Run timed steps in the background: each step sets VSET and ISET, switches the output on and '
                    'holds `seconds`. steps: [{voltage, current, seconds}, ...] or file: a CSV like the vendor '
                    'template (voltage,current,seconds). repeat cycles (0 = until job_stop). end: off (default, '
                    'output off at the end or on job_stop) or keep. Optional log file. Bench limits apply to every '
                    'step (checked before it starts).',
     'inputSchema': _schema({'steps': {'type': 'array', 'minItems': 1, 'maxItems': 1000, 'items': _schema({
                                 'voltage': _num('V', 0, 30.0), 'current': _num('A', 0.01, 10.1),
                                 'seconds': _num('s', 0, 864000)}, ['voltage', 'current', 'seconds'])},
                             'file': {'type': 'string'},
                             'repeat': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
                             'end': {'type': 'string', 'enum': ['off', 'keep']},
                             'every': _num('status poll s during a step (default 1)', 0.05, 60),
                             'log': {'type': 'string', 'description': 'also log to this CSV/JSONL file'}})},
    {'name': 'job_status', 'title': 'Background job status',
     'description': 'State of a log or sequence job (default: the latest): running, the started event, the last '
                    'sample/step, events (link lost ...), and the summary once it has ended.',
     'inputSchema': _schema({'job': {'type': 'string'}}), 'annotations': {'readOnlyHint': True}},
    {'name': 'job_stop', 'title': 'Stop a background job',
     'description': 'Stop a log or sequence job (default: every running one) cleanly and return its summary. A '
                    'sequence switches the output off unless it was started with end: keep.',
     'inputSchema': _schema({'job': {'type': 'string'}})},
    {'name': 'selftest', 'title': 'Self-test',
     'description': 'Read-only checks: port, status replies and checksums, latency, input voltage and temperature '
                    'plausible, power = V x I, a bad-checksum frame ignored. Never changes a setting.',
     'inputSchema': _schema({'max_wall': _wall()}), 'annotations': {'readOnlyHint': True}},
    {'name': 'ports', 'title': 'Find the supply',
     'description': 'List serial ports and ask each USB one for a PP300S status (Bluetooth ports are skipped). The '
                    'port that answers is remembered for later calls.',
     'inputSchema': _schema({'max_wall': _wall()}), 'annotations': {'readOnlyHint': True}},
]
TOOL_BY_NAME = {t['name']: t for t in TOOLS}


class ToolError(Exception):
    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.doc = {'ok': False, 'error': dict({'code': code, 'message': message}, **({'hint': hint} if hint else {}))}


_TYPES = {'string': (str,), 'integer': (int,), 'number': (int, float), 'boolean': (bool,), 'array': (list,),
          'object': (dict,)}


def _value(name, s, v):
    t = s.get('type')
    if t == 'integer' and isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) and t != 'boolean' or not isinstance(v, _TYPES[t]) \
            or (isinstance(v, float) and not math.isfinite(v)):
        raise ToolError('INVALID_ARG', '%s must be of type %s' % (name, t))
    if 'enum' in s and v not in s['enum']:
        raise ToolError('INVALID_ARG', '%s must be one of %s' % (name, ', '.join(map(str, s['enum']))))
    if 'minimum' in s and v < s['minimum']:
        raise ToolError('INVALID_ARG', '%s must be >= %g' % (name, s['minimum']))
    if 'maximum' in s and v > s['maximum']:
        raise ToolError('INVALID_ARG', '%s must be <= %g' % (name, s['maximum']))
    if t == 'array':
        if not s.get('minItems', 0) <= len(v) <= s.get('maxItems', len(v)):
            raise ToolError('INVALID_ARG', '%s takes %d to %d items' % (name, s.get('minItems', 0),
                                                                        s.get('maxItems', len(v))))
        v = [_value('%s[%d]' % (name, i), s['items'], x) for i, x in enumerate(v)]
    if t == 'object':
        if 'properties' in s:
            v = check_args({'name': name, 'inputSchema': s}, v, prefix=name + '.')
        elif 'additionalProperties' in s and isinstance(s['additionalProperties'], dict):
            v = {k: _value('%s.%s' % (name, k), s['additionalProperties'], x) for k, x in v.items()}
    return v


def check_args(tool, a, prefix=''):
    props = tool['inputSchema']['properties']
    unknown = sorted(set(a) - set(props))
    if unknown:
        raise ToolError('INVALID_ARG', 'unknown argument(s) for %s: %s' % (tool['name'], ', '.join(unknown)),
                        'arguments: %s' % (', '.join(props) or 'none'))
    out = {}
    for k, v in a.items():
        if v is not None:
            out[k] = _value(prefix + k, props[k], v)
    for k in tool['inputSchema'].get('required', ()):
        if k not in out:
            raise ToolError('INVALID_ARG', '%s needs %s' % (tool['name'], k))
    return out


def _flag(flag, value):
    value = str(value)
    return [flag + '=' + value] if value.startswith('-') else [flag, value]


def _g(x):
    """A number as the CLI reads it (no exponent, no trailing .0 noise)."""
    if isinstance(x, bool):
        return str(int(x))
    if isinstance(x, int):
        return str(x)
    return repr(float(x))


# ------------------------------------------------------------------------------------------------ child processes

def child_env():
    env = dict(os.environ)
    env['OPENAXPP300S_HOME'] = home()
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def _no_window():
    return 0x08000000 if os.name == 'nt' else 0             # CREATE_NO_WINDOW


def _json_lines(data):
    out = []
    for line in data.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


class Cli:
    def __init__(self, argv0=None):
        self.argv0 = argv0 or [sys.executable, '-c', BOOT]

    def run(self, args, wall, cancel=None, kill_on_cancel=True):
        """Run one CLI command -> (docs, exit code, stderr text). TIMEOUT when it runs past wall (plus a grace
        period for a command that changes the supply, so it can record what it sent). With kill_on_cancel False a
        cancelled command is left to finish (they are short) before CANCELLED is raised: killing it between a frame
        and its record would leave the tool's record of the settings wrong."""
        p = subprocess.Popen(self.argv0 + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, env=child_env(), creationflags=_no_window())
        box = {}
        th = threading.Thread(target=lambda: box.update(zip(('out', 'err'), p.communicate())), daemon=True)
        th.start()
        t_end = time.time() + wall + (0 if kill_on_cancel else 10.0)
        while th.is_alive():
            th.join(0.1)
            cancelled = cancel is not None and cancel.is_set()
            if cancelled and kill_on_cancel or time.time() > t_end:
                p.kill()
                th.join(5)
                if cancelled:
                    raise ToolError('CANCELLED', 'the call was cancelled')
                raise ToolError('TIMEOUT', '%s did not finish within %g s' % (args[0], wall),
                                'raise max_wall; check the supply with status (a change may have been sent)')
        if cancel is not None and cancel.is_set():
            raise ToolError('CANCELLED', 'the call was cancelled (the command had already run to its end)')
        out = box.get('out', b'').decode('utf-8', 'replace')
        err = box.get('err', b'').decode('utf-8', 'replace')
        return _json_lines(out), p.returncode, err

    def start(self, args, out_path, err_path):
        flags = _no_window()
        if os.name == 'nt':
            flags |= 0x00000008 | 0x00000200                 # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw = {'start_new_session': True} if os.name != 'nt' else {}
        with open(out_path, 'wb') as fo, open(err_path, 'wb') as fe:
            return subprocess.Popen(self.argv0 + list(args), stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                                    env=child_env(), creationflags=flags, close_fds=True, **kw)


def jobs_dir():
    return ensure(os.path.join(state_dir(), 'mcp_jobs'))


def _read_tail(path, n=TAIL_BYTES):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            data = f.read().decode('utf-8', 'replace')
        if size > n:
            data = data.split('\n', 1)[1] if '\n' in data else ''
        return data
    except OSError:
        return ''


def _read_head(path, n=16 * 1024):
    try:
        with open(path, 'rb') as f:
            return f.read(n).decode('utf-8', 'replace')
    except OSError:
        return ''


def _last_row(path, fmt=None):
    """The last data row of a log file (CSV with header, or JSONL), or None."""
    tail = _read_tail(path, 4096).strip().splitlines()
    if not tail:
        return None
    if (fmt or ('jsonl' if path.lower().endswith('.jsonl') else 'csv')) == 'jsonl':
        for line in reversed(tail):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if 'vout_v' in d:
                return d
        return None
    import csv
    head = _read_head(path, 1024).splitlines()
    if not head or len(tail) < 1 or tail[-1] == head[0]:
        return None
    cols = next(csv.reader([head[0]]))
    vals = next(csv.reader([tail[-1]]))
    return dict(zip(cols, vals)) if len(vals) == len(cols) else None


class Job:
    def __init__(self, job, kind, pid, args, t0):
        self.job, self.kind, self.pid, self.args, self.t0 = job, kind, pid, args, t0

    @property
    def dir(self):
        return os.path.join(jobs_dir(), self.job)

    def save(self):
        ensure(self.dir)
        state.write_json(os.path.join(self.dir, 'meta.json'), {'job': self.job, 'kind': self.kind, 'pid': self.pid,
                                                               'args': self.args, 't0': self.t0})

    @classmethod
    def load(cls, name):
        m = state.read_json(os.path.join(jobs_dir(), name, 'meta.json'))
        if not m:
            return None
        return cls(m['job'], m['kind'], m['pid'], m['args'], m['t0'])

    @classmethod
    def all(cls):
        out = []
        for name in os.listdir(jobs_dir()):
            j = cls.load(name)
            if j:
                out.append(j)
        return sorted(out, key=lambda j: j.t0)

    def cli_pid(self):
        """The pid the CLI process reports in its 'started' line (under a Windows venv, Popen.pid is the
        launcher's, not the interpreter's)."""
        st = self.started() or {}
        return st.get('pid') or self.pid

    def alive(self):
        if not state.pid_alive(self.pid):
            return False
        cp = self.cli_pid()
        if cp != self.pid and not state.pid_alive(cp):
            return False
        reg = [r for r in state.list_running() if r.get('pid') == cp]
        if self.started() is not None and not reg:
            return False                          # alive pid but no registry entry: the pid was reused
        return True

    def running(self):
        return self.final() is None and self.alive()

    def lines(self):
        return _json_lines(_read_tail(os.path.join(self.dir, 'stdout.jsonl')))

    def started(self):
        for d in _json_lines(_read_head(os.path.join(self.dir, 'stdout.jsonl'))):
            if d.get('event') == 'started':
                return d
        return None

    def final(self):
        for d in reversed(self.lines()):
            if d.get('event') == 'done' or d.get('ok') is False:
                return d
        return None

    def status(self):
        lines = self.lines()
        final = self.final()
        d = {'job': self.job, 'kind': self.kind, 'pid': self.cli_pid(), 'running': not final and self.alive(),
             'elapsed_s': round(time.time() - self.t0, 1), 'started': self.started()}
        samples = [x for x in lines if x.get('type') == 'sample']
        steps = [x for x in lines if x.get('event') == 'step']
        events = [x for x in lines if x.get('event') in ('link_lost', 'reconnected', 'status')]
        if steps:
            d['last_step'] = steps[-1]
        if events:
            d['events'] = events[-5:]
        if samples:
            d['last_sample'] = samples[-1]
        started = d['started'] or {}
        if started.get('file') and not samples:
            row = _last_row(started['file'], started.get('format'))
            if row:
                d['last_row'] = row
        if final:
            d['result'] = final
        elif not d['running']:
            err = _read_tail(os.path.join(self.dir, 'stderr.txt'), 2000).strip()
            d['result'] = {'ok': False, 'error': {'code': 'JOB_DIED', 'message': 'the job ended without a summary',
                                                  'stderr': err[-1500:]}}
        return d


# ------------------------------------------------------------------------------------------------ server

def _rpc_error(mid, code, message):
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def _rpc_result(mid, result):
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


class Server:
    def __init__(self, cli=None, out=None):
        self.cli = cli or Cli()
        self.out = out or sys.stdout.buffer
        self.wlock = threading.Lock()
        self.version = PROTOCOL_VERSIONS[0]
        self.calls = threading.Lock()
        self.inflight = {}
        self.cancelled = set()
        self.job_lock = threading.Lock()

    def send(self, msg):
        data = (json.dumps(msg, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')
        with self.wlock:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):
                pass

    def serve(self, inp=None):
        inp = inp or sys.stdin.buffer
        for raw in iter(inp.readline, b''):
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.send(_rpc_error(None, -32700, 'parse error'))
                continue
            for m in (msg if isinstance(msg, list) and msg else [msg]):
                try:
                    self.dispatch(m)
                except Exception as e:  # noqa: BLE001
                    if isinstance(m, dict) and m.get('id') is not None:
                        self.send(_rpc_error(m['id'], -32603, 'internal error: %r' % e))

    def dispatch(self, m):
        if not isinstance(m, dict):
            return self.send(_rpc_error(None, -32600, 'invalid request'))
        method, mid, params = m.get('method'), m.get('id'), m.get('params') or {}
        if method is None:
            if 'result' in m or 'error' in m:
                return
            return self.send(_rpc_error(mid, -32600, 'invalid request: no method'))
        if not isinstance(params, dict):
            if mid is not None:
                self.send(_rpc_error(mid, -32602, 'params must be an object'))
            return
        if mid is None:
            if method == 'notifications/cancelled':
                rid = params.get('requestId')
                with self.calls:
                    ev = self.inflight.get(rid)
                    if ev is not None:
                        self.cancelled.add(rid)
                        ev.set()
            return
        if method == 'initialize':
            want = params.get('protocolVersion')
            self.version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return self.send(_rpc_result(mid, {
                'protocolVersion': self.version, 'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'openaxpp300s', 'title': 'AX PP300S bench power supply',
                               'version': __version__},
                'instructions': INSTRUCTIONS}))
        if method == 'ping':
            return self.send(_rpc_result(mid, {}))
        if method == 'tools/list':
            return self.send(_rpc_result(mid, {'tools': TOOLS}))
        if method == 'tools/call':
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                return self.send(_rpc_error(mid, -32602, 'arguments must be an object'))
            with self.calls:
                if mid in self.inflight:
                    return self.send(_rpc_error(mid, -32600, 'request id in use'))
                self.inflight[mid] = threading.Event()
            threading.Thread(target=self.call, args=(mid, params.get('name'), args), daemon=True).start()
            return
        self.send(_rpc_error(mid, -32601, 'unknown method %s' % method))

    def call(self, mid, name, args):
        try:
            msg = self._call(mid, name, args)
        except Exception as e:  # noqa: BLE001
            msg = _rpc_error(mid, -32603, 'internal error: %r' % e)
        with self.calls:
            ev = self.inflight.pop(mid, None)
            dropped = mid in self.cancelled
            self.cancelled.discard(mid)
        if not dropped and ev is not None:
            self.send(msg)

    def _call(self, mid, name, args):
        tool = TOOL_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return _rpc_error(mid, -32602, 'unknown tool %s' % name)
        try:
            doc = getattr(self, 'tool_' + name)(check_args(tool, args), self.inflight.get(mid))
            err = not doc.get('ok', False)
        except ToolError as e:
            doc, err = e.doc, True
        except Exception as e:  # noqa: BLE001
            doc, err = {'ok': False, 'error': {'code': 'INTERNAL', 'message': repr(e)}}, True
        result = {'content': [{'type': 'text', 'text': self.text(doc)}], 'isError': err}
        if self.version >= STRUCTURED_SINCE:
            result['structuredContent'] = doc
        return _rpc_result(mid, result)

    @staticmethod
    def text(doc):
        body = json.dumps(doc, indent=1, ensure_ascii=False)
        if doc.get('ok', False):
            return body
        e = doc.get('error') or {}
        head = '%s: %s' % (e.get('code'), e.get('message'))
        if e.get('hint'):
            head += '\nhint: %s' % e['hint']
        return head + '\n' + body

    def _run(self, args, wall, cancel, change=False):
        docs, code, err = self.cli.run(args, wall, cancel, kill_on_cancel=not change)
        if not docs:
            raise ToolError('INTERNAL', 'the CLI printed nothing (exit %s)' % code, err.strip()[-800:] or None)
        return docs[-1]

    # ------------------------------------------------------------ tools
    def tool_status(self, a, cancel):
        return self._run(['status'], a.get('max_wall', DEFAULT_WALL_S), cancel)

    def tool_set(self, a, cancel):
        args = ['set']
        for k in ('voltage', 'current', 'ovp', 'ocp', 'delay', 'rev', 'opp', 'otp', 'priority'):
            if k in a:
                args += _flag('--' + k, a[k] if k == 'priority' else _g(a[k]))
        if len(args) == 1:
            raise ToolError('INVALID_ARG', 'nothing to set', 'give voltage, current, ovp, ocp, delay, rev, opp, otp '
                                                             'or priority')
        if a.get('wait') is False:
            args.append('--no-wait')
        return self._run(args, a.get('max_wall', DEFAULT_WALL_S), cancel, change=True)

    def tool_output(self, a, cancel):
        args = ['on' if a['on'] else 'off']
        wall = a.get('max_wall', max(DEFAULT_WALL_S, a.get('wait_timeout', 0) + 15))
        if a.get('wait') is False:
            args.append('--no-wait')
        elif 'wait_timeout' in a:
            args += ['--wait-timeout', _g(a['wait_timeout'])]
        else:                                  # let the CLI give up (settled false + note) before the wall
            args += ['--wait-timeout', _g(max(1.0, wall - 8.0))]
        doc = self._run(args, wall, cancel, change=True)
        if a['on'] and doc.get('ok') and doc.get('settled') is False and not doc.get('note'):
            doc['note'] = 'switched on but not settled: check vout_v in status'
        return doc

    def tool_param_list(self, a, cancel):
        n = str(a['list'])
        vals = [k for k in ('voltage', 'current', 'ovp', 'ocp', 'cut_current', 'cut_time') if k in a]
        if not vals:
            if not a.get('select'):
                raise ToolError('INVALID_ARG', 'nothing to do', 'give select: true and/or values')
            return self._run(['list', 'select', n], a.get('max_wall', DEFAULT_WALL_S), cancel, change=True)
        args = ['list', 'set', n]
        for k in vals:
            args += _flag('--' + k.replace('_', '-'), a[k] if k == 'cut_time' else _g(a[k]))
        if a.get('select'):
            args.append('--select')
        return self._run(args, a.get('max_wall', DEFAULT_WALL_S), cancel, change=True)

    def tool_display_colors(self, a, cancel):
        if a.get('reset'):
            if a.get('colors'):
                raise ToolError('INVALID_ARG', 'give colors or reset, not both')
            return self._run(['color', 'reset'], a.get('max_wall', DEFAULT_WALL_S), cancel, change=True)
        if not a.get('colors'):
            raise ToolError('INVALID_ARG', 'give colors {element: colour} or reset: true',
                            'elements: ' + ', '.join(COLOR_ELEMENTS))
        return self._run(['color', 'set'] + ['%s=%s' % (k, v) for k, v in a['colors'].items()],
                         a.get('max_wall', DEFAULT_WALL_S), cancel, change=True)

    def tool_watch(self, a, cancel):
        args = ['watch', '--quiet', '--every', _g(a.get('every', 1.0))]
        if 'count' in a:
            args += ['--count', str(a['count'])]
        if 'seconds' in a or 'count' not in a:
            args += ['--for', _g(a.get('seconds', 5.0))]
        secs = a.get('seconds', 5.0) if 'count' not in a else a['count'] * a.get('every', 1.0) + 5
        return self._run(args, a.get('max_wall', secs + 30), cancel)

    def tool_selftest(self, a, cancel):
        return self._run(['selftest'], a.get('max_wall', DEFAULT_WALL_S), cancel)

    def tool_ports(self, a, cancel):
        return self._run(['ports', '--probe'], a.get('max_wall', DEFAULT_WALL_S), cancel)

    # ------------------------------------------------------------ jobs
    def _start_job(self, kind, args, cancel=None):
        with self.job_lock:
            if cancel is not None and cancel.is_set():
                raise ToolError('CANCELLED', 'the call was cancelled before the job started')
            ts = time.strftime('%Y%m%d-%H%M%S')
            n = len(os.listdir(jobs_dir())) + 1
            while True:                               # claim the folder atomically (other servers start jobs too)
                name = '%s-%s-%d' % (kind, ts, n)
                jdir = os.path.join(jobs_dir(), name)
                try:
                    os.mkdir(jdir)
                    break
                except FileExistsError:
                    n += 1
            p = self.cli.start(args, os.path.join(jdir, 'stdout.jsonl'), os.path.join(jdir, 'stderr.txt'))
            job = Job(name, kind, p.pid, args, time.time())
            job.save()
        t_end = time.time() + JOB_START_WAIT_S
        while time.time() < t_end:
            st = job.status()
            if st.get('started') or st.get('result'):
                break
            time.sleep(0.1)
        st = job.status()
        if st.get('result') and st['result'].get('ok') is False:
            return dict({'ok': False, 'job': name}, error=st['result'].get('error'))
        if cancel is not None and cancel.is_set():        # the reply is dropped: do not leave it running unseen
            state.request_stop(pid=job.cli_pid())
        return dict({'ok': True}, **st)

    def tool_log_start(self, a, cancel):
        fmt = a.get('format')
        out = a.get('out') or os.path.join(data_dir(), time.strftime('log_%Y%m%d_%H%M%S') + '.' + (fmt or 'csv'))
        args = (['log'] + _flag('--out', out) + ['--every', _g(a.get('every', 1.0))] +
                _flag('--duration', a.get('duration', 'forever')) + ['--status-every', '10s'])
        if fmt:
            args += ['--format', fmt]
        return self._start_job('log', args, cancel)

    def tool_sequence_start(self, a, cancel):
        if bool(a.get('steps')) == bool(a.get('file')):
            raise ToolError('INVALID_ARG', 'give steps or file (one of them)')
        args = ['sequence']
        if not a.get('file'):
            args += _flag('--steps', ';'.join('%s,%s,%s' % (_g(s['voltage']), _g(s['current']), _g(s['seconds']))
                                              for s in a['steps']))
        for k, flag in (('repeat', '--repeat'), ('end', '--end'), ('every', '--every'), ('log', '--log')):
            if k in a:
                args += _flag(flag, _g(a[k]) if isinstance(a[k], (int, float)) else a[k])
        tail = ['--', a['file']] if a.get('file') else []
        # validate (ranges, bench limits, OVP headroom) before starting the background job
        docs, code, err = self.cli.run(args + ['--dry-run'] + tail, DEFAULT_WALL_S, cancel)
        if code != 0 or not docs:
            return docs[-1] if docs else {'ok': False, 'error': {'code': 'INTERNAL', 'message': err[-800:]}}
        return self._start_job('sequence', args + tail, cancel)

    def _job(self, name):
        if name:
            j = Job.load(name)
            if j is None:
                raise ToolError('INVALID_ARG', 'unknown job %s' % name,
                                'jobs: %s' % ', '.join(x.job for x in Job.all()[-10:]))
            return j
        jobs = Job.all()
        if not jobs:
            raise ToolError('INVALID_ARG', 'no jobs yet', 'start one with log_start or sequence_start')
        return jobs[-1]

    def tool_job_status(self, a, cancel):
        return dict({'ok': True}, **self._job(a.get('job')).status())

    def tool_job_stop(self, a, cancel):
        jobs = [self._job(a['job'])] if a.get('job') else [j for j in Job.all() if j.running()]
        if not jobs:
            return {'ok': True, 'stopped': [], 'note': 'no job running'}
        ended_before = {j.job: not j.running() for j in jobs}
        for j in jobs:
            if not ended_before[j.job]:
                state.request_stop(pid=j.cli_pid())
        out = []
        t_end = time.time() + STOP_WAIT_S
        for j in jobs:
            while j.running() and time.time() < t_end:
                time.sleep(0.1)
            st = j.status()
            st['already_ended'] = ended_before[j.job]
            if st['running']:
                st['note'] = 'still running %g s after the stop request' % STOP_WAIT_S
            out.append(st)
        return {'ok': all(not s['running'] for s in out), 'stopped': out}


def main():
    Server().serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
