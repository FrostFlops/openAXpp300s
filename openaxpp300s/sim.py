"""Virtual AX PP300S: a drop-in for the pyserial port (SimSerial) plus a device model that follows the firmware
V1.4.4 image (docs/PROTOCOL.md, evidence addresses there) and what was measured on the real supply (2026-11-07).

Modelled:
- the byte parser of the firmware: hunt for A5, then take the next 5 bytes unconditionally (an A5 among them is
  data); valid if byte 3 = (ID + lo + hi) & 0xFF and byte 4 = 5A; a frame not complete 10 ms after its A5 is dropped;
  so garbage containing an A5 before a frame eats the frame, and a frame split over > 10 ms is lost [FIRMWARE];
- every valid frame except 0xFF is acknowledged with 01, also unknown ids and out-of-range data; nothing is
  range-checked, values are stored as sent (OPP, OTP, output, list index and priority keep only the low byte)
  [FIRMWARE]; the ACK arrives ~13 ms after the frame (USB latency of the CH340, measured);
- the status reply is sent on the next 50 ms tick; several queries within one tick get one reply [FIRMWARE];
- every valid frame (queries too) makes the firmware save the front-panel field under the cursor to its EEPROM;
  counted in eeprom_writes [FIRMWARE];
- output ON: the output flag (byte 12) is 1 at once without DELAY/REV; the voltage stays near 11 mV for ~1.1 s
  (soft start) then settles; byte 11 (CC flag) reads 1 until then and whenever the output is off [MEASURED];
  OFF: the voltage decays with ~0.1 s time constant (10 MOhm load) [MEASURED]; readings below 9 mV read 0
  [FIRMWARE];
- DELAY d: the flag goes on d s after ON; REV r: on after r s, then toggles every r s [MEASURED, FIRMWARE];
- protections: OVP trips when the output (with step overshoot) reaches OVP (VSET = OVP = 3.00 V tripped on
  hardware); OCP when I >= OCP; OPP when floor(P) > (OPP+1)*10 W; OTP when T >= OTP*10 C [FIRMWARE]; a trip
  switches the output off and latches the alarm, which only a front-panel key press clears (`sim press`); a remote
  ON still switches the output on again [FIRMWARE, MEASURED for OVP];
- screens: the Digital/Waveform registers act only there, the parameter lists only on the Chinese screen, which
  ignores DELAY/REV and applies the cut-off current (output off after 5 s below it) and cut-off time [FIRMWARE];
- CV/CC with a resistive load (sim parameter load_ohm; default 10 MOhm, a voltmeter).

State lives in a JSON file (state/sim/state.json) shared by all processes. Clock: the PC clock plus an offset that
`python -m openaxpp300s.sim advance S` moves forward. OPENAXPP300S_SIM_DELAY scales the answer latency (0 in tests).

Control: python -m openaxpp300s.sim reset | show | set key=value ... | advance SECONDS | press | audit [--clear]
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

from . import protocol as P
from .paths import ensure, state_dir

ACK_LATENCY = 0.013
TICK = 0.05
REPLY_TX = 0.0013
SOFT_START = 1.1
OFF_TAU = 0.1
SLEW_TAU = 0.08          # s, output following a setpoint change while on
PARSER_TIMEOUT = 0.0101
BYTE_TIME = 10 / P.BAUD
AUDIT_MAX = 500


def sim_dir():
    return ensure(os.path.join(state_dir(), 'sim'))


def state_file():
    return os.path.join(sim_dir(), 'state.json')


def latency_scale():
    try:
        return float(os.environ.get('OPENAXPP300S_SIM_DELAY', '1'))
    except ValueError:
        return 1.0


def default_state():
    return {
        'screen': 'digital',          # digital | waveform | chinese (front panel only)
        'firmware': '1.4.4',
        'vset': 1200, 'iset': 1010, 'ovp': 3050, 'ocp': 1050, 'delay': 0, 'rev': 0,   # first-boot defaults
        'opp': 30, 'otp': 8, 'priority': 1, 'list_sel': 0,
        'lists': [{'v': v, 'i': 1010, 'ovp': 3050, 'ocp': 1050, 'cut_i': 0, 'cut_h': 99, 'cut_m': 60, 'cut_s': 60}
                  for v in (500, 1200, 2000, 2400, 3000)],
        'colors': [P.COLOR_DEFAULTS[k] for k in P.COLOR_DEFAULTS],
        'vin': 19.80, 'temp': 28.5, 'load_ohm': 1e7,
        'on_cmd': False, 't_on': 0.0, 't_off': 0.0, 'v_at_off': 0.0,
        'alarm': None, 'cut': None, 't_trip': 0.0,
        'rx_state': 'hunt', 'rx_buf': [], 'rx_t_a5': 0.0,
        'cursor': 0,                  # front-panel selection: 0..5 = V-SET..REV, 6 = DATA (Digital/Waveform)
        'on_acc': 0.0, 'v_from': 0.0, 't_vchange': 0.0, 't_change': 0.0,
        'eeprom_writes': 0, 'frames': 0,
        'clock_offset': 0.0, 'powered': True,
        'audit': [],
    }


def load():
    try:
        with open(state_file(), encoding='utf-8') as f:
            st = json.load(f)
        base = default_state()
        base.update(st)
        return base
    except (OSError, ValueError):
        return default_state()


def save(st):
    from .state import write_json
    write_json(state_file(), st)


def now(st):
    return time.time() + st.get('clock_offset', 0.0)


# ------------------------------------------------------------------------------------------------ device model

def chinese(st):
    return st['screen'] == 'chinese'


def _active(st):
    """(vset, iset, ovp, ocp) raw words in force on the current screen."""
    if chinese(st):
        if st['list_sel'] > 4:                       # the firmware would read unrelated RAM
            return 0, 0, 0, 0
        L = st['lists'][st['list_sel']]
        return L['v'], L['i'], L['ovp'], L['ocp']
    return st['vset'], st['iset'], st['ovp'], st['ocp']


def _flag(st, t):
    """Output flag (reply byte 12) at time t, and the time it last went on (None when off)."""
    if not st['on_cmd'] or not st['powered']:
        return False, None
    dt = t - st['t_on']
    if chinese(st):
        return True, st['t_on']
    d, r = st['delay'], st['rev']
    if dt < d:
        return False, None
    if r > 0:
        k = math.floor((dt - d) / r)
        if k < 1:
            return False, None
        if k % 2 == 1:
            return True, st['t_on'] + d + k * r
        return False, None
    return True, st['t_on'] + d


def _solve(st, vset_raw, iset_raw):
    v = vset_raw / 100.0
    imax = iset_raw / 100.0
    r = st.get('load_ohm')
    if not r or r <= 0:
        return v, 0.0, False
    i = v / r
    if i > imax:
        return imax * r, imax, True
    return v, i, False


def _terminal(st, t):
    """(v, i, cc flag, on flag, t_up) at the output terminals at time t, before protection checks. A setpoint change
    while the output is on slews with SLEW_TAU; after OFF the voltage decays with OFF_TAU."""
    on, t_up = _flag(st, t)
    if not on:
        if st['t_off'] and t >= st['t_off']:
            v = st['v_at_off'] * math.exp(-(t - st['t_off']) / OFF_TAU)
            return (v if v >= 0.009 else 0.0), 0.0, True, False, None
        return 0.0, 0.0, True, False, None
    if t - t_up < SOFT_START:
        return 0.011, 0.0, True, True, t_up
    vset, iset, _, _ = _active(st)
    v_t, i_t, cc = _solve(st, vset, iset)
    tv = st.get('t_vchange', 0.0)
    if not cc and tv > t_up + SOFT_START and t >= tv:      # the CV loop slews; the CC loop is fast
        v = v_t + (st.get('v_from', v_t) - v_t) * math.exp(-(t - tv) / SLEW_TAU)
        i = i_t * (v / v_t) if v_t > 0 else 0.0
    else:
        v, i = v_t, i_t
    return v, i, cc, True, t_up


def _temp(st, v, i):
    """The temperature the reply reports and OTP compares (ambient + self-heating)."""
    return st['temp'] + min(40.0, v * i * 0.15)


def _switch_off(st, t, v_now, t_up=None):
    if t_up is not None and t >= t_up:
        st['on_acc'] = st.get('on_acc', 0.0) + (t - t_up)       # the firmware's on-time counter keeps counting
    st['on_cmd'] = False
    st['t_off'] = t
    st['v_at_off'] = v_now


def output(st, t):
    """-> (vout V, iout A, cc flag, on flag) at time t; applies protection trips and cut-offs (mutates st).

    Like the firmware, OVP / OCP / OTP are checked whatever the output state (OVP <= measured V, OCP <= I for
    100 ms, T >= OTP); OPP only matters with power flowing."""
    v, i, cc, on, t_up = _terminal(st, t)
    vset, iset, ovp, ocp = _active(st)
    settled = on and t_up is not None and t - t_up >= SOFT_START
    trip = None
    v_chk = v + (0.005 * v + 0.01 if settled else 0.0)          # step overshoot while regulating
    if ovp / 100.0 <= v_chk:
        trip = 'OVP'
    elif ocp / 100.0 <= i:
        since = max((t_up + SOFT_START) if settled else st.get('t_change', 0.0), st.get('t_change', 0.0))
        if t - since >= 0.1:
            trip = 'OCP'
    elif on and math.floor(v * i) > (st['opp'] + 1) * 10:
        trip = 'OPP'
    elif _temp(st, v, i) >= st['otp'] * 10:
        trip = 'OTP'
    if trip:
        st['alarm'] = trip
        st['t_trip'] = t
        if on:
            _switch_off(st, t, v, t_up)
        return v, 0.0, True, False
    if on and chinese(st) and st['list_sel'] <= 4:
        L = st['lists'][st['list_sel']]
        limit = L['cut_h'] * 3600 + L['cut_m'] * 60 + L['cut_s']
        if st.get('on_acc', 0.0) + (t - t_up) >= limit:
            st['cut'] = 'time'
            _switch_off(st, t, v, t_up)
            return v, 0.0, True, False
        if L['cut_i'] and i * 1000 < L['cut_i'] * 10 and t - t_up - SOFT_START > 6:
            st['cut'] = 'current'
            _switch_off(st, t, v, t_up)
            return v, 0.0, True, False
    return v, i, cc, on


def reply(st, t):
    v, i, cc, on = output(st, t)
    mv = round(v * 1000)
    ma = round(i * 1000)
    return P.build_reply(mv, ma, mv * ma // 10000, round(st['vin'] * 100), round(_temp(st, v, i) * 10), cc, on)


DIGITAL = {0x00: 'vset', 0x01: 'iset', 0x02: 'ovp', 0x03: 'ocp', 0x04: 'delay', 0x05: 'rev'}
LIST_FIELDS = {0xA: 'v', 0xB: 'i', 0xC: 'ovp', 0xD: 'ocp', 0xE: 'cut_i'}


def _setpoint_change(st, t):
    """Before a change of what is in force: remember where the output is, for the slew and the OCP timer."""
    v_now = _terminal(st, t)[0]
    st['v_from'] = v_now
    st['t_vchange'] = t
    st['t_change'] = t


def apply_frame(st, reg_id, data, t):
    """Apply one valid frame like the firmware's dispatcher (no range checks) -> answer bytes (b'' for a query,
    whose reply is sent on the next tick)."""
    lo = data & 0xFF
    # the cursor-field save runs after every valid frame; on the Digital/Waveform screens cursor 6 (DATA box) has
    # no field and saves nothing (0x08001F8A); on the Chinese screen every cursor position (0..8) saves something
    if chinese(st) or st.get('cursor', 0) < 6:
        st['eeprom_writes'] += 1
    if reg_id == P.QUERY_ID:
        return b''
    output(st, t)                                  # bring trips / cut-offs up to the frame's time
    if reg_id in DIGITAL:
        if reg_id <= 0x03 and not chinese(st):
            _setpoint_change(st, t)
        st[DIGITAL[reg_id]] = data
    elif reg_id == 0x06:
        st['opp'] = lo
    elif reg_id == 0x07:
        st['otp'] = lo
    elif reg_id == 0x08:
        if lo:
            if not st['on_cmd']:
                st['t_on'] = t
                if st.get('cut') == 'time':        # the on-time counter restarts after a time cut-off
                    st['on_acc'] = 0.0
            st['on_cmd'] = True
            st['cut'] = None
        elif st['on_cmd']:
            v, _, _, on, t_up = _terminal(st, t)
            _switch_off(st, t, v, t_up if on else None)
    elif reg_id == 0x09:
        if chinese(st):
            _setpoint_change(st, t)
        st['list_sel'] = lo
    elif reg_id == 0x10:
        st['priority'] = lo
    elif 0x50 <= reg_id <= 0x59:
        st['colors'][reg_id - 0x50] = data
    elif (reg_id & 0x0F) >= 0xA and (reg_id >> 4) <= 4:
        n = reg_id >> 4
        L = st['lists'][n]
        low = reg_id & 0x0F
        if low in LIST_FIELDS:
            if chinese(st) and n == st['list_sel'] and low <= 0xD:
                _setpoint_change(st, t)
            L[LIST_FIELDS[low]] = data
        else:                                      # 0xnF cut-off time: low byte value, high byte field
            unit = data >> 8
            if unit <= 2:
                L[('cut_s', 'cut_m', 'cut_h')[unit]] = lo
    output(st, t)                                  # a threshold below the present value trips at once
    return bytes([P.ACK])


def receive(burst, t_wall, pending_reply_at):
    """Bytes from the host -> list of (deliver_at_wall_time, bytes); pending_reply_at: wall time of a reply already
    scheduled by this port (several queries within one tick get one reply) or None."""
    st = load()
    off = st.get('clock_offset', 0.0)
    out = []
    entry = {'t': round(t_wall + off, 3), 'rx': bytes(burst).hex(' '), 'frames': []}
    if not st['powered']:
        entry['note'] = 'supply off'
    else:
        for k, b in enumerate(burst):
            tb = t_wall + k * BYTE_TIME
            if st['rx_state'] == 'collect' and tb - st['rx_t_a5'] > PARSER_TIMEOUT:
                st['rx_state'], st['rx_buf'] = 'hunt', []          # TIM2 timeout: partial frame dropped
            if st['rx_state'] == 'hunt':
                if b == P.FRAME_HEAD:
                    st['rx_state'], st['rx_buf'], st['rx_t_a5'] = 'collect', [], tb
                continue
            st['rx_buf'].append(b)
            if len(st['rx_buf']) < 5:
                continue
            buf = st['rx_buf']
            st['rx_state'], st['rx_buf'] = 'hunt', []
            if buf[3] != (buf[0] + buf[1] + buf[2]) & 0xFF or buf[4] != P.FRAME_TAIL:
                entry['frames'].append({'id': buf[0], 'dropped': 'checksum/tail'})
                continue
            reg_id, data = buf[0], buf[1] | (buf[2] << 8)
            st['frames'] += 1
            ans = apply_frame(st, reg_id, data, tb + off)
            entry['frames'].append({'id': reg_id, 'data': data})
            if reg_id == P.QUERY_ID:
                scale = latency_scale()
                tick = (math.floor((tb + off) / TICK) + 1) * TICK - off
                at = tb + (tick - tb) * scale + REPLY_TX * scale
                if pending_reply_at is None or at > pending_reply_at + 1e-9 and \
                        math.floor((pending_reply_at + off) / TICK) != math.floor((at + off) / TICK):
                    t_reply = at + off
                    out.append((at, ('reply', t_reply)))
                    pending_reply_at = at
            else:
                out.append((tb + ACK_LATENCY * latency_scale(), ans))
    st['audit'] = (st.get('audit') or [])[-(AUDIT_MAX - 1):] + [entry]
    save(st)
    return out


def materialize(item):
    """A scheduled answer -> bytes (a status reply is computed at its send time)."""
    if isinstance(item, tuple) and item[0] == 'reply':
        st = load()
        data = reply(st, item[1])
        save(st)
        return data
    return item


class SimSerial:
    """The subset of serial.Serial used by transport.Session."""

    def __init__(self):
        self.is_open = True
        self._rx = []                 # (deliver_at, bytes or ('reply', t))
        self._buf = b''

    def _pump(self):
        t = time.time()
        due = [x for x in self._rx if x[0] <= t]
        self._rx = [x for x in self._rx if x[0] > t]
        for _, item in sorted(due, key=lambda x: x[0]):
            self._buf += materialize(item)

    @property
    def in_waiting(self):
        self._pump()
        return len(self._buf)

    def read(self, n=1):
        self._pump()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def reset_input_buffer(self):
        self._pump()
        self._buf = b''

    def write(self, data):
        pend = max((at for at, item in self._rx if isinstance(item, tuple)), default=None)
        self._rx.extend(receive(bytes(data), time.time(), pend))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


# ------------------------------------------------------------------------------------------------ control CLI

def _coerce(v):
    for f in (int, float):
        try:
            return f(v)
        except ValueError:
            pass
    if v.lower() in ('true', 'false'):
        return v.lower() == 'true'
    if v.lower() in ('none', 'null'):
        return None
    return v


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else 'show'
    if cmd == 'reset':
        save(default_state())
        print(json.dumps({'ok': True, 'sim': 'reset', 'file': state_file()}))
    elif cmd == 'show':
        st = load()
        st.pop('audit', None)
        t = now(st)
        v, i, cc, on = output(st, t)
        st['now'] = {'vout': round(v, 4), 'iout': round(i, 4), 'cc': cc, 'on': on}
        print(json.dumps(st, indent=1))
    elif cmd == 'set':
        st = load()
        for kv in argv[1:]:
            k, _, v = kv.partition('=')
            if k not in st:
                print(json.dumps({'ok': False, 'error': 'unknown sim key %s' % k}))
                return 2
            st[k] = _coerce(v)
        save(st)
        print(json.dumps({'ok': True}))
    elif cmd == 'advance':
        st = load()
        st['clock_offset'] = st.get('clock_offset', 0.0) + float(argv[1])
        save(st)
        print(json.dumps({'ok': True, 'clock_offset': st['clock_offset']}))
    elif cmd == 'press':
        st = load()
        was = st['alarm']
        st['alarm'] = None
        save(st)
        print(json.dumps({'ok': True, 'cleared': was}))
    elif cmd == 'audit':
        st = load()
        print(json.dumps(st.get('audit', []), indent=0))
        if '--clear' in argv:
            st['audit'] = []
            save(st)
    else:
        print('usage: python -m openaxpp300s.sim reset | show | set key=value ... | advance SECONDS | press | '
              'audit [--clear]')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
