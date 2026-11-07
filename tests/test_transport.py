"""transport.py against the virtual supply and scripted fake ports: sessions, retries, error codes, the reply search,
the cross-process port lock, port resolution, re-discovery in Supply._do and frame traces.

No real serial port is ever opened: on top of OPENAXPP300S_NO_SERIAL (conftest), every test here replaces
serial.Serial and the PC's port list with stand-ins that fail the test if they are reached."""
import os
import threading
import time

import pytest
import serial
import serial.tools.list_ports

from openaxpp300s import device, sim, state
from openaxpp300s import protocol as P
from openaxpp300s import transport as T
from openaxpp300s.errors import PP300SError
from openaxpp300s.lock import PortLock, lock_path
from openaxpp300s.paths import state_dir

VENDOR_REPLY = bytes.fromhex('A5 88 13 00 00 00 00 B4 07 25 01 00 01 7D 5A')   # 5 V, 0 A, CV, on
QUERY_HEX = 'a5 ff 00 00 ff 5a'


@pytest.fixture(autouse=True)
def no_real_serial(monkeypatch):
    class Forbidden:
        def __init__(self, *a, **k):
            raise AssertionError('a test tried to open a real serial port')
    monkeypatch.setattr(serial, 'Serial', Forbidden)
    monkeypatch.setattr(serial.tools.list_ports, 'comports', lambda *a, **k: [])
    monkeypatch.setattr(T, 'SETTLE_AFTER_TIMEOUT', 0.005)      # shorter pause after a timeout: faster tests


# ------------------------------------------------------------------------------------------------ fakes

class FakePort:
    """Scripted stand-in for serial.Serial. respond(frame, n) -> bytes, or a list of chunks that in_waiting hands
    out one at a time (a reply split over USB packets); n counts the frames written to this port so far.
    fail_writes: that many writes raise SerialException (the 'WriteFile access denied' seen on hardware)."""

    def __init__(self, respond, fail_writes=0):
        self.respond = respond
        self.fail_writes = fail_writes
        self.written = []
        self.chunks = []
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n=1):
        if not self.chunks:
            return b''
        c = self.chunks.pop(0)
        if len(c) > n:
            self.chunks.insert(0, c[n:])
            c = c[:n]
        return c

    def reset_input_buffer(self):
        self.chunks = []

    def write(self, data):
        if self.fail_writes:
            self.fail_writes -= 1
            raise serial.SerialException("WriteFile failed (PermissionError(13, 'Access is denied.', None, 5))")
        data = bytes(data)
        self.written.append(data)
        ans = self.respond(data, len(self.written))
        if isinstance(ans, (bytes, bytearray)):
            ans = [ans]
        self.chunks = [bytes(c) for c in (ans or []) if c]
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def silent(frame, n):
    return b''


def supply_like(frame, n):
    """ACK every setting frame, answer a query with the vendor example reply."""
    return VENDOR_REPLY if frame[1] == P.QUERY_ID else b'\x01'


class Ports(dict):
    """name -> factory returning a port object. Installed as transport._open_serial; a missing name is NOT_FOUND."""

    def __init__(self):
        super().__init__()
        self.opened = []

    def open(self, name):
        self.opened.append(name)
        if name not in self:
            raise PP300SError('NOT_FOUND', 'serial port %s does not exist' % name)
        return self[name]()


@pytest.fixture
def ports(monkeypatch):
    p = Ports()
    p['SIM'] = sim.SimSerial
    monkeypatch.setattr(T, '_open_serial', p.open)
    return p


def fake(respond, **kw):
    """A factory that returns the same FakePort every time (port.fake is that port)."""
    port = FakePort(respond, **kw)
    f = lambda: port                                       # noqa: E731
    f.fake = port
    return f


def sequence(*factories):
    it = iter(factories)
    return lambda: next(it)()


def gone():
    raise PP300SError('NOT_FOUND', 'serial port does not exist')


@pytest.fixture
def no_sim(monkeypatch):
    monkeypatch.setenv('OPENAXPP300S_SIM', '0')


# ------------------------------------------------------------------------------------------------ happy path (sim)

def test_query_sim(simstate):
    with T.Session('SIM') as s:
        d = s.query()
        assert s.stats == {'frames': 1, 'retries': 0, 'reopens': 0}
    assert d['vin_v'] == pytest.approx(19.8) and d['temp_c'] == pytest.approx(28.5)
    assert d['output_on'] is False and d['mode'] == 'OFF' and d['vout_v'] == 0.0
    assert d['latency_ms'] >= 0 and 'attempts' not in d
    assert P.decode_reply(bytes.fromhex(d['raw']))['vin_v'] == d['vin_v']
    assert simstate()['frames'] == 1


def test_write_sim(simstate):
    with T.Session('SIM') as s:
        w = s.write(0x00, 500)
        w2 = s.write(0x02, 700)
        d = s.query()
        assert s.stats == {'frames': 3, 'retries': 0, 'reopens': 0}
    assert w == {'id': '0x00', 'data': 500, 'frame': 'a5 00 f4 01 f5 5a', 'ack': '01',
                 'latency_ms': w['latency_ms'], 'attempts': 1}
    assert w['latency_ms'] >= 0
    assert w2['id'] == '0x02' and w2['frame'] == P.encode(0x02, 700).hex(' ') and w2['ack'] == '01'
    st = simstate()
    assert st['vset'] == 500 and st['ovp'] == 700 and st['frames'] == 3
    assert d['output_on'] is False


def test_session_closes_port_and_releases_lock():
    s = T.Session('SIM')
    with s:
        ser = s.ser
        s.query()
    assert s.ser is None and ser.is_open is False
    with PortLock('SIM', 0):
        pass
    s.close()                                              # a second close is harmless


def test_open_serial_sim():
    assert isinstance(T._open_serial('SIM'), sim.SimSerial)


def test_open_serial_refuses_real_ports_when_disabled():
    with pytest.raises(PP300SError) as ei:
        T._open_serial('NOT_A_REAL_PORT_FOR_TESTS')
    assert ei.value.code == 'PORT_ERROR' and 'OPENAXPP300S_NO_SERIAL' in ei.value.message


# ------------------------------------------------------------------------------------------------ no answer (sim off)

def test_query_powered_off_retries_then_no_reply(simstate):
    simstate(powered=False)
    traces = []
    with T.Session('SIM', timeout=0.05, retries=2, trace=traces.append) as s:
        with pytest.raises(PP300SError) as ei:
            s.query()
        assert s.stats['frames'] == 3 and s.stats['retries'] == 2
    assert ei.value.code == 'NO_REPLY' and ei.value.exit_code == 6 and ei.value.hint
    assert [t['attempt'] for t in traces] == [1, 2, 3]
    assert all(t['tx'] == QUERY_HEX and t['rx'] == '' for t in traces)
    audit = simstate()['audit']
    assert len(audit) == 3 and all(e.get('note') == 'supply off' for e in audit)


def test_write_powered_off_retries_then_no_reply(simstate):
    simstate(powered=False)
    traces = []
    with T.Session('SIM', timeout=0.05, retries=1, trace=traces.append) as s:
        with pytest.raises(PP300SError) as ei:
            s.write(0x00, 500)
        assert s.stats['frames'] == 2 and s.stats['retries'] == 1
    assert ei.value.code == 'NO_REPLY' and 'acknowledge' in ei.value.message
    assert [t['attempt'] for t in traces] == [1, 2] and all(t['rx'] == '' for t in traces)
    assert simstate()['vset'] == 1200                       # nothing reached the (unpowered) supply


def test_write_retry_after_lost_frame(ports):
    ports['COMX'] = f = fake(lambda frame, n: b'' if n == 1 else b'\x01')
    with T.Session('COMX', timeout=0.03) as s:
        w = s.write(0x01, 100)
        assert s.stats == {'frames': 2, 'retries': 1, 'reopens': 0}
    assert w['attempts'] == 2 and w['ack'] == '01'
    assert f.fake.written == [P.encode(0x01, 100)] * 2       # the same frame was sent again


def test_retries_zero_sends_once(ports):
    ports['COMX'] = f = fake(silent)
    with T.Session('COMX', timeout=0.03, retries=0) as s:
        with pytest.raises(PP300SError) as ei:
            s.write(0x00, 0)
        assert s.stats['retries'] == 0
    assert ei.value.code == 'NO_REPLY' and len(f.fake.written) == 1


# ------------------------------------------------------------------------------------------------ bad answers

def test_bad_reply_on_garbage_instead_of_ack(ports):
    ports['COMX'] = f = fake(lambda frame, n: b'\x55\xaa\x00')
    with T.Session('COMX', timeout=0.03, retries=2) as s:
        with pytest.raises(PP300SError) as ei:
            s.write(0x00, 500)
    assert ei.value.code == 'BAD_REPLY' and ei.value.exit_code == 6
    assert '55 aa 00' in ei.value.message
    assert len(f.fake.written) == 3


def test_bad_reply_on_garbage_instead_of_status(ports):
    ports['COMX'] = fake(lambda frame, n: bytes(range(0x10, 0x30)))
    with T.Session('COMX', timeout=0.03, retries=1) as s:
        with pytest.raises(PP300SError) as ei:
            s.query()
    assert ei.value.code == 'BAD_REPLY'


def test_bad_reply_on_corrupted_status(ports):
    bad = bytearray(VENDOR_REPLY)
    bad[3] ^= 0x01                                         # checksum no longer matches
    ports['COMX'] = fake(lambda frame, n: bytes(bad))
    with T.Session('COMX', timeout=0.03, retries=1) as s:
        with pytest.raises(PP300SError) as ei:
            s.query()
    assert ei.value.code == 'BAD_REPLY' and bytes(bad).hex(' ') in ei.value.message


def test_truncated_status_reply_is_bad_reply(ports):
    """Only the first 10 bytes of a reply arrive (cable glitch): BAD_REPLY, not a crash."""
    ports['COMX'] = fake(lambda frame, n: VENDOR_REPLY[:10])
    with T.Session('COMX', timeout=0.03, retries=1) as s:
        with pytest.raises(PP300SError) as ei:
            s.query()
    assert ei.value.code == 'BAD_REPLY'


def test_status_reply_split_over_two_reads(ports):
    """A real USB-serial port can deliver the 15-byte reply in pieces; the reader must wait for the rest."""
    ports['COMX'] = fake(lambda frame, n: [VENDOR_REPLY[:8], VENDOR_REPLY[8:]])
    with T.Session('COMX', timeout=0.05) as s:
        d = s.query()
    assert d['vout_v'] == pytest.approx(5.0) and d['raw'] == VENDOR_REPLY.hex(' ')


def test_status_reply_byte_by_byte(ports):
    ports['COMX'] = fake(lambda frame, n: [VENDOR_REPLY[i:i + 1] for i in range(len(VENDOR_REPLY))])
    with T.Session('COMX', timeout=0.05) as s:
        assert s.query()['vin_v'] == pytest.approx(19.72)


def test_query_after_stray_ack(ports):
    ports['COMX'] = fake(lambda frame, n: b'\x01' + VENDOR_REPLY)
    with T.Session('COMX', timeout=0.05) as s:
        d = s.query()
    assert d['vout_v'] == pytest.approx(5.0) and d['mode'] == 'CV' and 'attempts' not in d


def test_query_after_junk(ports):
    ports['COMX'] = fake(lambda frame, n: b'\x00\xa5\x13' + VENDOR_REPLY + b'\x01')
    with T.Session('COMX', timeout=0.05) as s:
        assert s.query()['vin_v'] == pytest.approx(19.72)


def test_ack_inside_reply_is_retried(ports):
    """Firmware: an ACK can land inside a status reply that is going out. That reply is unusable; the query is sent
    again and the clean reply is used."""
    broken = VENDOR_REPLY[:6] + b'\x01' + VENDOR_REPLY[6:]
    ports['COMX'] = fake(lambda frame, n: broken if n == 1 else VENDOR_REPLY)
    with T.Session('COMX', timeout=0.03) as s:
        d = s.query()
    assert d['attempts'] == 2 and d['vout_v'] == pytest.approx(5.0)


def test_echoed_frame_counts_as_ack(ports):
    ports['COMX'] = fake(lambda frame, n: frame)
    with T.Session('COMX', timeout=0.03) as s:
        w = s.write(0x08, 0)
    assert w['ack'] == P.encode(0x08, 0).hex(' ') and w['attempts'] == 1


def test_ack_followed_by_junk(ports):
    ports['COMX'] = fake(lambda frame, n: b'\x01\xff\xa5')
    with T.Session('COMX', timeout=0.03) as s:
        assert s.write(0x00, 0)['ack'] == '01'


def test_invalid_frame_echo_is_bad_reply(ports):
    ports['COMX'] = fake(lambda frame, n: bytes.fromhex('a5 00 00 00 01 5a'))      # bad checksum
    with T.Session('COMX', timeout=0.03, retries=0) as s:
        with pytest.raises(PP300SError) as ei:
            s.write(0x00, 0)
    assert ei.value.code == 'BAD_REPLY'


@pytest.mark.parametrize('answer', [P.encode(0x00, 1), P.encode(0x01, 0), b'\x00\x01', b'\xa5\x01'],
                         ids=['echo_other_data', 'echo_other_id', 'ack_not_leading', 'ack_after_a5'])
def test_only_leading_ack_or_exact_echo_counts(ports, answer):
    """ACK: only a leading 0x01, or an exact echo of the frame just sent, counts; a valid echo of another frame or a
    0x01 after other bytes does not."""
    ports['COMX'] = f = fake(lambda frame, n: answer)
    with T.Session('COMX', timeout=0.03, retries=1) as s:
        with pytest.raises(PP300SError) as ei:
            s.write(0x00, 0)
    assert ei.value.code == 'BAD_REPLY' and answer.hex(' ') in ei.value.message
    assert f.fake.written == [P.encode(0x00, 0)] * 2


# ------------------------------------------------------------------------------------------------ _find_reply

@pytest.mark.parametrize('prefix', [b'', b'\x01', b'\x01\x01', b'\x00\xff\x13', b'\xa5', b'\xa5\x5a\x01',
                                    bytes(range(40))],
                         ids=['none', 'ack', 'two_acks', 'junk', 'lone_a5', 'a5_5a', 'long_junk'])
def test_find_reply_skips_leading_bytes(prefix):
    assert T._find_reply(prefix + VENDOR_REPLY) == VENDOR_REPLY
    assert T._find_reply(prefix + VENDOR_REPLY + b'\x01\xa5') == VENDOR_REPLY


def test_find_reply_skips_a_frame_shaped_candidate_with_a_bad_checksum():
    bad = bytearray(VENDOR_REPLY)
    bad[5] ^= 0x01
    assert T._find_reply(bytes(bad) + VENDOR_REPLY) == VENDOR_REPLY


def test_find_reply_first_of_two():
    other = P.build_reply(1000, 0, 0, 1980, 285, 1, 0)
    assert T._find_reply(b'\x01' + other + VENDOR_REPLY) == other


@pytest.mark.parametrize('buf', [
    b'', b'\x01', b'\xa5', VENDOR_REPLY[:8], VENDOR_REPLY[:14], b'\x01' + VENDOR_REPLY[:14], bytes(15),
    VENDOR_REPLY[:6] + b'\x01' + VENDOR_REPLY[6:],
    bytes(VENDOR_REPLY[:13]) + b'\x00\x5a',
], ids=['empty', 'ack', 'lone_a5', 'half', 'one_short', 'ack_one_short', 'zeros', 'ack_inside', 'bad_sum'])
def test_find_reply_none(buf):
    assert T._find_reply(buf) is None


# ------------------------------------------------------------------------------------------------ serial I/O errors

def test_io_error_reopens_and_retries(ports):
    first = FakePort(supply_like, fail_writes=1)
    second = FakePort(supply_like)
    ports['COMX'] = sequence(lambda: first, lambda: second)
    traces = []
    with T.Session('COMX', timeout=0.03, trace=traces.append) as s:
        w = s.write(0x00, 500)
        assert s.stats == {'frames': 1, 'retries': 1, 'reopens': 1}
    assert w['attempts'] == 2 and w['ack'] == '01'
    assert ports.opened == ['COMX', 'COMX'] and first.is_open is False
    assert second.written == [P.encode(0x00, 500)]
    assert traces[0]['attempt'] == 1 and traces[0]['rx'] == '' and traces[0]['note'].startswith('io-error')
    assert traces[1]['attempt'] == 2 and traces[1]['rx'] == '01' and 'note' not in traces[1]


def test_io_error_and_port_gone(ports):
    ports['COMX'] = sequence(lambda: FakePort(supply_like, fail_writes=1), gone)
    with pytest.raises(PP300SError) as ei:
        with T.Session('COMX', timeout=0.03) as s:
            s.query()
    assert ei.value.code == 'NOT_FOUND'
    with PortLock('COMX', 0):                               # the lock was released
        pass


def test_io_error_on_every_attempt_is_port_error(ports):
    ports['COMX'] = lambda: FakePort(supply_like, fail_writes=99)
    with pytest.raises(PP300SError) as ei:
        with T.Session('COMX', timeout=0.03, retries=1) as s:
            s.write(0x00, 0)
    assert ei.value.code == 'PORT_ERROR' and ei.value.exit_code == 6 and 'COMX' in ei.value.message


# ------------------------------------------------------------------------------------------------ port lock

def test_lock_busy_when_another_handle_holds_it():
    holder = PortLock('SIM', 0).acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(PP300SError) as ei:
            with T.Session('SIM', lock_wait=0.05):
                pass
        assert time.monotonic() - t0 < 2.0
        assert ei.value.code == 'BUSY' and ei.value.exit_code == 4 and 'SIM' in ei.value.message
        with pytest.raises(PP300SError):
            PortLock('SIM', 0).acquire()
        with PortLock('COMOTHER', 0):                       # another port's lock is independent
            pass
    finally:
        holder.release()
    with T.Session('SIM', lock_wait=0) as s:
        assert s.query()['vin_v'] == pytest.approx(19.8)


def test_lock_waits_for_release():
    holder = PortLock('SIM', 0).acquire()
    timer = threading.Timer(0.1, holder.release)
    timer.start()
    try:
        with T.Session('SIM', lock_wait=5.0) as s:
            assert s.query()['output_on'] is False
    finally:
        timer.join()
        holder.release()


def test_lock_released_when_open_fails(ports):
    with pytest.raises(PP300SError) as ei:
        with T.Session('COMGONE'):
            pass
    assert ei.value.code == 'NOT_FOUND'
    with PortLock('COMGONE', 0):
        pass


def test_lock_path():
    p = lock_path(r'\\.\COM10')
    assert os.path.dirname(p) == os.path.join(state_dir(), 'locks')
    assert os.path.basename(p) == 'port___._COM10.lock'          # '\\.\' -> '__._'
    assert os.path.basename(lock_path('SIM')) == 'port_SIM.lock'
    assert p.startswith(os.path.abspath(os.environ['OPENAXPP300S_HOME']))


# ------------------------------------------------------------------------------------------------ ports and probing

@pytest.mark.parametrize('env,expected', [('1', True), ('yes', True), ('0', False), ('', False)])
def test_sim_enabled_env(monkeypatch, env, expected):
    monkeypatch.setenv('OPENAXPP300S_SIM', env)
    assert T.sim_enabled() is expected


def test_use_sim_overrides_env(no_sim):
    assert T.sim_enabled() is False
    T.use_sim(True)
    assert T.sim_enabled() is True
    T.use_sim(False)
    assert T.sim_enabled() is False


def test_list_ports_sim():
    ps = T.list_ports()
    assert [p['port'] for p in ps] == ['SIM'] and ps[0]['wch'] and not ps[0]['bluetooth']
    assert [p['port'] for p in T.candidates()] == ['SIM']


class PortInfo:
    def __init__(self, device, description, hwid, vid=None, pid=None):
        self.device, self.description, self.hwid, self.vid, self.pid = device, description, hwid, vid, pid


def test_list_ports_and_candidates(no_sim, monkeypatch):
    infos = [
        PortInfo('COM7', 'USB-SERIAL CH340 (COM7)', 'USB VID:PID=1A86:7523 SER=5&1 LOCATION=1-2', 0x1A86, 0x7523),
        PortInfo('COM3', 'Standard Serial over Bluetooth link (COM3)', 'BTHENUM\\{00001101-0000}_LOCALMFG&0000'),
        PortInfo('COM1', 'Communications Port (COM1)', 'ACPI\\PNP0501\\0'),
        PortInfo('COM12', 'USB Serial Device (COM12)', 'USB VID:PID=0483:5740 SER=1', 0x0483, 0x5740),
        PortInfo('COM5', 'USB-SERIAL CH9102 (COM5)', 'USB VID:PID=1A86:55D4', 0x1A86, 0x55D4),
    ]
    monkeypatch.setattr(serial.tools.list_ports, 'comports', lambda *a, **k: list(infos))
    ps = {p['port']: p for p in T.list_ports()}
    assert [p['port'] for p in T.list_ports()] == ['COM1', 'COM12', 'COM3', 'COM5', 'COM7']
    assert ps['COM7']['wch'] and ps['COM7']['usb'] and not ps['COM7']['bluetooth']
    assert ps['COM3']['bluetooth'] and not ps['COM3']['usb']
    assert not ps['COM12']['wch'] and ps['COM12']['usb']
    assert [p['port'] for p in T.candidates()] == ['COM5', 'COM7', 'COM12']
    assert [p['port'] for p in T.candidates(include_all=True)] == ['COM5', 'COM7', 'COM1', 'COM12']


def test_probe_sim():
    d = T.probe('SIM')
    assert d is not None and d['vin_v'] == pytest.approx(19.8)


def test_probe_missing_port_is_none(ports):
    assert T.probe('COMGONE') is None


def test_probe_silent_port_is_none(ports):
    ports['COMQUIET'] = fake(silent)
    assert T.probe('COMQUIET', timeout=0.03) is None


def test_discover_sim():
    assert T.discover() == 'SIM'
    assert state.remembered_port() == 'SIM'


def test_discover_first_answering_port(no_sim, monkeypatch):
    probed = []
    monkeypatch.setattr(T, 'candidates', lambda include_all=False: [{'port': 'COMA'}, {'port': 'COMB'},
                                                                     {'port': 'COMC'}])

    def probe(port, timeout=0.25):
        probed.append(port)
        return {'vin_v': 19.8} if port == 'COMB' else None
    monkeypatch.setattr(T, 'probe', probe)
    assert T.discover() == 'COMB'
    assert probed == ['COMA', 'COMB'] and state.remembered_port() == 'COMB'


def test_discover_none_answers(no_sim, monkeypatch):
    monkeypatch.setattr(T, 'candidates', lambda include_all=False: [{'port': 'COMA'}, {'port': 'COMB'}])
    monkeypatch.setattr(T, 'probe', lambda port, timeout=0.25: None)
    with pytest.raises(PP300SError) as ei:
        T.discover()
    assert ei.value.code == 'NOT_FOUND' and ei.value.exit_code == 3
    assert ei.value.extra['ports_tried'] == ['COMA', 'COMB'] and 'COMA, COMB' in ei.value.message
    assert state.remembered_port() is None


def test_discover_no_candidates(no_sim):
    with pytest.raises(PP300SError) as ei:
        T.discover()
    assert ei.value.code == 'NOT_FOUND' and 'any USB serial port' in ei.value.message


def test_discover_skips_a_silent_port(no_sim, ports, monkeypatch):
    """Another USB-serial device that never answers comes first: discovery must move on to the supply, not crash."""
    ports['COMQUIET'] = fake(silent)
    ports['COMGOOD'] = sim.SimSerial
    monkeypatch.setattr(T, 'candidates', lambda include_all=False: [{'port': 'COMQUIET'}, {'port': 'COMGOOD'}])
    assert T.discover() == 'COMGOOD'
    assert state.remembered_port() == 'COMGOOD'


# ------------------------------------------------------------------------------------------------ resolve_port

def _no_discover(monkeypatch):
    def boom():
        raise AssertionError('discover() must not run')
    monkeypatch.setattr(T, 'discover', boom)


def test_resolve_explicit_wins(no_sim, monkeypatch):
    _no_discover(monkeypatch)
    monkeypatch.setenv('OPENAXPP300S_PORT', 'COM7')
    state.remember_port('COM5')
    assert T.resolve_port('COM9') == ('COM9', 'explicit')


@pytest.mark.parametrize('how', ['use_sim', 'env'])
def test_resolve_explicit_real_port_conflicts_with_sim(how, no_sim, monkeypatch):
    """The simulator wins over everything: --sim / OPENAXPP300S_SIM=1 with --port COMx is refused, never a real
    port."""
    _no_discover(monkeypatch)
    monkeypatch.setenv('OPENAXPP300S_PORT', 'COM7')
    state.remember_port('COM5')
    if how == 'use_sim':
        T.use_sim(True)
    else:
        monkeypatch.setenv('OPENAXPP300S_SIM', '1')
    with pytest.raises(PP300SError) as ei:
        T.resolve_port('COM9')
    assert ei.value.code == 'INVALID_ARG' and ei.value.exit_code == 2 and 'COM9' in ei.value.message
    assert T.resolve_port('SIM') == ('SIM', 'sim')
    assert T.resolve_port('sim') == ('SIM', 'sim')


def test_resolve_sim_beats_env(monkeypatch):
    """OPENAXPP300S_PORT is ignored in sim mode."""
    _no_discover(monkeypatch)
    monkeypatch.setenv('OPENAXPP300S_PORT', 'COM7')
    state.remember_port('COM5')
    assert T.resolve_port() == ('SIM', 'sim')
    assert T.resolve_port('') == ('SIM', 'sim')
    assert T.resolve_port(None) == ('SIM', 'sim')


def test_resolve_env_beats_remembered(no_sim, monkeypatch):
    _no_discover(monkeypatch)
    monkeypatch.setenv('OPENAXPP300S_PORT', 'COM7')
    state.remember_port('COM5')
    assert T.resolve_port() == ('COM7', 'env')
    assert T.resolve_port('') == ('COM7', 'env')
    assert T.resolve_port(None) == ('COM7', 'env')


def test_resolve_sim_beats_remembered(monkeypatch):
    _no_discover(monkeypatch)
    state.remember_port('COM5')
    assert T.resolve_port() == ('SIM', 'sim')


def test_resolve_use_sim_beats_remembered(no_sim, monkeypatch):
    _no_discover(monkeypatch)
    state.remember_port('COM5')
    T.use_sim(True)
    assert T.resolve_port() == ('SIM', 'sim')


def test_resolve_remembered_beats_discover(no_sim, monkeypatch):
    _no_discover(monkeypatch)
    state.remember_port('COM5')
    assert T.resolve_port() == ('COM5', 'remembered')


def test_resolve_remembered_sim_is_ignored_without_sim(no_sim, monkeypatch):
    monkeypatch.setattr(T, 'discover', lambda: 'COM8')
    state.remember_port('SIM')
    assert T.resolve_port() == ('COM8', 'probe')


def test_resolve_discover_last(no_sim, monkeypatch):
    monkeypatch.setattr(T, 'discover', lambda: 'COM8')
    assert T.resolve_port() == ('COM8', 'probe')


def test_resolve_discover_failure_propagates(no_sim):
    with pytest.raises(PP300SError) as ei:
        T.resolve_port()
    assert ei.value.code == 'NOT_FOUND'


# ------------------------------------------------------------------------------------------------ Supply._do

@pytest.fixture
def rediscover(monkeypatch):
    """device.discover replaced: returns 'COMNEW'; .calls counts the calls."""
    calls = []

    def discover():
        calls.append(1)
        return 'COMNEW'
    monkeypatch.setattr(device, 'discover', discover)
    return calls


def test_do_rediscovers_when_remembered_port_is_gone(no_sim, ports, rediscover):
    state.remember_port('COMOLD')
    ports['COMNEW'] = sim.SimSerial
    sup = device.Supply()
    assert (sup.port, sup.port_how) == ('COMOLD', 'remembered')
    d = sup.status()
    assert d['vin_v'] == pytest.approx(19.8) and d['port'] == 'COMNEW'
    assert (sup.port, sup.port_how) == ('COMNEW', 'probe') and rediscover == [1]
    assert ports.opened == ['COMOLD', 'COMNEW']
    assert state.remembered_port() == 'COMNEW'


def test_do_rediscovers_when_remembered_port_is_silent_write(no_sim, ports, rediscover, simstate):
    state.remember_port('COMOLD')
    ports['COMOLD'] = fake(silent)
    ports['COMNEW'] = sim.SimSerial
    sup = device.Supply(timeout=0.03, retries=0)
    ans = sup._do(lambda s: s.write(0x00, 500))
    assert ans['ack'] == '01' and sup.port == 'COMNEW' and rediscover == [1]
    assert simstate()['vset'] == 500
    assert state.remembered_port() == 'COMNEW'


def test_do_rediscovers_when_remembered_port_is_silent_status(no_sim, ports, rediscover):
    """The usual case after a replug: the remembered COM number now belongs to a device that does not answer."""
    state.remember_port('COMOLD')
    ports['COMOLD'] = fake(silent)
    ports['COMNEW'] = sim.SimSerial
    sup = device.Supply(timeout=0.03, retries=0)
    d = sup.status()
    assert d['port'] == 'COMNEW' and rediscover == [1]


def test_do_no_rediscovery_for_explicit_port(no_sim, ports, rediscover):
    sup = device.Supply('COMX')
    assert sup.port_how == 'explicit'
    with pytest.raises(PP300SError) as ei:
        sup.query()
    assert ei.value.code == 'NOT_FOUND' and rediscover == []


def test_do_no_rediscovery_after_a_success(no_sim, ports, rediscover):
    state.remember_port('COMOLD')
    ports['COMOLD'] = sim.SimSerial
    sup = device.Supply()
    sup.query()
    del ports['COMOLD']                                     # unplugged after the first command
    with pytest.raises(PP300SError) as ei:
        sup.query()
    assert ei.value.code == 'NOT_FOUND' and rediscover == [] and sup.port == 'COMOLD'


def test_do_no_rediscovery_when_busy(no_sim, ports, rediscover):
    state.remember_port('COMOLD')
    ports['COMOLD'] = sim.SimSerial
    holder = PortLock('COMOLD', 0).acquire()
    try:
        with pytest.raises(PP300SError) as ei:
            device.Supply(lock_wait=0.02).query()
    finally:
        holder.release()
    assert ei.value.code == 'BUSY' and rediscover == []


def test_do_rediscovery_failure_propagates(no_sim, ports, monkeypatch):
    state.remember_port('COMOLD')

    def discover():
        raise PP300SError('NOT_FOUND', 'no AX PP300S answered')
    monkeypatch.setattr(device, 'discover', discover)
    with pytest.raises(PP300SError) as ei:
        device.Supply().query()
    assert ei.value.code == 'NOT_FOUND' and 'no AX PP300S' in ei.value.message


def test_do_remembers_a_working_explicit_port(no_sim, ports):
    ports['COMX'] = sim.SimSerial
    device.Supply('COMX').query()
    assert state.remembered_port() == 'COMX'


def test_do_does_not_remember_sim():
    sup = device.Supply()
    assert sup.port == 'SIM'
    sup.query()
    assert state.remembered_port() is None


# ------------------------------------------------------------------------------------------------ traces

def test_trace_callback_sim():
    traces = []
    with T.Session('SIM', trace=traces.append) as s:
        d = s.query()
        s.write(0x08, 0)
    assert len(traces) == 2
    q, a = traces
    assert set(q) == {'tx', 'rx', 'ms', 'attempt'}
    assert q['tx'] == QUERY_HEX and q['rx'] == d['raw'] and q['attempt'] == 1
    assert isinstance(q['ms'], float) and q['ms'] >= 0
    assert a['tx'] == 'a5 08 00 00 08 5a' and a['rx'] == '01' and a['attempt'] == 1


def test_trace_through_supply():
    traces = []
    device.Supply(trace=traces.append).status()
    assert [t['tx'] for t in traces] == [QUERY_HEX]
    assert len(bytes.fromhex(traces[0]['rx'])) == P.REPLY_LEN
