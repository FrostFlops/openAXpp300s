"""Tests of the MCP server (openaxpp300s/mcp_server.py).

- JSON-RPC over stdio: initialize (version negotiation), ping, tools/list (schemas), errors, batches, cancellation;
  Server.serve() reads an in-memory stdin, its out is a list-backed writer (tools/call answers from threads).
- check_args: unknown arguments, types, bool-for-number, NaN, enums, ranges, nested step items, colour objects.
- The CLI command line every tool builds, recorded by a fake Cli and parsed back with the real CLI parser.
- A few real child runs through the default Cli against the virtual supply (status, set + output, a log job and
  sequence jobs), plus the server itself as a stdio process.

Never touches hardware: conftest sets OPENAXPP300S_SIM=1, OPENAXPP300S_NO_SERIAL=1, OPENAXPP300S_SIM_DELAY=0 and a
per-test OPENAXPP300S_HOME; the server's children inherit that environment (checked before any real run).
"""
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import types

import pytest

from openaxpp300s import __version__, protocol as P, runner, sim, state
from openaxpp300s import cli as cli_mod
from openaxpp300s import mcp_server as M
from openaxpp300s.errors import PP300SError
from openaxpp300s.paths import data_dir, home

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEP = {'voltage': 5, 'current': 1, 'seconds': 2}
TOOL_NAMES = ['status', 'set', 'output', 'param_list', 'display_colors', 'watch', 'log_start', 'sequence_start',
              'job_status', 'job_stop', 'selftest', 'ports']


# ================================================================================================ helpers

class Out:
    """Writer for Server(out=...): keeps every raw write and the JSON-RPC message in it."""

    def __init__(self):
        self.raw = []
        self.msgs = []
        self.cond = threading.Condition()

    def write(self, data):
        try:
            msg = json.loads(data.decode('utf-8'))
        except ValueError:
            msg = {'_unparsed': repr(data)}
        with self.cond:
            self.raw.append(bytes(data))
            self.msgs.append(msg)
            self.cond.notify_all()
        return len(data)

    def flush(self):
        pass

    def by_id(self, mid):
        return [m for m in self.msgs if 'id' in m and m['id'] == mid and type(m['id']) is type(mid)]

    def one(self, mid):
        got = self.by_id(mid)
        assert len(got) == 1, 'expected one answer to id %r, got %r (all: %r)' % (mid, got, self.msgs)
        return got[0]

    def wait_ids(self, ids, timeout=15.0):
        t_end = time.monotonic() + timeout
        with self.cond:
            while not all(self.by_id(i) for i in ids):
                left = t_end - time.monotonic()
                assert left > 0, 'no answer to %r within %g s; got %r' % (ids, timeout, self.msgs)
                self.cond.wait(left)
        return {i: self.one(i) for i in ids}


class FakeCli:
    """Stands in for mcp_server.Cli: records each command line, answers with canned documents, and fakes a
    background job that has already ended (pid 0 never counts as alive)."""

    def __init__(self, reply=None):
        self.reply = reply
        self.runs = []                     # (args, wall, cancel)
        self.kill_on_cancel = []           # the kill_on_cancel of each run, in order
        self.starts = []                   # args
        self.events = []                   # ('run' | 'start', args) in order
        self.start_lines = None
        self.start_stderr = ''

    def run(self, args, wall, cancel=None, kill_on_cancel=True, **kw):
        self.runs.append((list(args), wall, cancel))
        self.kill_on_cancel.append(kill_on_cancel)
        self.events.append(('run', list(args)))
        if self.reply is not None:
            return self.reply(list(args), wall, cancel)
        return [{'ok': True, 'cmd': args[0], 'argv': list(args)}], 0, ''

    def start(self, args, out_path, err_path):
        self.starts.append(list(args))
        self.events.append(('start', list(args)))
        lines = self.start_lines if self.start_lines is not None else [
            {'event': 'started', 'fake': True, 't': round(time.time(), 3)},
            {'ok': True, 'cmd': args[0], 'event': 'done', 'stopped_by': 'end'}]
        with open(out_path, 'w', encoding='utf-8') as f:
            for d in lines:
                f.write(json.dumps(d) + '\n')
        with open(err_path, 'w', encoding='utf-8') as f:
            f.write(self.start_stderr)
        return types.SimpleNamespace(pid=0)


def make_server(reply=None):
    f = FakeCli(reply)
    return f, M.Server(cli=f, out=Out())


def rpc(mid, method, params=None):
    m = {'jsonrpc': '2.0', 'id': mid, 'method': method}
    if params is not None:
        m['params'] = params
    return m


def note(method, params=None):
    m = {'jsonrpc': '2.0', 'method': method}
    if params is not None:
        m['params'] = params
    return m


def tcall(mid, name, arguments=None):
    return rpc(mid, 'tools/call', {'name': name, 'arguments': {} if arguments is None else arguments})


def feed(srv, *msgs):
    """Serve these lines from an in-memory stdin (dicts/lists are JSON-encoded, str/bytes go as they are)."""
    data = b''
    for m in msgs:
        if isinstance(m, (dict, list)):
            m = json.dumps(m)
        if isinstance(m, str):
            m = m.encode('utf-8')
        data += m if m.endswith(b'\n') else m + b'\n'
    srv.serve(io.BytesIO(data))
    return srv.out


def wait_idle(srv, timeout=10.0):
    t_end = time.monotonic() + timeout
    while True:
        with srv.calls:
            if not srv.inflight:
                return
        assert time.monotonic() < t_end, 'calls still in flight: %r' % list(srv.inflight)
        time.sleep(0.01)


def call(srv, name, args=None, mid=1):
    """One tools/call, synchronously (Server._call) -> the MCP result."""
    msg = srv._call(mid, name, {} if args is None else args)
    assert msg['jsonrpc'] == '2.0' and msg['id'] == mid and 'result' in msg, msg
    return msg['result']


def doc(result):
    return result['structuredContent']


def argv_of(name, args):
    """The one foreground CLI command a tool runs -> (argv, wall)."""
    f, srv = make_server()
    res = call(srv, name, args)
    assert res['isError'] is False, res
    assert len(f.runs) == 1 and not f.starts, f.events
    return f.runs[0][0], f.runs[0][1]


def parse(argv):
    """argv as the real CLI parses it (PP300SError INVALID_ARG when it does not)."""
    return cli_mod.build_parser().parse_args(argv)


def error_of(result):
    assert result['isError'] is True, result
    return doc(result)['error']


# ================================================================================================ JSON-RPC

@pytest.mark.parametrize('want,got', [('2025-11-25', '2025-11-25'), ('2025-06-18', '2025-06-18'),
                                      ('2025-03-26', '2025-03-26'), ('2024-11-05', '2024-11-05'),
                                      ('1999-01-01', M.PROTOCOL_VERSIONS[0]), (None, M.PROTOCOL_VERSIONS[0]),
                                      (20250618, M.PROTOCOL_VERSIONS[0])])
def test_initialize_negotiates_the_protocol_version(want, got):
    f, srv = make_server()
    params = {'capabilities': {}, 'clientInfo': {'name': 'pytest', 'version': '1'}}
    if want is not None:
        params['protocolVersion'] = want
    srv.dispatch(rpc(1, 'initialize', params))
    res = srv.out.one(1)['result']
    assert res['protocolVersion'] == got and srv.version == got
    assert f.runs == []


def test_protocol_versions_latest_first():
    assert list(M.PROTOCOL_VERSIONS) == sorted(M.PROTOCOL_VERSIONS, reverse=True)
    assert M.STRUCTURED_SINCE in M.PROTOCOL_VERSIONS


def test_initialize_result_describes_the_server():
    _, srv = make_server()
    srv.dispatch(rpc('init-1', 'initialize'))          # no params at all
    m = srv.out.one('init-1')
    assert m['jsonrpc'] == '2.0' and 'error' not in m
    res = m['result']
    assert res['protocolVersion'] == M.PROTOCOL_VERSIONS[0]
    assert res['capabilities'] == {'tools': {'listChanged': False}}
    assert res['serverInfo']['name'] == 'openaxpp300s' and res['serverInfo']['version'] == __version__
    assert isinstance(res['serverInfo']['title'], str) and res['serverInfo']['title']
    assert res['instructions'] == M.INSTRUCTIONS
    for name in TOOL_NAMES:
        assert '`%s`' % name in res['instructions'], name
    for code in ('NOT_FOUND', 'BUSY', 'NO_REPLY', 'UNSAFE_OPERATION', 'NOT_APPLIED'):
        assert code in res['instructions']


def test_ping_and_ids_echoed():
    _, srv = make_server()
    out = feed(srv, rpc(0, 'ping'), rpc('abc', 'ping'), rpc(7, 'ping', {}))
    for mid in (0, 'abc', 7):
        assert out.one(mid) == {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    assert len(out.msgs) == 3


def test_every_write_is_one_json_line():
    _, srv = make_server()
    out = feed(srv, rpc(1, 'initialize'), rpc(2, 'tools/list'), rpc(3, 'ping'), tcall(4, 'status'))
    out.wait_ids([4])
    assert len(out.raw) == 4
    for data in out.raw:
        assert data.endswith(b'\n') and data.count(b'\n') == 1
        assert isinstance(json.loads(data.decode('utf-8')), dict)


def test_tools_list_is_the_tool_table():
    _, srv = make_server()
    srv.dispatch(rpc(1, 'tools/list'))
    tools = srv.out.one(1)['result']['tools']
    assert tools == json.loads(json.dumps(M.TOOLS))
    assert [t['name'] for t in tools] == TOOL_NAMES
    assert set(M.TOOL_BY_NAME) == set(TOOL_NAMES)


_TYPE_OK = {'string': lambda v: isinstance(v, str),
            'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
            'number': lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
            'boolean': lambda v: isinstance(v, bool),
            'array': lambda v: isinstance(v, list),
            'object': lambda v: isinstance(v, dict)}
_SCHEMA_KEYS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'minItems', 'maxItems', 'minimum',
                'maximum', 'enum', 'description'}


def _check_schema(s, path):
    assert isinstance(s, dict), path
    assert set(s) <= _SCHEMA_KEYS, (path, set(s) - _SCHEMA_KEYS)
    t = s.get('type')
    assert t in _TYPE_OK, (path, t)
    if 'description' in s:
        assert isinstance(s['description'], str) and s['description'].strip(), path
    if 'enum' in s:
        assert isinstance(s['enum'], list) and s['enum'], path
        assert all(_TYPE_OK[t](x) for x in s['enum']), path
        assert len({json.dumps(x) for x in s['enum']}) == len(s['enum']), path
    for k in ('minimum', 'maximum'):
        if k in s:
            assert t in ('number', 'integer') and _TYPE_OK['number'](s[k]), path
    if 'minimum' in s and 'maximum' in s:
        assert s['minimum'] <= s['maximum'], path
    if t == 'array':
        _check_schema(s['items'], path + '[]')
        for k in ('minItems', 'maxItems'):
            if k in s:
                assert _TYPE_OK['integer'](s[k]) and s[k] >= 0, path
        if 'minItems' in s and 'maxItems' in s:
            assert s['minItems'] <= s['maxItems'], path
    else:
        assert not {'items', 'minItems', 'maxItems'} & set(s), path
    if t == 'object':
        if 'properties' in s:
            props = s['properties']
            assert isinstance(props, dict), path
            for k, sub in props.items():
                assert re.fullmatch(r'[a-z][a-z0-9_]*', k), (path, k)
                _check_schema(sub, '%s.%s' % (path, k))
            req = s.get('required', [])
            assert isinstance(req, list) and len(set(req)) == len(req), path
            assert set(req) <= set(props), (path, set(req) - set(props))
            assert s.get('additionalProperties') is False, path
        else:
            _check_schema(s['additionalProperties'], path + '.*')
            assert 'required' not in s, path
    else:
        assert not {'properties', 'required', 'additionalProperties'} & set(s), path


@pytest.mark.parametrize('tool', M.TOOLS, ids=lambda t: t['name'])
def test_tool_has_a_valid_schema(tool):
    assert set(tool) <= {'name', 'title', 'description', 'inputSchema', 'annotations'}
    assert re.fullmatch(r'[a-z][a-z0-9_]{0,63}', tool['name'])
    assert isinstance(tool['title'], str) and tool['title'].strip()
    assert isinstance(tool['description'], str) and len(tool['description']) > 20
    s = tool['inputSchema']
    assert s['type'] == 'object' and isinstance(s['properties'], dict) and isinstance(s['required'], list)
    assert s['additionalProperties'] is False
    _check_schema(s, tool['name'])
    assert callable(getattr(M.Server, 'tool_' + tool['name'], None))
    if 'annotations' in tool:
        assert set(tool['annotations']) <= {'title', 'readOnlyHint', 'destructiveHint', 'idempotentHint',
                                            'openWorldHint'}
    # with the required arguments only, the arguments pass the server's own check
    base = {'output': {'on': False}, 'param_list': {'list': 0}}.get(tool['name'], {})
    assert set(base) == set(s['required'])
    assert M.check_args(tool, base) == base


def test_read_only_hints():
    ro = {t['name'] for t in M.TOOLS if (t.get('annotations') or {}).get('readOnlyHint')}
    assert ro == {'status', 'job_status', 'selftest', 'ports'}


@pytest.mark.parametrize('tool,prop,key', [
    ('set', 'voltage', 'voltage'), ('set', 'current', 'current'), ('set', 'ovp', 'ovp'), ('set', 'ocp', 'ocp'),
    ('set', 'delay', 'delay'), ('set', 'rev', 'rev'), ('set', 'opp', 'opp'),
    ('param_list', 'voltage', 'list0_voltage'), ('param_list', 'current', 'list0_current'),
    ('param_list', 'ovp', 'list0_ovp'), ('param_list', 'ocp', 'list0_ocp'),
    ('param_list', 'cut_current', 'list0_cut_current'), ('param_list', 'list', 'list'),
    ('sequence_start', 'steps.voltage', 'voltage'), ('sequence_start', 'steps.current', 'current')])
def test_schema_ranges_match_the_register_table(tool, prop, key):
    props = M.TOOL_BY_NAME[tool]['inputSchema']['properties']
    if '.' in prop:
        a, b = prop.split('.')
        s = props[a]['items']['properties'][b]
    else:
        s = props[prop]
    reg = P.BY_KEY[key]
    assert s['minimum'] == pytest.approx(reg.value_min) and s['maximum'] == pytest.approx(reg.value_max)


def test_schema_enums_match_the_protocol():
    props = M.TOOL_BY_NAME['set']['inputSchema']['properties']
    otp = P.BY_KEY['otp']
    assert props['otp']['enum'] == [P.raw_to_value(otp, r) for r in range(otp.raw_min, otp.raw_max + 1)]
    assert props['priority']['enum'] == list(P.BY_KEY['priority'].choices)
    assert M.COLOR_ELEMENTS == list(P.COLOR_DEFAULTS)


def test_unknown_method():
    _, srv = make_server()
    out = feed(srv, rpc(5, 'resources/list'), rpc(6, 7), note('no/such/notification'), rpc(8, 'ping'))
    assert out.one(5)['error']['code'] == -32601 and 'resources/list' in out.one(5)['error']['message']
    assert out.one(6)['error']['code'] == -32601
    assert out.one(8)['result'] == {}
    assert len(out.msgs) == 3                                # nothing for the notification


def test_parse_error_then_the_server_goes_on():
    _, srv = make_server()
    out = feed(srv, b'{not json', b'   ', b'', b'{"jsonrpc":"2.0","id":2,"method":"ping"}\r\n')
    assert out.msgs[0] == {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
    assert out.one(2)['result'] == {}
    assert len(out.msgs) == 2


def test_params_must_be_an_object():
    f, srv = make_server()
    out = feed(srv, rpc(3, 'ping', [1]), rpc(4, 'tools/call', 'status'), rpc(5, 'initialize', 5),
               note('notifications/initialized', [1]),
               rpc(6, 'tools/call', {'name': 'status', 'arguments': [1]}),
               rpc(7, 'tools/call', {'name': 'status', 'arguments': 'x'}))
    for mid in (3, 4, 5):
        assert out.one(mid)['error']['code'] == -32602 and 'params' in out.one(mid)['error']['message']
    for mid in (6, 7):
        assert out.one(mid)['error'] == {'code': -32602, 'message': 'arguments must be an object'}
    assert len(out.msgs) == 5 and f.runs == []


def test_invalid_requests_and_client_responses():
    _, srv = make_server()
    out = feed(srv, '5', '"x"', '[]', {'jsonrpc': '2.0', 'id': 9},
               {'jsonrpc': '2.0', 'id': 10, 'result': {}}, {'jsonrpc': '2.0', 'id': 11, 'error': {'code': 1}})
    assert [m['error']['code'] for m in out.msgs] == [-32600] * 4
    assert [m['id'] for m in out.msgs] == [None, None, None, 9]


def test_batch_is_dispatched_message_by_message():
    _, srv = make_server()
    out = feed(srv, [rpc(1, 'ping'), note('notifications/initialized'), rpc(2, 'tools/list'), tcall(3, 'status')])
    got = out.wait_ids([1, 2, 3])
    assert got[1]['result'] == {} and got[2]['result']['tools'] and got[3]['result']['isError'] is False
    assert len(out.msgs) == 3


def test_a_request_that_breaks_dispatch_gets_an_internal_error():
    f, srv = make_server()
    out = feed(srv, tcall([1], 'status'), rpc(2, 'ping'))
    assert out.one([1])['error']['code'] == -32603
    assert out.one(2)['result'] == {}
    wait_idle(srv)


def test_unknown_tool_is_a_protocol_error():
    f, srv = make_server()
    out = feed(srv, tcall(1, 'reboot'), rpc(2, 'tools/call', {'arguments': {}}), tcall(3, 'tool_status'))
    got = out.wait_ids([1, 2, 3])
    for mid in (1, 2, 3):
        assert got[mid]['error']['code'] == -32602 and 'unknown tool' in got[mid]['error']['message']
    assert f.runs == []


def test_tools_call_with_null_arguments():
    f, srv = make_server()
    out = feed(srv, rpc(1, 'tools/call', {'name': 'status', 'arguments': None}), rpc(2, 'tools/call',
                                                                                        {'name': 'status'}))
    got = out.wait_ids([1, 2])
    assert got[1]['result']['isError'] is False and got[2]['result']['isError'] is False
    assert [r[0] for r in f.runs] == [['status'], ['status']]


# ------------------------------------------------------------------------------------------------ cancellation

def test_cancelled_call_gets_no_answer_and_the_cli_sees_the_cancel():
    entered, seen = threading.Event(), {}

    def reply(args, wall, cancel):
        seen['cancel'] = cancel
        entered.set()
        if cancel is None or not cancel.wait(10):
            return [{'ok': True, 'cmd': args[0], 'late': True}], 0, ''
        raise M.ToolError('CANCELLED', 'the call was cancelled')

    f, srv = make_server(reply)
    srv.dispatch(tcall(7, 'watch', {'seconds': 60}))
    assert entered.wait(5)
    assert isinstance(seen['cancel'], threading.Event) and not seen['cancel'].is_set()
    srv.dispatch(note('notifications/cancelled', {'requestId': 99}))          # unknown: ignored
    assert not seen['cancel'].is_set()
    srv.dispatch(note('notifications/cancelled', {'requestId': 7, 'reason': 'user'}))
    wait_idle(srv)
    assert seen['cancel'].is_set()
    srv.dispatch(rpc(8, 'ping'))
    assert srv.out.one(8)['result'] == {}
    assert srv.out.by_id(7) == []
    assert not srv.cancelled
    # the id is free again and answered normally
    f.reply = None
    srv.dispatch(tcall(7, 'status'))
    assert srv.out.wait_ids([7])[7]['result']['isError'] is False


def test_cancel_after_the_answer_is_ignored():
    f, srv = make_server()
    srv.dispatch(tcall(1, 'status'))
    srv.out.wait_ids([1])
    wait_idle(srv)
    srv.dispatch(note('notifications/cancelled', {'requestId': 1}))
    assert not srv.cancelled and not srv.inflight
    srv.dispatch(rpc(2, 'ping'))
    assert len(srv.out.msgs) == 2


def test_request_id_in_use():
    release = threading.Event()

    def reply(args, wall, cancel):
        release.wait(10)
        return [{'ok': True, 'cmd': args[0]}], 0, ''

    f, srv = make_server(reply)
    srv.dispatch(tcall(9, 'status'))
    srv.dispatch(tcall(9, 'status'))
    assert srv.out.one(9)['error'] == {'code': -32600, 'message': 'request id in use'}
    release.set()
    t_end = time.monotonic() + 10
    while len(srv.out.by_id(9)) < 2:
        assert time.monotonic() < t_end, srv.out.msgs
        time.sleep(0.01)
    answers = srv.out.by_id(9)
    assert answers[1]['result']['isError'] is False
    assert len(f.runs) == 1
    wait_idle(srv)


def test_cli_run_cancel_and_timeout_kill_the_child(monkeypatch):
    procs = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(M.subprocess, 'Popen', popen)
    c = M.Cli([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        t0 = time.monotonic()
        with pytest.raises(M.ToolError) as ei:
            c.run(['status'], 0.5)
        assert ei.value.doc['error']['code'] == 'TIMEOUT' and 'max_wall' in ei.value.doc['error']['hint']
        assert time.monotonic() - t0 < 8
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        with pytest.raises(M.ToolError) as ei:
            c.run(['status'], 30, cancel)
        assert ei.value.doc['error']['code'] == 'CANCELLED'
        assert time.monotonic() - t0 < 8
        assert len(procs) == 2
        for p in procs:
            assert p.wait(5) is not None                    # killed, not left running
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(5)


def test_cli_run_without_kill_on_cancel_lets_the_child_finish(monkeypatch):
    """kill_on_cancel False (state-changing commands): the child gets wall + 10 s and is never killed on a cancel;
    a cancelled call still raises CANCELLED, but only after the child has run to its end."""
    procs = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(M.subprocess, 'Popen', popen)
    c = M.Cli([sys.executable, '-c', 'import time; time.sleep(0.6); print(\'{"ok": true, "done": true}\')'])
    try:
        docs, rc, err = c.run(['set'], 0.2, None, kill_on_cancel=False)      # past the wall, inside the grace
        assert docs == [{'ok': True, 'done': True}] and rc == 0
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(M.ToolError) as ei:
            c.run(['set'], 30, cancel, kill_on_cancel=False)
        assert ei.value.doc['error']['code'] == 'CANCELLED'
        assert 'already run to its end' in ei.value.doc['error']['message']
        assert len(procs) == 2 and procs[1].returncode == 0                  # finished by itself, not killed
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(5)


def test_cli_run_collects_json_lines_exit_code_and_stderr():
    code = ('import sys; print("noise"); print(\'{"ok": true, "n": 1}\'); print("{bad"); '
            'print(\'  {"ok": false, "n": 2}\'); sys.stderr.write("trouble"); sys.exit(3)')
    docs, rc, err = M.Cli([sys.executable, '-c', code]).run(['x'], 20)
    assert docs == [{'ok': True, 'n': 1}, {'ok': False, 'n': 2}] and rc == 3 and err == 'trouble'


def test_child_env_carries_the_home_and_the_simulator():
    env = M.child_env()
    assert env['OPENAXPP300S_HOME'] == home() == os.path.abspath(os.environ['OPENAXPP300S_HOME'])
    assert env['PYTHONIOENCODING'] == 'utf-8'
    assert env['OPENAXPP300S_SIM'] == '1' and env['OPENAXPP300S_NO_SERIAL'] == '1'
    assert repr(M.PKG_PARENT) in M.BOOT


# ================================================================================================ check_args

BAD_ARGS = [
    ('set', {'volts': 5}, 'unknown argument(s) for set: volts'),
    ('status', {'verbose': True}, 'unknown argument(s) for status: verbose'),
    ('log_start', {'max_wall': 5}, 'unknown argument(s) for log_start: max_wall'),
    ('set', {'voltage': '5'}, 'voltage must be of type number'),
    ('set', {'voltage': [5]}, 'voltage must be of type number'),
    ('set', {'voltage': True}, 'voltage must be of type number'),
    ('set', {'delay': False}, 'delay must be of type integer'),
    ('set', {'delay': 1.5}, 'delay must be of type integer'),
    ('set', {'delay': '2'}, 'delay must be of type integer'),
    ('set', {'voltage': float('nan')}, 'voltage must be of type number'),
    ('set', {'voltage': float('inf')}, 'voltage must be of type number'),
    ('set', {'ovp': float('-inf')}, 'ovp must be of type number'),
    ('set', {'delay': float('nan')}, 'delay must be of type integer'),
    ('set', {'otp': 65}, 'otp must be one of 60, 70, 80, 90, 100'),
    ('set', {'otp': 110}, 'otp must be one of'),
    ('set', {'priority': 'CV'}, 'priority must be one of cv, cc'),
    ('set', {'voltage': -0.01}, 'voltage must be >= 0'),
    ('set', {'voltage': 30.01}, 'voltage must be <= 30'),
    ('set', {'current': 10.11}, 'current must be <= 10.1'),
    ('set', {'current': 0}, 'current must be >= 0.01'),
    ('set', {'current': 0.009}, 'current must be >= 0.01'),
    ('set', {'ovp': 0.49}, 'ovp must be >= 0.5'),
    ('set', {'ocp': 0.09}, 'ocp must be >= 0.1'),
    ('set', {'ocp': 0}, 'ocp must be >= 0.1'),
    ('set', {'opp': 0}, 'opp must be >= 10'),
    ('set', {'opp': 320}, 'opp must be <= 310'),
    ('set', {'delay': 10000}, 'delay must be <= 9999'),
    ('set', {'wait': 1}, 'wait must be of type boolean'),
    ('set', {'wait': 'false'}, 'wait must be of type boolean'),
    ('set', {'max_wall': 0.5}, 'max_wall must be >= 1'),
    ('set', {'max_wall': 3601}, 'max_wall must be <= 3600'),
    ('output', {}, 'output needs on'),
    ('output', {'on': None}, 'output needs on'),
    ('output', {'on': 1}, 'on must be of type boolean'),
    ('output', {'on': True, 'wait_timeout': 0}, 'wait_timeout must be >= 0.1'),
    ('param_list', {'voltage': 5}, 'param_list needs list'),
    ('param_list', {'list': 5}, 'list must be <= 4'),
    ('param_list', {'list': -1}, 'list must be >= 0'),
    ('param_list', {'list': '1'}, 'list must be of type integer'),
    ('param_list', {'list': 0, 'cut_time': 90}, 'cut_time must be of type string'),
    ('param_list', {'list': 0, 'cut_current': 10.01}, 'cut_current must be <= 10'),
    ('param_list', {'list': 0, 'current': 0}, 'current must be >= 0.01'),
    ('param_list', {'list': 0, 'ovp': 0.4}, 'ovp must be >= 0.5'),
    ('param_list', {'list': 0, 'ocp': 0.05}, 'ocp must be >= 0.1'),
    ('watch', {'count': 0}, 'count must be >= 1'),
    ('watch', {'every': 0.01}, 'every must be >= 0.05'),
    ('watch', {'seconds': 601}, 'seconds must be <= 600'),
    ('log_start', {'format': 'xml'}, 'format must be one of csv, jsonl'),
    ('log_start', {'every': 0}, 'every must be >= 0.05'),
    ('log_start', {'duration': 30}, 'duration must be of type string'),
    ('sequence_start', {'steps': []}, 'steps takes 1 to 1000 items'),
    ('sequence_start', {'steps': [STEP] * 1001}, 'steps takes 1 to 1000 items'),
    ('sequence_start', {'steps': {'voltage': 5}}, 'steps must be of type array'),
    ('sequence_start', {'steps': [5]}, 'steps[0] must be of type object'),
    ('sequence_start', {'steps': [STEP, {'voltage': 5, 'current': 1}]}, 'steps[1] needs seconds'),
    ('sequence_start', {'steps': [dict(STEP, ramp=1)]}, 'unknown argument(s) for steps[0]: ramp'),
    ('sequence_start', {'steps': [dict(STEP, voltage=31)]}, 'voltage must be <= 30'),
    ('sequence_start', {'steps': [dict(STEP, seconds=-1)]}, 'seconds must be >= 0'),
    ('sequence_start', {'steps': [dict(STEP, current=0)]}, 'steps[0].current must be >= 0.01'),
    ('sequence_start', {'steps': [dict(STEP, current=True)]}, 'current must be of type number'),
    ('sequence_start', {'steps': [dict(STEP, seconds=float('nan'))]}, 'seconds must be of type number'),
    ('sequence_start', {'steps': [STEP], 'end': 'on'}, 'end must be one of off, keep'),
    ('sequence_start', {'steps': [STEP], 'repeat': 1.5}, 'repeat must be of type integer'),
    ('sequence_start', {'steps': [STEP], 'repeat': -1}, 'repeat must be >= 0'),
    ('display_colors', {'colors': 'red'}, 'colors must be of type object'),
    ('display_colors', {'colors': ['vout', 'red']}, 'colors must be of type object'),
    ('display_colors', {'colors': {'vout': 5}}, 'colors.vout must be of type string'),
    ('display_colors', {'colors': {'vout': '#FF0000', 'iout': None}}, 'colors.iout must be of type string'),
    ('display_colors', {'reset': 'yes'}, 'reset must be of type boolean'),
    ('job_status', {'job': 5}, 'job must be of type string'),
    ('job_stop', {'all': True}, 'unknown argument(s) for job_stop: all'),
]


@pytest.mark.parametrize('tool,args,message', BAD_ARGS)
def test_check_args_refuses(tool, args, message):
    with pytest.raises(M.ToolError) as ei:
        M.check_args(M.TOOL_BY_NAME[tool], args)
    err = ei.value.doc
    assert err['ok'] is False and err['error']['code'] == 'INVALID_ARG'
    assert message in err['error']['message'], err


def test_unknown_argument_hint_lists_the_arguments():
    with pytest.raises(M.ToolError) as ei:
        M.check_args(M.TOOL_BY_NAME['output'], {'on': True, 'state': 1, 'abc': 2})
    e = ei.value.doc['error']
    assert e['message'] == 'unknown argument(s) for output: abc, state'
    assert e['hint'] == 'arguments: on, wait, wait_timeout, max_wall'


GOOD_ARGS = [
    ('set', {'voltage': 30.0, 'current': 0.01, 'ovp': 30.5, 'ocp': 10.5, 'opp': 310, 'otp': 100, 'priority': 'cv',
             'delay': 9999, 'rev': 0, 'wait': True, 'max_wall': 3600}, None),
    ('set', {'voltage': 0, 'current': 10.1, 'ovp': 0.5, 'ocp': 0.1}, None),            # the front-panel minimums
    ('param_list', {'list': 0, 'current': 0.01, 'ovp': 0.5, 'ocp': 0.1, 'cut_current': 0}, None),
    ('set', {'delay': 2.0, 'otp': 60.0, 'opp': 100.0}, {'delay': 2, 'otp': 60, 'opp': 100}),
    ('set', {'voltage': None, 'current': 1}, {'current': 1}),
    ('output', {'on': False, 'wait': False, 'wait_timeout': 0.1}, None),
    ('param_list', {'list': 4.0, 'cut_time': '1h30m'}, {'list': 4, 'cut_time': '1h30m'}),
    ('sequence_start', {'steps': [{'voltage': 0, 'current': 0.01, 'seconds': 0}], 'repeat': 0}, None),
    ('sequence_start', {'steps': [STEP] * 1000}, None),
    ('display_colors', {'colors': {'vout': '#FFFF00', 'iout': 'green', 'unknown_el': '0xF800'}}, None),
    ('display_colors', {'colors': {}, 'reset': True}, None),
    ('watch', {}, None),
    ('job_stop', {'job': None}, {}),
]


@pytest.mark.parametrize('tool,args,expected', GOOD_ARGS)
def test_check_args_accepts(tool, args, expected):
    got = M.check_args(M.TOOL_BY_NAME[tool], args)
    assert got == (args if expected is None else expected)
    for k, v in got.items():
        if M.TOOL_BY_NAME[tool]['inputSchema']['properties'][k]['type'] == 'integer':
            assert type(v) is int, (k, v)


def _numeric_props():
    for t in M.TOOLS:
        props = t['inputSchema']['properties']
        for k, s in props.items():
            if s['type'] in ('number', 'integer') and 'enum' not in s:
                yield pytest.param(t['name'], k, None, id='%s.%s' % (t['name'], k))
        if 'steps' in props:
            for k in props['steps']['items']['properties']:
                yield pytest.param(t['name'], 'steps', k, id='%s.steps.%s' % (t['name'], k))


@pytest.mark.parametrize('tool,prop,sub', list(_numeric_props()))
def test_numeric_bounds_are_inclusive(tool, prop, sub):
    t = M.TOOL_BY_NAME[tool]
    base = {'output': {'on': True}, 'param_list': {'list': 0}}.get(tool, {})
    s = t['inputSchema']['properties'][prop]
    if sub:
        s = s['items']['properties'][sub]
    eps = 1 if s['type'] == 'integer' else 1e-6

    def args(v):
        if sub:
            return dict(base, **{prop: [dict(STEP, **{sub: v})]})
        return dict(base, **{prop: v})

    for v in (s['minimum'], s['maximum']):
        M.check_args(t, args(v))
    for v in (s['minimum'] - eps, s['maximum'] + eps):
        with pytest.raises(M.ToolError):
            M.check_args(t, args(v))


def test_nested_step_errors_name_the_step():
    """An error inside step 2 of a long list must say which step (as 'steps[1] needs seconds' already does)."""
    tool = M.TOOL_BY_NAME['sequence_start']
    for bad, message in ((dict(STEP, voltage=31), 'steps[1].voltage must be <= 30'),
                         (dict(STEP, seconds=-1), 'steps[1].seconds must be >= 0'),
                         (dict(STEP, current='1'), 'steps[1].current must be of type number'),
                         (dict(STEP, current=0.001), 'steps[1].current must be >= 0.01'),
                         (dict(STEP, ramp=1), 'unknown argument(s) for steps[1]: ramp')):
        with pytest.raises(M.ToolError) as ei:
            M.check_args(tool, {'steps': [STEP, bad]})
        assert ei.value.doc['error']['message'] == message, ei.value.doc


def test_schema_minimums_are_the_front_panel_limits():
    """ISET >= 0.01 A, OVP >= 0.5 V, OCP >= 0.1 A (the front panel's own limits), in set, param_list and steps."""
    for tool in ('set', 'param_list'):
        props = M.TOOL_BY_NAME[tool]['inputSchema']['properties']
        assert (props['current']['minimum'], props['ovp']['minimum'], props['ocp']['minimum']) == (0.01, 0.5, 0.1)
        assert props['voltage']['minimum'] == 0
    step = M.TOOL_BY_NAME['sequence_start']['inputSchema']['properties']['steps']['items']['properties']
    assert step['current']['minimum'] == 0.01 and step['voltage']['minimum'] == 0


@pytest.mark.parametrize('literal', ['NaN', 'Infinity', '-Infinity'])
def test_non_finite_json_numbers_are_refused_before_the_cli(literal):
    f, srv = make_server()
    line = '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"set","arguments":{"voltage":%s}}}' \
           % literal
    res = feed(srv, line).wait_ids([3])[3]['result']
    assert error_of(res)['code'] == 'INVALID_ARG'
    assert f.runs == []


def test_invalid_arguments_are_a_tool_error_not_a_protocol_error():
    f, srv = make_server()
    out = feed(srv, rpc(1, 'initialize', {'protocolVersion': '2025-06-18'}), tcall(2, 'set', {'voltage': 99}),
               tcall(3, 'output', {}))
    got = out.wait_ids([2, 3])
    for mid in (2, 3):
        res = got[mid]['result']
        assert 'error' not in got[mid] and res['isError'] is True
        assert res['structuredContent']['error']['code'] == 'INVALID_ARG'
        assert res['content'][0]['text'].startswith('INVALID_ARG: ')
    assert f.runs == []


# ================================================================================================ argv per tool

def test_status_selftest_ports_argv():
    assert argv_of('status', {}) == (['status'], M.DEFAULT_WALL_S)
    assert argv_of('status', {'max_wall': 5}) == (['status'], 5)
    assert argv_of('selftest', {}) == (['selftest'], M.DEFAULT_WALL_S)
    assert argv_of('ports', {'max_wall': 90}) == (['ports', '--probe'], 90)
    assert parse(['ports', '--probe']).probe is True


def test_set_argv():
    argv, wall = argv_of('set', {'priority': 'cc', 'otp': 80, 'opp': 100, 'rev': 0, 'delay': 2, 'ocp': 1.5,
                                 'ovp': 6, 'current': 1.25, 'voltage': 5})
    assert argv == ['set', '--voltage', '5', '--current', '1.25', '--ovp', '6', '--ocp', '1.5', '--delay', '2',
                    '--rev', '0', '--opp', '100', '--otp', '80', '--priority', 'cc']
    assert wall == M.DEFAULT_WALL_S
    ns = parse(argv)
    assert (ns.cmd, ns.voltage, ns.current, ns.ovp, ns.ocp, ns.delay, ns.rev, ns.opp, ns.otp, ns.priority,
            ns.no_wait) == ('set', 5.0, 1.25, 6.0, 1.5, 2, 0, 100, 80, 'cc', False)


def test_set_argv_numbers_parse_back_exactly():
    argv, _ = argv_of('set', {'voltage': 3.3, 'current': 0.1 + 0.2, 'ovp': 5.0, 'delay': 3.0, 'opp': 20.0})
    assert argv == ['set', '--voltage', '3.3', '--current', repr(0.1 + 0.2), '--ovp', '5.0', '--delay', '3',
                    '--opp', '20']
    ns = parse(argv)
    assert (ns.voltage, ns.current, ns.ovp, ns.delay, ns.opp) == (3.3, 0.1 + 0.2, 5.0, 3, 20)


def test_set_wait_and_max_wall():
    assert argv_of('set', {'voltage': 5, 'wait': False}) == (['set', '--voltage', '5', '--no-wait'],
                                                              M.DEFAULT_WALL_S)
    assert argv_of('set', {'voltage': 5, 'wait': True, 'max_wall': 12}) == (['set', '--voltage', '5'], 12)
    assert parse(['set', '--voltage', '5', '--no-wait']).no_wait is True


@pytest.mark.parametrize('tool,args,change', [
    ('status', {}, False), ('selftest', {}, False), ('ports', {}, False), ('watch', {'seconds': 1}, False),
    ('set', {'voltage': 5}, True), ('output', {'on': True}, True), ('output', {'on': False, 'wait': False}, True),
    ('param_list', {'list': 1, 'select': True}, True), ('param_list', {'list': 1, 'voltage': 5}, True),
    ('display_colors', {'reset': True}, True), ('display_colors', {'colors': {'vout': 'red'}}, True),
])
def test_state_changing_tools_are_not_killed_on_cancel(tool, args, change):
    """set/output/param_list/display_colors run with kill_on_cancel False (a child killed between a frame and its
    record would leave the record of the settings wrong); read-only tools are killed at once."""
    f, srv = make_server()
    assert call(srv, tool, args)['isError'] is False
    assert f.kill_on_cancel == [not change]


def test_sequence_dry_run_is_killed_on_cancel():
    f, srv = make_server()
    call(srv, 'sequence_start', {'steps': [STEP]})
    assert f.kill_on_cancel == [True] and len(f.starts) == 1


@pytest.mark.parametrize('args', [{}, {'wait': False}, {'max_wall': 5}, {'voltage': None}])
def test_set_nothing_is_refused_without_running_the_cli(args):
    f, srv = make_server()
    e = error_of(call(srv, 'set', args))
    assert e['code'] == 'INVALID_ARG' and e['message'] == 'nothing to set' and 'voltage' in e['hint']
    assert f.runs == []


def test_output_argv():
    # waiting (the default) without a wait_timeout: the CLI gives up 8 s before the wall (at least 1 s), so the
    # answer is 'settled false + note' rather than a TIMEOUT that hides what was sent
    assert argv_of('output', {'on': True}) == (['on', '--wait-timeout', '22.0'], M.DEFAULT_WALL_S)
    assert argv_of('output', {'on': False}) == (['off', '--wait-timeout', '22.0'], M.DEFAULT_WALL_S)
    assert argv_of('output', {'on': True, 'max_wall': 60}) == (['on', '--wait-timeout', '52.0'], 60)
    assert argv_of('output', {'on': True, 'max_wall': 5}) == (['on', '--wait-timeout', '1.0'], 5)
    assert parse(['on', '--wait-timeout', '22.0']).wait_timeout == 22.0
    # wait false: --no-wait only
    assert argv_of('output', {'on': False, 'wait': False}) == (['off', '--no-wait'], M.DEFAULT_WALL_S)
    assert argv_of('output', {'on': True, 'wait': False, 'max_wall': 5}) == (['on', '--no-wait'], 5)
    assert argv_of('output', {'on': True, 'wait': True, 'wait_timeout': 2.5}) == (['on', '--wait-timeout', '2.5'],
                                                                                 M.DEFAULT_WALL_S)
    assert argv_of('output', {'on': True, 'wait_timeout': 100}) == (['on', '--wait-timeout', '100'], 115)
    assert argv_of('output', {'on': True, 'wait_timeout': 100, 'max_wall': 20}) == (
        ['on', '--wait-timeout', '100'], 20)
    ns = parse(['off', '--no-wait'])
    assert ns.cmd == 'off' and ns.no_wait is True
    ns = parse(['on', '--wait-timeout', '2.5'])
    assert ns.cmd == 'on' and ns.wait_timeout == 2.5 and ns.no_wait is False


def test_param_list_select_only():
    argv, wall = argv_of('param_list', {'list': 2, 'select': True})
    assert (argv, wall) == (['list', 'select', '2'], M.DEFAULT_WALL_S)
    ns = parse(argv)
    assert (ns.cmd, ns.list_cmd, ns.n) == ('list', 'select', 2)


def test_param_list_set_values():
    argv, _ = argv_of('param_list', {'select': True, 'ovp': 6, 'list': 1, 'voltage': 5})
    assert argv == ['list', 'set', '1', '--voltage', '5', '--ovp', '6', '--select']
    ns = parse(argv)
    assert (ns.list_cmd, ns.n, ns.voltage, ns.ovp, ns.select) == ('set', 1, 5.0, 6.0, True)
    argv, wall = argv_of('param_list', {'list': 4, 'current': 0.5, 'ocp': 0.6, 'cut_current': 0.05,
                                        'cut_time': '1h30m', 'select': False, 'max_wall': 9})
    assert argv == ['list', 'set', '4', '--current', '0.5', '--ocp', '0.6', '--cut-current', '0.05', '--cut-time',
                    '1h30m']
    assert wall == 9
    ns = parse(argv)
    assert (ns.n, ns.current, ns.ocp, ns.cut_current, ns.cut_time, ns.select) == (4, 0.5, 0.6, 0.05, '1h30m',
                                                                                  False)


@pytest.mark.parametrize('args', [{'list': 0}, {'list': 3, 'select': False}])
def test_param_list_nothing_to_do(args):
    f, srv = make_server()
    e = error_of(call(srv, 'param_list', args))
    assert e['code'] == 'INVALID_ARG' and e['message'] == 'nothing to do'
    assert f.runs == []


def test_display_colors_set_and_reset():
    argv, _ = argv_of('display_colors', {'colors': {'vout': '#FFFF00', 'iout': 'green', 'pout': '0xF800'}})
    assert argv == ['color', 'set', 'vout=#FFFF00', 'iout=green', 'pout=0xF800']
    ns = parse(argv)
    assert (ns.color_cmd, ns.assign) == ('set', ['vout=#FFFF00', 'iout=green', 'pout=0xF800'])
    assert argv_of('display_colors', {'reset': True}) == (['color', 'reset'], M.DEFAULT_WALL_S)
    assert argv_of('display_colors', {'reset': True, 'colors': {}})[0] == ['color', 'reset']
    assert argv_of('display_colors', {'reset': False, 'colors': {'bar_back': 'black'}, 'max_wall': 4}) == (
        ['color', 'set', 'bar_back=black'], 4)
    assert parse(['color', 'reset']).color_cmd == 'reset'


@pytest.mark.parametrize('args,message', [({'reset': True, 'colors': {'vout': 'red'}}, 'not both'),
                                          ({}, 'give colors'), ({'colors': {}}, 'give colors'),
                                          ({'reset': False}, 'give colors')])
def test_display_colors_conflict_or_nothing(args, message):
    f, srv = make_server()
    e = error_of(call(srv, 'display_colors', args))
    assert e['code'] == 'INVALID_ARG' and message in e['message']
    if message == 'give colors':
        assert all(el in e['hint'] for el in M.COLOR_ELEMENTS)
    assert f.runs == []


@pytest.mark.parametrize('args,argv,wall', [
    ({}, ['watch', '--quiet', '--every', '1.0', '--for', '5.0'], 35),
    ({'count': 3}, ['watch', '--quiet', '--every', '1.0', '--count', '3'], 38),
    ({'seconds': 2, 'every': 0.5}, ['watch', '--quiet', '--every', '0.5', '--for', '2'], 32),
    ({'count': 4, 'every': 0.25}, ['watch', '--quiet', '--every', '0.25', '--count', '4'], 36),
    ({'count': 2, 'seconds': 10}, ['watch', '--quiet', '--every', '1.0', '--count', '2', '--for', '10'], 37),
    ({'seconds': 600, 'max_wall': 7}, ['watch', '--quiet', '--every', '1.0', '--for', '600'], 7),
])
def test_watch_argv(args, argv, wall):
    got, w = argv_of('watch', args)
    assert got == argv and w == pytest.approx(wall)
    ns = parse(got)
    assert ns.quiet is True and ns.every == args.get('every', 1.0) and ns.count == args.get('count')
    if 'seconds' in args or 'count' not in args:
        assert cli_mod.parse_duration(ns.for_, '--for') == args.get('seconds', 5.0)
    else:
        assert ns.for_ is None


def test_log_start_defaults():
    f, srv = make_server()
    res = call(srv, 'log_start', {})
    d = doc(res)
    assert res['isError'] is False and d['ok'] is True
    assert f.runs == [] and len(f.starts) == 1
    argv = f.starts[0]
    assert argv[:2] == ['log', '--out']
    out = argv[2]
    assert os.path.dirname(out) == data_dir() and out.startswith(home())
    assert re.fullmatch(r'log_\d{8}_\d{6}\.csv', os.path.basename(out)), out
    assert argv[3:] == ['--every', '1.0', '--duration', 'forever', '--status-every', '10s']
    ns = parse(argv)
    assert ns.cmd == 'log' and ns.out == out and ns.every == 1.0 and ns.format is None
    assert cli_mod.parse_duration(ns.duration) is None and cli_mod.parse_duration(ns.status_every) == 10
    # the job is registered under state/mcp_jobs/<job>/ with its command line
    job = d['job']
    assert re.fullmatch(r'log-\d{8}-\d{6}-\d+', job), job
    meta = state.read_json(os.path.join(M.jobs_dir(), job, 'meta.json'))
    assert meta['kind'] == 'log' and meta['args'] == argv and meta['pid'] == 0
    assert d['kind'] == 'log' and d['started']['event'] == 'started' and d['running'] is False


def test_log_start_options():
    f, srv = make_server()
    call(srv, 'log_start', {'format': 'jsonl'})
    argv = f.starts[-1]
    assert argv[2].endswith('.jsonl') and argv[-2:] == ['--format', 'jsonl']
    assert parse(argv).format == 'jsonl'
    call(srv, 'log_start', {'out': 'sub/x.csv', 'every': 0.5, 'duration': '30s'})
    assert f.starts[-1] == ['log', '--out', 'sub/x.csv', '--every', '0.5', '--duration', '30s', '--status-every',
                            '10s']
    ns = parse(f.starts[-1])
    assert (ns.out, ns.every, cli_mod.parse_duration(ns.duration)) == ('sub/x.csv', 0.5, 30)
    assert f.runs == []


def test_sequence_start_steps_and_dry_run_first():
    f, srv = make_server(lambda args, wall, cancel: ([{'ok': True, 'cmd': 'sequence', 'dry_run': True}], 0, ''))
    steps = [{'voltage': 5, 'current': 1, 'seconds': 2}, {'voltage': 3.3, 'current': 0.25, 'seconds': 1.5}]
    res = call(srv, 'sequence_start', {'steps': steps, 'repeat': 3, 'end': 'keep', 'every': 0.5, 'log': 'seq.csv'})
    assert res['isError'] is False and doc(res)['ok'] is True and doc(res)['kind'] == 'sequence'
    expected = ['sequence', '--steps', '5,1,2;3.3,0.25,1.5', '--repeat', '3', '--end', 'keep', '--every', '0.5',
                '--log', 'seq.csv']
    assert f.events == [('run', expected + ['--dry-run']), ('start', expected)]
    assert f.runs[0][1] == M.DEFAULT_WALL_S
    ns = parse(expected)
    assert [{k: s[k] for k in ('voltage', 'current', 'seconds')} for s in runner.parse_steps(ns.steps)] == steps
    assert (ns.file, ns.repeat, ns.end, ns.every, ns.log, ns.dry_run) == (None, 3, 'keep', 0.5, 'seq.csv', False)
    assert parse(expected + ['--dry-run']).dry_run is True


def test_sequence_start_defaults_and_file():
    f, srv = make_server()
    call(srv, 'sequence_start', {'steps': [STEP]})
    assert f.starts[-1] == ['sequence', '--steps', '5,1,2']
    ns = parse(f.starts[-1])
    assert (ns.repeat, ns.end, ns.every) == (1, 'off', 1.0)          # the CLI defaults: one cycle, output off
    call(srv, 'sequence_start', {'file': 'x/seq.csv', 'repeat': 0})
    # the file goes last, after '--' (options first; a path that starts with '-' stays a path)
    assert f.events[-2:] == [('run', ['sequence', '--repeat', '0', '--dry-run', '--', 'x/seq.csv']),
                             ('start', ['sequence', '--repeat', '0', '--', 'x/seq.csv'])]
    ns = parse(f.starts[-1])
    assert (ns.file, ns.steps, ns.repeat, ns.dry_run) == ('x/seq.csv', None, 0, False)
    ns = parse(f.runs[-1][0])
    assert (ns.file, ns.steps, ns.repeat, ns.dry_run) == ('x/seq.csv', None, 0, True)


def test_sequence_start_dry_run_gets_the_cancel_event():
    """The dry run sees the call's cancel event; a call cancelled during it starts no job and gets no answer."""
    seen = {}

    def reply(args, wall, cancel):
        seen['cancel'] = cancel
        srv.dispatch(note('notifications/cancelled', {'requestId': 5}))
        return [{'ok': True, 'cmd': 'sequence', 'dry_run': True}], 0, ''

    f, srv = make_server(reply)
    srv.dispatch(tcall(5, 'sequence_start', {'steps': [STEP]}))
    wait_idle(srv)
    assert isinstance(seen['cancel'], threading.Event) and seen['cancel'].is_set()
    assert [a for a, _, _ in f.runs] == [['sequence', '--steps', '5,1,2', '--dry-run']]
    assert f.starts == [] and os.listdir(M.jobs_dir()) == []
    assert srv.out.by_id(5) == [] and not srv.cancelled


@pytest.mark.parametrize('args', [{}, {'steps': [STEP], 'file': 'a.csv'}, {'repeat': 2}, {'file': ''}])
def test_sequence_start_needs_steps_or_file(args):
    f, srv = make_server()
    e = error_of(call(srv, 'sequence_start', args))
    assert e['code'] == 'INVALID_ARG' and 'steps or file' in e['message']
    assert f.events == []


def test_sequence_start_refused_by_the_dry_run_starts_nothing():
    refusal = {'ok': False, 'cmd': 'sequence', 'error': {'code': 'UNSAFE_OPERATION', 'message': 'step 2: VSET 9 V '
               'is above the bench voltage limit 5 V', 'hint': 'ask the user'}}
    f, srv = make_server(lambda args, wall, cancel: ([refusal], 7, ''))
    res = call(srv, 'sequence_start', {'steps': [STEP, dict(STEP, voltage=9)]})
    assert res['isError'] is True and doc(res) == refusal
    assert f.starts == [] and os.listdir(M.jobs_dir()) == []


def test_sequence_start_dry_run_crash_without_output():
    f, srv = make_server(lambda args, wall, cancel: ([], 1, 'Traceback (most recent call last):\n  boom'))
    e = error_of(call(srv, 'sequence_start', {'steps': [STEP]}))
    assert e['code'] == 'INTERNAL' and 'boom' in e['message']
    assert f.starts == []


@pytest.mark.parametrize('tool,args,attr,value', [
    ('param_list', {'list': 0, 'cut_time': '-5'}, 'cut_time', '-5'),
    ('sequence_start', {'steps': [STEP], 'log': '-dash.csv'}, 'log', '-dash.csv'),
    ('log_start', {'out': '-dash.csv'}, 'out', '-dash.csv'),
    ('sequence_start', {'file': '-steps.csv'}, 'file', '-steps.csv'),
])
def test_values_starting_with_a_dash_reach_the_cli_intact(tool, args, attr, value):
    """A path or value that starts with '-' must not be read as an option by the CLI (the server has _flag for it)."""
    f, srv = make_server()
    call(srv, tool, args)
    argv = (f.starts or [f.runs[-1][0]])[-1]
    try:
        ns = parse(argv)
    except PP300SError as e:
        pytest.fail('the CLI cannot parse %r: %s' % (argv, e.message))
    assert getattr(ns, attr) == value


def test_flag_helper():
    assert M._flag('--x', 5) == ['--x', '5']
    assert M._flag('--x', '-5') == ['--x=-5']
    assert M._g(True) == '1' and M._g(7) == '7' and M._g(2.5) == '2.5' and M._g(5.0) == '5.0'


# ================================================================================================ results

@pytest.mark.parametrize('version,structured', [('2025-11-25', True), ('2025-06-18', True), ('2025-03-26', False),
                                                ('2024-11-05', False)])
def test_structured_content_only_since_2025_06_18(version, structured):
    f, srv = make_server()
    out = feed(srv, rpc(1, 'initialize', {'protocolVersion': version}), note('notifications/initialized'),
               tcall(2, 'status'), tcall(3, 'set', {}))
    got = out.wait_ids([1, 2, 3])
    assert got[1]['result']['protocolVersion'] == version
    for mid, is_error in ((2, False), (3, True)):
        res = got[mid]['result']
        assert set(res) == {'content', 'isError'} | ({'structuredContent'} if structured else set())
        assert res['isError'] is is_error
        assert res['content'][0]['type'] == 'text' and len(res['content']) == 1
    ok_doc = {'ok': True, 'cmd': 'status', 'argv': ['status']}
    assert json.loads(got[2]['result']['content'][0]['text']) == ok_doc
    if structured:
        assert got[2]['result']['structuredContent'] == ok_doc
        assert got[3]['result']['structuredContent']['error']['code'] == 'INVALID_ARG'


def test_ok_false_is_an_error_result_with_a_readable_head():
    err_doc = {'ok': False, 'cmd': 'set', 'error': {'code': 'UNSAFE_OPERATION', 'message': 'OVP 5 V is too close to '
               'VSET 5 V: the output would trip', 'hint': 'give OVP >= 5.2 V'}}
    f, srv = make_server(lambda args, wall, cancel: ([{'event': 'progress'}, err_doc], 7, ''))
    res = call(srv, 'set', {'voltage': 5, 'ovp': 5})
    assert res['isError'] is True and doc(res) == err_doc               # the last JSON line is the answer
    head, hint, body = res['content'][0]['text'].split('\n', 2)
    assert head == 'UNSAFE_OPERATION: OVP 5 V is too close to VSET 5 V: the output would trip'
    assert hint == 'hint: give OVP >= 5.2 V'
    assert json.loads(body) == err_doc


def test_a_doc_without_ok_true_is_an_error():
    f, srv = make_server(lambda args, wall, cancel: ([{'cmd': 'status'}], 0, ''))
    assert call(srv, 'status')['isError'] is True


def test_cli_printing_nothing_is_internal_with_stderr_hint():
    f, srv = make_server(lambda args, wall, cancel: ([], 1, 'Traceback\nImportError: no serial\n'))
    e = error_of(call(srv, 'status'))
    assert e['code'] == 'INTERNAL' and 'exit 1' in e['message'] and 'ImportError: no serial' in e['hint']


def test_exception_in_a_tool_is_internal():
    def reply(args, wall, cancel):
        raise RuntimeError('boom')

    f, srv = make_server(reply)
    e = error_of(call(srv, 'status'))
    assert e['code'] == 'INTERNAL' and 'boom' in e['message']


# ------------------------------------------------------------------------------------------------ jobs (fake)

def test_job_tools_without_jobs():
    f, srv = make_server()
    e = error_of(call(srv, 'job_status'))
    assert e['code'] == 'INVALID_ARG' and e['message'] == 'no jobs yet'
    d = doc(call(srv, 'job_stop'))
    assert d == {'ok': True, 'stopped': [], 'note': 'no job running'}
    for tool in ('job_status', 'job_stop'):
        e = error_of(call(srv, tool, {'job': 'log-x'}))
        assert e['code'] == 'INVALID_ARG' and e['message'] == 'unknown job log-x'
    assert f.events == []


def test_fake_job_lifecycle():
    f, srv = make_server()
    first = doc(call(srv, 'log_start', {}))['job']
    second = doc(call(srv, 'sequence_start', {'steps': [STEP]}))['job']
    assert first != second
    st = doc(call(srv, 'job_status'))                                   # default: the latest job
    assert st['ok'] and st['job'] == second and st['kind'] == 'sequence' and st['running'] is False
    assert st['result'] == {'ok': True, 'cmd': 'sequence', 'event': 'done', 'stopped_by': 'end'}
    st = doc(call(srv, 'job_status', {'job': first}))
    assert st['job'] == first and st['kind'] == 'log'
    d = doc(call(srv, 'job_stop', {'job': first}))
    assert d['ok'] is True and len(d['stopped']) == 1
    s = d['stopped'][0]
    assert s['job'] == first and s['already_ended'] is True and s['running'] is False
    assert doc(call(srv, 'job_stop'))['stopped'] == []                  # nothing running


def test_job_status_pid_is_the_clis_own_pid():
    """Under a Windows venv Popen.pid is the launcher's: the job's pid is the one the CLI reports when it starts."""
    f, srv = make_server()
    f.start_lines = [{'event': 'started', 'pid': 424242, 't': round(time.time(), 3)},
                     {'ok': True, 'cmd': 'log', 'event': 'done', 'stopped_by': 'end'}]
    d = doc(call(srv, 'log_start', {}))
    assert d['ok'] is True and d['pid'] == 424242 and d['started']['pid'] == 424242
    meta = state.read_json(os.path.join(M.jobs_dir(), d['job'], 'meta.json'))
    assert meta['pid'] == 0                                               # what Popen gave
    j = M.Job.load(d['job'])
    assert j.cli_pid() == 424242 and doc(call(srv, 'job_status'))['pid'] == 424242


def test_job_alive_needs_the_registry_entry_once_started():
    """A live pid without the CLI's registry entry (after its 'started' line) is a reused pid, not the job."""
    me = os.getpid()
    j = M.Job('log-test-1', 'log', me, ['log'], time.time())
    j.save()
    out = os.path.join(j.dir, 'stdout.jsonl')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write('')
    assert j.alive() is True                                     # not started yet: the pid alone counts
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps({'event': 'started', 'pid': me}) + '\n')
    assert state.list_running() == [] and j.alive() is False and j.running() is False
    state.register_running('log', {})
    try:
        assert j.alive() is True and j.running() is True
    finally:
        state.unregister_running(me)
    assert j.alive() is False


def test_job_folder_is_claimed_uniquely(monkeypatch):
    """Job folders are claimed with os.mkdir: a name already taken (another server) moves on to the next number."""
    f, srv = make_server()
    monkeypatch.setattr(M.time, 'strftime', lambda fmt, *a: '20260101-000000')
    os.mkdir(os.path.join(M.jobs_dir(), 'log-20260101-000000-2'))           # n would be 2: taken
    d = doc(call(srv, 'log_start', {'out': 'x.csv'}))
    assert d['ok'] is True and d['job'] == 'log-20260101-000000-3'
    assert os.listdir(os.path.join(M.jobs_dir(), 'log-20260101-000000-2')) == []     # left alone
    d2 = doc(call(srv, 'log_start', {'out': 'y.csv'}))
    assert d2['job'] not in ('log-20260101-000000-2', 'log-20260101-000000-3')


def test_job_failing_at_once_is_an_error():
    f, srv = make_server()
    f.start_lines = [{'ok': False, 'cmd': 'log', 'error': {'code': 'INVALID_ARG',
                                                           'message': "--duration: cannot parse 'soon'"}}]
    res = call(srv, 'log_start', {'duration': 'soon'})
    d = doc(res)
    assert res['isError'] is True and d['ok'] is False and d['job'].startswith('log-')
    assert d['error']['code'] == 'INVALID_ARG'


def test_job_dying_without_summary_is_job_died():
    f, srv = make_server()
    f.start_lines = []
    f.start_stderr = 'Traceback (most recent call last):\nRuntimeError: boom\n'
    res = call(srv, 'log_start', {})
    e = error_of(res)
    assert e['code'] == 'JOB_DIED' and 'boom' in e['stderr']


# ================================================================================================ real children

class TrackingCli(M.Cli):
    """The default Cli, remembering every background child so the test can make sure it has ended."""

    def __init__(self):
        super().__init__()
        self.procs = []

    def start(self, args, out_path, err_path):
        p = super().start(args, out_path, err_path)
        self.procs.append(p)
        return p


@pytest.fixture
def real(isolated):
    # the children inherit this environment: the simulator, and real serial ports refused
    env = M.child_env()
    assert env['OPENAXPP300S_SIM'] == '1' and env['OPENAXPP300S_NO_SERIAL'] == '1'
    assert env['OPENAXPP300S_SIM_DELAY'] == '0' and env['OPENAXPP300S_HOME'] == str(isolated / 'home')
    c = TrackingCli()
    srv = M.Server(cli=c, out=Out())
    yield srv
    left = [p for p in c.procs if p.poll() is None]
    if left:
        state.request_stop()
    for p in left:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
    for p in c.procs:
        p.wait(5)
    assert not left, 'background children still running at the end of the test: %r' % [p.pid for p in left]


def wait_job(srv, job, pred, timeout=20.0):
    t_end = time.monotonic() + timeout
    while True:
        st = doc(call(srv, 'job_status', {'job': job}))
        if pred(st):
            return st
        assert time.monotonic() < t_end, st
        time.sleep(0.1)


def test_real_status(real):
    res = call(real, 'status')
    d = doc(res)
    assert res['isError'] is False and d['ok'] is True and d['cmd'] == 'status' and d['port'] == 'SIM'
    assert d['output_on'] is False and d['mode'] in ('CV', 'CC', 'OFF')
    assert 18 < d['vin_v'] < 22 and d['last_sent'] == {}
    assert json.loads(res['content'][0]['text']) == d


def test_real_set_and_output(real):
    d = doc(call(real, 'set', {'voltage': 5, 'current': 1, 'ovp': 6}))
    assert d['ok'] is True and d['port'] == 'SIM'
    assert {s['key']: s['value'] for s in d['sent']} == {'voltage': 5.0, 'current': 1.0, 'ovp': 6.0}
    assert os.path.exists(state.sent_path())                     # written into the test's home by the child
    d = doc(call(real, 'output', {'on': True}))
    assert d['ok'] is True and d['settled'] is True
    assert d['status']['output_on'] is True and abs(d['status']['vout_v'] - 5.0) < 0.01
    d = doc(call(real, 'status'))
    assert d['output_on'] is True and d['last_sent']['voltage']['value'] == 5.0
    assert d['last_sent']['ovp']['value'] == 6.0 and d['last_sent']['output']['value'] is True
    # refused by the CLI before anything is sent: OVP too close to VSET, and an OPP off the 10 W grid
    frames = sim.load()['frames']
    e = error_of(call(real, 'set', {'voltage': 5.5, 'ovp': 5.6}))
    assert e['code'] == 'UNSAFE_OPERATION'
    e = error_of(call(real, 'set', {'opp': 15}))
    assert e['code'] == 'INVALID_ARG'
    st = sim.load()
    assert st['frames'] == frames and st['alarm'] is None and st['on_cmd'] is True
    d = doc(call(real, 'output', {'on': False}))
    assert d['ok'] is True and d['status']['output_on'] is False
    st = sim.load()
    assert st['on_cmd'] is False and st['alarm'] is None and st['vset'] == 500 and st['ovp'] == 600


def test_real_serve_round_trip(real):
    out = feed(real, rpc(1, 'initialize', {'protocolVersion': '2025-03-26'}), note('notifications/initialized'),
               tcall(2, 'status'), tcall(3, 'output', {'on': 'yes'}))
    got = out.wait_ids([1, 2, 3], timeout=60)
    res = got[2]['result']
    assert 'structuredContent' not in res and res['isError'] is False
    d = json.loads(res['content'][0]['text'])
    assert d['ok'] is True and d['port'] == 'SIM'
    assert got[3]['result']['isError'] is True and got[3]['result']['content'][0]['text'].startswith('INVALID_ARG')
    wait_idle(real)


def test_real_log_lifecycle(real):
    res = call(real, 'log_start', {'every': 0.1})
    d = doc(res)
    assert res['isError'] is False and d['ok'] is True and d['running'] is True and d['kind'] == 'log'
    job, pid = d['job'], d['pid']
    assert [p.pid for p in real.cli.procs] == [pid]
    started = d['started']
    assert started['event'] == 'started' and started['format'] == 'csv' and started['every_s'] == 0.1
    assert started['port'] == 'SIM' and started['duration_s'] is None and started['pid'] == pid
    path = started['file']
    assert os.path.dirname(path) == data_dir()
    st = wait_job(real, job, lambda s: s.get('last_row'))
    assert st['running'] is True and 'result' not in st
    assert st['last_row']['output_on'] == '0' and set(runner.CSV_COLUMNS) == set(st['last_row'])
    assert [r['pid'] for r in state.list_running()] == [pid]
    d = doc(call(real, 'job_stop', {'job': job}))
    assert d['ok'] is True and len(d['stopped']) == 1
    s = d['stopped'][0]
    assert s['job'] == job and s['running'] is False and s['already_ended'] is False
    r = s['result']
    assert r['ok'] is True and r['event'] == 'done' and r['stopped_by'] == 'stop' and r['rows'] >= 1
    assert real.cli.procs[0].wait(10) == 0
    assert not state.pid_alive(pid) and state.list_running() == []
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().splitlines()
    assert lines[0] == ','.join(runner.CSV_COLUMNS) and len(lines) - 1 == r['rows']
    st = doc(call(real, 'job_status'))
    assert st['job'] == job and st['running'] is False and st['result'] == r


def test_real_sequence_ends_by_itself_with_the_output_off(real):
    d = doc(call(real, 'set', {'ovp': 6, 'wait': False}))
    assert d['ok'] is True
    steps = [{'voltage': 3, 'current': 0.5, 'seconds': 0.3}, {'voltage': 4, 'current': 0.5, 'seconds': 0.3}]
    res = call(real, 'sequence_start', {'steps': steps, 'every': 0.1})
    d = doc(res)
    assert res['isError'] is False and d['ok'] is True and d['kind'] == 'sequence'
    started = d['started']
    assert started['steps'] == 2 and started['repeat'] == 1 and started['end'] == 'off'
    assert not started.get('warnings')                     # OVP known (6 V) and above both steps
    st = wait_job(real, d['job'], lambda s: not s['running'])
    r = st['result']
    assert r['ok'] is True and r['event'] == 'done' and r['stopped_by'] == 'end'
    assert r['steps_done'] == 2 and r['cycles'] == 1 and r['end'] == 'output switched off'
    assert st['last_step']['step'] == 2 and st['last_step']['voltage'] == 4
    assert real.cli.procs[0].wait(10) == 0
    s = doc(call(real, 'status'))
    assert s['output_on'] is False and s['last_sent']['output']['value'] is False
    assert s['last_sent']['voltage']['value'] == 4.0
    st = sim.load()
    assert st['on_cmd'] is False and st['alarm'] is None


def test_real_sequence_until_stopped_switches_off_on_job_stop(real):
    assert doc(call(real, 'set', {'ovp': 6, 'wait': False}))['ok'] is True
    d = doc(call(real, 'sequence_start', {'steps': [{'voltage': 2, 'current': 0.2, 'seconds': 0.2}], 'repeat': 0,
                                          'every': 0.1}))
    assert d['ok'] is True and d['started']['repeat'] == 0
    job = d['job']
    wait_job(real, job, lambda s: s.get('last_step') and sim.load()['on_cmd'])
    d = doc(call(real, 'job_stop', {}))                       # every running job
    assert d['ok'] is True and [s['job'] for s in d['stopped']] == [job]
    s = d['stopped'][0]
    assert s['running'] is False and s['already_ended'] is False
    assert s['result']['stopped_by'] == 'stop' and s['result']['end'] == 'output switched off'
    assert real.cli.procs[0].wait(10) == 0
    st = sim.load()
    assert st['on_cmd'] is False and st['alarm'] is None


def test_real_sequence_over_the_bench_limit_is_refused_before_it_starts(real):
    state.set_limits(max_voltage=5, note='5 V device')
    res = call(real, 'sequence_start', {'steps': [{'voltage': 4, 'current': 0.5, 'seconds': 1},
                                                  {'voltage': 6, 'current': 0.5, 'seconds': 1}]})
    e = error_of(res)
    assert e['code'] == 'UNSAFE_OPERATION' and '5' in e['message']
    assert real.cli.procs == [] and os.listdir(M.jobs_dir()) == []
    assert sim.load()['frames'] == 0                          # the dry run sent nothing


def test_stdio_server_process(isolated):
    """The server as a real process: stdout carries only JSON-RPC lines; it exits at the end of its input."""
    p = subprocess.Popen([sys.executable, '-m', 'openaxpp300s.mcp_server'], cwd=ROOT, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
    lines, errs = queue.Queue(), []
    threading.Thread(target=lambda: [lines.put(x) for x in p.stdout], daemon=True).start()
    threading.Thread(target=lambda: errs.extend(p.stderr), daemon=True).start()
    try:
        for m in (rpc(1, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 'pytest', 'version': '1'}}),
                  note('notifications/initialized'), rpc(2, 'tools/list'), tcall(3, 'status')):
            p.stdin.write((json.dumps(m) + '\n').encode('utf-8'))
        p.stdin.flush()
        got = {}
        t_end = time.monotonic() + 60
        while 3 not in got:
            line = lines.get(timeout=max(0.1, t_end - time.monotonic()))
            m = json.loads(line.decode('utf-8'))
            assert m['jsonrpc'] == '2.0' and 'id' in m, m
            got[m['id']] = m
        assert got[1]['result']['protocolVersion'] == '2025-06-18'
        assert [t['name'] for t in got[2]['result']['tools']] == TOOL_NAMES
        d = got[3]['result']['structuredContent']
        assert d['ok'] is True and d['port'] == 'SIM'
        p.stdin.close()
        assert p.wait(15) == 0
        time.sleep(0.1)
        assert lines.empty() and set(got) == {1, 2, 3}
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(5)
