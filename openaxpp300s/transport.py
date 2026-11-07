"""Serial port discovery and per-transaction sessions with the PP300S.

A Session holds the port lock and the open port for a few frames, then closes both (opening a CH340 port costs
about 1 ms, so commands never keep the port: a background log and a foreground command share the supply).
One frame is in flight at a time; the next frame is sent only after the answer (or the timeout, then a pause and
an input flush, so a late answer cannot be mistaken for the next one). Setting frames and status queries are
idempotent, so a frame without an answer is simply sent again (default 2 retries). A serial I/O error (seen once
on hardware: WriteFile 'access denied' without the device leaving Windows) closes and reopens the port before the
retry.

Backends: pyserial for real ports; the virtual supply (sim.py) for port 'SIM' (--sim or OPENAXPP300S_SIM=1).
OPENAXPP300S_NO_SERIAL=1 makes real ports refuse to open (tests, CI).
"""
from __future__ import annotations

import os
import time

from . import protocol as P
from . import state
from .errors import PP300SError
from .lock import PortLock

SIM_PORT = 'SIM'
WCH_VID = 0x1A86                      # CH340 (7523), CH341 (5523), CH9102 (55D4) USB-serial bridges
DEFAULT_TIMEOUT = 0.3                 # s per attempt; measured: ACK ~13 ms, status reply 15-55 ms
DEFAULT_RETRIES = 2
SETTLE_AFTER_TIMEOUT = 0.08           # s: let a late answer arrive before the input is flushed
MIN_GAP = 0.003                       # s between the last byte received and the next frame
_SIM = [False]


def use_sim(on=True):
    _SIM[0] = bool(on)


def sim_enabled():
    return _SIM[0] or os.environ.get('OPENAXPP300S_SIM', '') not in ('', '0')


def list_ports():
    """Serial ports of this PC: port, description, hwid, vid, pid, usb, wch (a WCH CH34x/CH910x bridge like the
    PP300S's), bluetooth."""
    if sim_enabled():
        return [{'port': SIM_PORT, 'description': 'virtual AX PP300S (openaxpp300s sim)', 'hwid': 'SIM',
                 'vid': WCH_VID, 'pid': 0x7523, 'usb': True, 'wch': True, 'bluetooth': False}]
    from serial.tools import list_ports as lp
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        hwid = p.hwid or ''
        out.append({'port': p.device, 'description': p.description, 'hwid': hwid, 'vid': p.vid, 'pid': p.pid,
                    'usb': p.vid is not None, 'wch': p.vid == WCH_VID,
                    'bluetooth': 'BTHENUM' in hwid.upper() or 'BLUETOOTH' in (p.description or '').upper()})
    return out


def _open_serial(name):
    if name == SIM_PORT:
        from .sim import SimSerial
        return SimSerial()
    if os.environ.get('OPENAXPP300S_NO_SERIAL', '') not in ('', '0'):
        raise PP300SError('PORT_ERROR', 'real serial ports are disabled (OPENAXPP300S_NO_SERIAL is set)',
                          'unset OPENAXPP300S_NO_SERIAL, or use --sim')
    import serial
    s = serial.Serial()
    s.port = name
    s.baudrate = P.BAUD
    s.bytesize, s.parity, s.stopbits = serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE
    s.timeout = 0
    s.write_timeout = 1.0
    s.dtr = False                     # leave the modem lines deasserted (the supply does not use them)
    s.rts = False
    try:
        s.open()
    except serial.SerialException as e:
        text = str(e)
        if 'FileNotFoundError' in text or 'could not open port' in text and 'cannot find' in text.lower() \
                or 'No such file' in text:
            raise PP300SError('NOT_FOUND', 'serial port %s does not exist' % name,
                              'is the supply plugged in by its serial Type-C port? "openaxpp300s ports" lists '
                              'the ports') from None
        if 'PermissionError' in text or 'Access is denied' in text or 'denied' in text.lower() or 'busy' in \
                text.lower():
            raise PP300SError('BUSY', 'serial port %s is held by another program' % name,
                              'close the vendor PC app (AX PP300S 上位机), a serial terminal or FlyMcu, then '
                              'retry') from None
        raise PP300SError('PORT_ERROR', 'cannot open %s: %s' % (name, text)) from None
    return s


class Session:
    """The port open and locked for a few transactions. Use as a context manager."""

    def __init__(self, port, lock_wait=5.0, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES, trace=None):
        self.port = port
        self.lock = PortLock(port, lock_wait)
        self.timeout = timeout
        self.retries = retries
        self.ser = None
        self.trace = trace            # callable(dict) for --verbose frame traces
        self._last_rx = 0.0
        self.stats = {'frames': 0, 'retries': 0, 'reopens': 0}

    def __enter__(self):
        self.lock.acquire()
        try:
            self.ser = _open_serial(self.port)
        except BaseException:
            self.lock.release()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:             # noqa: BLE001 - closing a dead port must not hide the real error
            pass
        self.ser = None
        self.lock.release()

    def _reopen(self):
        try:
            self.ser.close()
        except Exception:             # noqa: BLE001
            pass
        time.sleep(0.1)
        self.ser = _open_serial(self.port)
        self.stats['reopens'] += 1

    def _read_until(self, done, deadline):
        buf = bytearray()
        while True:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n)
                if done(buf):
                    break
            elif time.perf_counter() >= deadline:
                break
            else:
                time.sleep(0.0005)
        if buf:
            self._last_rx = time.perf_counter()
        return bytes(buf)

    def transact(self, frame, expect):
        """Send one frame; expect 'ack' (one 0x01) or 'reply' (15-byte status) -> (answer, latency_s, attempts)."""
        import serial
        last = None
        for attempt in range(1, self.retries + 2):
            gap = self._last_rx + MIN_GAP - time.perf_counter()
            if gap > 0:
                time.sleep(gap)
            t0 = time.perf_counter()
            try:
                self.ser.reset_input_buffer()
                self.ser.write(frame)
                if expect == 'ack':
                    buf = self._read_until(lambda b: len(b) >= 1 and b[0] == P.ACK or len(b) >= P.FRAME_LEN,
                                           t0 + self.timeout)
                else:
                    buf = self._read_until(lambda b: _find_reply(b) is not None, t0 + self.timeout)
            except (serial.SerialException, OSError) as e:
                last = PP300SError('PORT_ERROR', 'serial I/O error on %s: %s' % (self.port, e),
                                   'check the cable; the command was retried after reopening the port')
                self._trace(frame, b'', t0, attempt, 'io-error: %s' % e)
                if attempt > self.retries:                # no attempt left: do not reopen for nothing
                    break
                try:
                    self._reopen()
                except PP300SError as e2:
                    last = e2
                    break
                self.stats['retries'] += 1
                continue
            dt = time.perf_counter() - t0
            self.stats['frames'] += 1
            self._trace(frame, buf, t0, attempt)
            if expect == 'ack':
                if buf[:1] == bytes([P.ACK]):
                    return buf[:1], dt, attempt
                if buf[:P.FRAME_LEN] == frame:            # an echo of our own frame also counts (vendor app does)
                    return buf[:P.FRAME_LEN], dt, attempt
                last = _no_answer(self.port, buf, 'acknowledge (0x01)')
            else:
                hit = _find_reply(buf)
                if hit is not None:
                    return hit, dt, attempt
                last = _no_answer(self.port, buf, 'status reply')
            self.stats['retries'] += 1
            time.sleep(SETTLE_AFTER_TIMEOUT)
            try:
                self.ser.reset_input_buffer()
            except (serial.SerialException, OSError):
                pass
        if self.stats['retries']:
            self.stats['retries'] -= 1                   # the last failed attempt was not a retry
        raise last

    def _trace(self, frame, buf, t0, attempt, note=None):
        if self.trace:
            d = {'tx': frame.hex(' '), 'rx': buf.hex(' '), 'ms': round((time.perf_counter() - t0) * 1000, 1),
                 'attempt': attempt}
            if note:
                d['note'] = note
            self.trace(d)

    def query(self):
        """One status query -> decoded dict with latency_ms."""
        raw, dt, attempts = self.transact(P.query_frame(), 'reply')
        d = P.decode_reply(raw)
        d['latency_ms'] = round(dt * 1000, 1)
        if attempts > 1:
            d['attempts'] = attempts
        return d

    def write(self, reg_id, data):
        """One setting frame -> {'id', 'data', 'ack', 'latency_ms', 'attempts'}."""
        frame = P.encode(reg_id, data)
        ans, dt, attempts = self.transact(frame, 'ack')
        return {'id': '0x%02X' % reg_id, 'data': data, 'frame': frame.hex(' '), 'ack': ans.hex(' '),
                'latency_ms': round(dt * 1000, 1), 'attempts': attempts}


def _find_reply(buf):
    """A valid 15-byte status reply anywhere in buf (a stray late ACK may precede it), else None."""
    for i in range(len(buf) - P.REPLY_LEN + 1):           # empty when the buffer is shorter than one reply
        if buf[i] == P.FRAME_HEAD and buf[i + P.REPLY_LEN - 1] == P.FRAME_TAIL:
            cand = buf[i:i + P.REPLY_LEN]
            if P.reply_checksum(cand) == cand[13]:
                return bytes(cand)
    return None


def _no_answer(port, buf, what):
    if not buf:
        return PP300SError('NO_REPLY', 'no %s from %s' % (what, port),
                           'is the supply on (screen lit, not in its white-screen bootloader) and connected by '
                           'its serial Type-C port? Is this the right port ("openaxpp300s ports --probe")?')
    return PP300SError('BAD_REPLY', 'unexpected answer from %s instead of a %s: %s' % (port, what, buf.hex(' ')),
                       'another program may be talking to the supply, or this port is another device')


# ------------------------------------------------------------------ finding the supply

def probe(port, timeout=0.25):
    """Status of a PP300S on port, or None (no answer, busy, not a PP300S)."""
    try:
        with Session(port, lock_wait=1.0, timeout=timeout, retries=1) as s:
            return s.query()
    except PP300SError:
        return None


def candidates(include_all=False):
    """Ports worth probing: USB serial ports, WCH bridges first; Bluetooth serial ports never (opening one can hang
    for many seconds while Windows tries to reach the remote device)."""
    ports = [p for p in list_ports() if not p['bluetooth'] and (p['usb'] or include_all)]
    return sorted(ports, key=lambda p: (not p['wch'], p['port']))


def resolve_port(explicit=None):
    """-> (port, how): how is 'sim', 'explicit', 'env', 'remembered' or 'probe' (in that order of preference).
    The simulator wins over everything: --sim must never reach a real supply."""
    if sim_enabled():
        if explicit and explicit.upper() != SIM_PORT:
            raise PP300SError('INVALID_ARG', '--port %s conflicts with --sim / OPENAXPP300S_SIM' % explicit,
                              'drop --port to use the virtual supply, or drop --sim')
        return SIM_PORT, 'sim'
    if explicit:
        return explicit, 'explicit'
    env = os.environ.get('OPENAXPP300S_PORT')
    if env:
        return env, 'env'
    rem = state.remembered_port()
    if rem and rem != SIM_PORT:
        return rem, 'remembered'
    return discover(), 'probe'


def discover():
    """Probe the candidate ports; the first that answers like a PP300S is remembered and returned."""
    tried = []
    for p in candidates():
        tried.append(p['port'])
        if probe(p['port']) is not None:
            state.remember_port(p['port'])
            return p['port']
    raise PP300SError('NOT_FOUND', 'no AX PP300S answered on %s' % (', '.join(tried) or 'any USB serial port'),
                      'plug the supply in by its serial Type-C port (the one marked as data/serial), switch it '
                      'on, close the vendor PC app; or give the port with --port COMx',
                      {'ports_tried': tried})
