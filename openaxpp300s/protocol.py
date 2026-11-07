"""AX PP300S serial protocol: frames, register table, value conversion and the status reply. Pure functions, no I/O.

Link: USB serial (CH340) on the supply's second Type-C port ("TypeC serial"), 115200 8N1.

Host -> supply, 6 bytes:  A5  ID  dataLo  dataHi  (ID + dataLo + dataHi) & 0xFF  5A
Supply -> host:           01                              to every valid setting frame (any ID except FF)
                          A5 + 12 data bytes + sum + 5A   15 bytes, answer to ID 0xFF (status query)

The firmware parses byte by byte: it waits for A5, then takes the next 5 bytes whatever they are, and accepts them
if the checksum and the 5A tail match; a frame not complete 10 ms after its A5 is dropped. So a stray A5 before a
frame swallows it, and nothing is ever answered for a bad frame. It checks no ranges at all: every valid frame is
acknowledged and stored as sent, so the ranges below (the front-panel limits) are enforced here. See
docs/PROTOCOL.md.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

FRAME_HEAD = 0xA5
FRAME_TAIL = 0x5A
ACK = 0x01
QUERY_ID = 0xFF
FRAME_LEN = 6
REPLY_LEN = 15
BAUD = 115200


class ProtocolError(ValueError):
    """A frame or reply that does not follow the protocol (bad length, header, tail or checksum)."""


def checksum(reg_id, lo, hi):
    return (reg_id + lo + hi) & 0xFF


def encode(reg_id, data):
    """6-byte frame for register reg_id (0..255) with a 16-bit data word."""
    if not 0 <= int(reg_id) <= 0xFF:
        raise ValueError('register id %r out of 0..255' % (reg_id,))
    if not 0 <= int(data) <= 0xFFFF:
        raise ValueError('data %r out of 0..65535' % (data,))
    reg_id, data = int(reg_id), int(data)
    lo, hi = data & 0xFF, data >> 8
    return bytes((FRAME_HEAD, reg_id, lo, hi, checksum(reg_id, lo, hi), FRAME_TAIL))


def decode_frame(b):
    """6-byte frame -> (reg_id, data); ProtocolError when it is not a valid frame."""
    b = bytes(b)
    if len(b) != FRAME_LEN:
        raise ProtocolError('frame must be %d bytes, got %d' % (FRAME_LEN, len(b)))
    if b[0] != FRAME_HEAD or b[5] != FRAME_TAIL:
        raise ProtocolError('bad frame head/tail: %s' % b.hex(' '))
    if checksum(b[1], b[2], b[3]) != b[4]:
        raise ProtocolError('bad frame checksum: %s' % b.hex(' '))
    return b[1], b[2] | (b[3] << 8)


def query_frame():
    return encode(QUERY_ID, 0)


def reply_checksum(b):
    return sum(b[1:13]) & 0xFF


def decode_reply(b):
    """15-byte status reply -> dict (SI units). ProtocolError for a bad length, header, tail or checksum.

    Fields: vout_v, iout_a, pout_w, vin_v, temp_c, output_on, mode, cc_flag (byte 11), output_flag (byte 12),
    raw.
    mode is 'OFF' when the output is off, else 'CC' or 'CV' from byte 11. Byte 11 is the supply's CC indicator;
    it is also 1 for about a second after the output switches on (soft start) and is not meaningful while the
    output is off (seen 0 and 1), so mode reports OFF then."""
    b = bytes(b)
    if len(b) != REPLY_LEN:
        raise ProtocolError('status reply must be %d bytes, got %d: %s' % (REPLY_LEN, len(b), b.hex(' ')))
    if b[0] != FRAME_HEAD or b[14] != FRAME_TAIL:
        raise ProtocolError('bad status reply head/tail: %s' % b.hex(' '))
    if reply_checksum(b) != b[13]:
        raise ProtocolError('bad status reply checksum (got %02x, want %02x): %s'
                            % (b[13], reply_checksum(b), b.hex(' ')))
    u16 = lambda i: b[i] | (b[i + 1] << 8)                                    # noqa: E731
    s16 = lambda i: u16(i) - 0x10000 if u16(i) & 0x8000 else u16(i)           # noqa: E731
    on = b[12] != 0
    cc = b[11] != 0
    return {
        'vout_v': u16(1) / 1000.0,
        'iout_a': u16(3) / 1000.0,
        'pout_w': u16(5) / 100.0,
        'vin_v': u16(7) / 100.0,
        'temp_c': s16(9) / 10.0,
        'output_on': on,
        'mode': ('CC' if cc else 'CV') if on else 'OFF',
        'cc_flag': b[11],
        'output_flag': b[12],
        'raw': b.hex(' '),
    }


def build_reply(vout_mv, iout_ma, pout_10mw, vin_10mv, temp_dc, cc, on):
    """15-byte status reply (used by the simulator and tests)."""
    def w(v):
        v = int(v) & 0xFFFF
        return [v & 0xFF, v >> 8]
    body = [FRAME_HEAD] + w(vout_mv) + w(iout_ma) + w(pout_10mw) + w(vin_10mv) + w(temp_dc) + [int(cc) & 0xFF,
                                                                                               int(on) & 0xFF]
    body.append(sum(body[1:13]) & 0xFF)
    body.append(FRAME_TAIL)
    return bytes(body)


# ------------------------------------------------------------------------------------------------ registers

@dataclass(frozen=True)
class Register:
    """One settable register. value <-> raw data word conversion is by kind:
    centi  value in V or A, raw = value * 100 (10 mV / 10 mA steps)
    int    raw = value (seconds, index, 0/1)
    opp    watts 10..310 in 10 W steps, raw = (W - 10) / 10
    otp    deg C 60..100 in 10 C steps, raw = C / 10
    enum   value is one of choices (raw = its index)
    rgb565 16-bit colour (raw = the RGB565 word)
    cut    one field of the cut-off time (low byte value, high byte unit 0 s / 1 min / 2 h)"""
    key: str
    reg_id: int
    name: str
    kind: str
    unit: str
    raw_min: int
    raw_max: int
    screen: str               # 'digital' (Digital/Waveform screens), 'chinese', 'any'
    doc: str
    choices: tuple = ()
    source: str = 'pdf'       # 'pdf' = vendor protocol PDF, 'app' = vendor PC app V1.2 (firmware >= 1.4.2/1.4.4)
    group: int | None = None  # parameter list number for the Chinese-screen registers
    limit: str | None = None  # 'voltage' / 'current': checked against the bench limits

    @property
    def value_min(self):
        return raw_to_value(self, self.raw_min)

    @property
    def value_max(self):
        return raw_to_value(self, self.raw_max)

    def describe(self):
        d = {'key': self.key, 'id': '0x%02X' % self.reg_id, 'name': self.name, 'unit': self.unit,
             'min': self.value_min, 'max': self.value_max, 'raw_min': self.raw_min, 'raw_max': self.raw_max,
             'screen': self.screen, 'source': self.source, 'doc': self.doc}
        if self.choices:
            d['choices'] = list(self.choices)
        if self.group is not None:
            d['list'] = self.group
        return d


COLOR_DEFAULTS = {          # RGB565, as the vendor app's "restore defaults" sends them
    'vout': 0xFFE0, 'iout': 0x07E0, 'pout': 0x7FFF, 'aux_back': 0x07E0, 'aux_font': 0x0000,
    'bar_back': 0x07E0, 'bar_label': 0x0000, 'bar_value': 0xFFFF, 'bar_onoff': 0x07E0, 'bar_select': 0xFFFF,
}
COLOR_DOCS = {
    'vout': 'output voltage digits', 'iout': 'output current digits', 'pout': 'output power digits',
    'aux_back': 'auxiliary boxes (VIN/STATE/TEMP, Time/C/E) background', 'aux_font': 'auxiliary boxes text',
    'bar_back': 'setting bar (V-SET ... DATA) label background', 'bar_label': 'setting bar labels',
    'bar_value': 'setting bar values', 'bar_onoff': 'ON/OFF indicator', 'bar_select': 'selected setting box',
}
CUT_UNITS = ('s', 'min', 'h')
CUT_FIELD_MAX = {'s': 60, 'min': 60, 'h': 99}     # front-panel limits; 99:60:60 is the factory default (= off)
CUT_OFF = {'h': 99, 'min': 60, 's': 60}


def _registers():
    regs = [
        Register('voltage', 0x00, 'VSET', 'centi', 'V', 0, 3000, 'digital', 'output voltage setpoint',
                 limit='voltage'),
        Register('current', 0x01, 'ISET', 'centi', 'A', 1, 1010, 'digital', 'output current limit',
                 limit='current'),
        Register('ovp', 0x02, 'OVP', 'centi', 'V', 50, 3050, 'digital', 'over-voltage protection threshold (trips '
                 'when the measured output reaches it, output on or off; 0 would trip at once)', limit='voltage'),
        Register('ocp', 0x03, 'OCP', 'centi', 'A', 10, 1050, 'digital', 'over-current protection threshold (trips '
                 'after 100 ms at or above it)', limit='current'),
        Register('delay', 0x04, 'DELAY', 'int', 's', 0, 9999, 'digital',
                 'delayed output: after ON the output switches on DELAY s later (0 = off)'),
        Register('rev', 0x05, 'REV', 'int', 's', 0, 9999, 'digital',
                 'output toggling: after ON the output switches on after REV s, then toggles every REV s '
                 '(0 = off)'),
        Register('opp', 0x06, 'OPP', 'opp', 'W', 0, 30, 'any', 'over-power protection, 10..310 W in 10 W steps'),
        Register('otp', 0x07, 'OTP', 'otp', 'C', 6, 10, 'any', 'over-temperature protection, 60..100 C in 10 C '
                                                               'steps'),
        Register('output', 0x08, 'ON/OFF', 'int', '', 0, 1, 'any', 'output switch (1 = on, 0 = off)'),
        Register('list', 0x09, 'PARAM_LIST', 'int', '', 0, 4, 'chinese',
                 'Chinese screen: select parameter list 0..4'),
        Register('priority', 0x10, 'CVCC_PRIORITY', 'enum', '', 0, 1, 'any',
                 'CV/CC priority ("CC First" in the settings menu): cv = constant-voltage first, cc = '
                 'constant-current first', choices=('cv', 'cc'), source='app'),
    ]
    for n in range(5):
        base = 0x0A + 0x10 * n
        regs += [
            Register('list%d_voltage' % n, base + 0, 'L%d_VSET' % n, 'centi', 'V', 0, 3000, 'chinese',
                     'parameter list %d: output voltage' % n, group=n, limit='voltage'),
            Register('list%d_current' % n, base + 1, 'L%d_ISET' % n, 'centi', 'A', 1, 1010, 'chinese',
                     'parameter list %d: current limit' % n, group=n, limit='current'),
            Register('list%d_ovp' % n, base + 2, 'L%d_OVP' % n, 'centi', 'V', 50, 3050, 'chinese',
                     'parameter list %d: over-voltage protection' % n, group=n, limit='voltage'),
            Register('list%d_ocp' % n, base + 3, 'L%d_OCP' % n, 'centi', 'A', 10, 1050, 'chinese',
                     'parameter list %d: over-current protection' % n, group=n, limit='current'),
            Register('list%d_cut_current' % n, base + 4, 'L%d_CUT_I' % n, 'centi', 'A', 0, 1000, 'chinese',
                     'parameter list %d: cut-off current (the output switches off after the current stays '
                     'below it for about 6 s; 0 = off)' % n, group=n),
            Register('list%d_cut_time' % n, base + 5, 'L%d_CUT_T' % n, 'cut', '', 0, 0x0263, 'chinese',
                     'parameter list %d: cut-off time H:M:S, one field per frame: low byte the value, high byte the '
                     'field (0 seconds 0..60, 1 minutes 0..60, 2 hours 0..99); the output switches off when the '
                     'accumulated on-time reaches it; 99:60:60 = off, 0:0:0 = at once' % n, group=n),
        ]
    for i, name in enumerate(COLOR_DEFAULTS):
        regs.append(Register('color_' + name, 0x50 + i, 'COLOR_' + name.upper(), 'rgb565', 'RGB565', 0, 0xFFFF,
                             'any', 'display colour: %s (saved in EEPROM)' % COLOR_DOCS[name], source='app'))
    return regs


REGISTERS = _registers()
BY_KEY = {r.key: r for r in REGISTERS}
BY_ID = {r.reg_id: r for r in REGISTERS}
KNOWN_IDS = frozenset(BY_ID) | {QUERY_ID}


def register(key_or_id):
    if isinstance(key_or_id, int):
        if key_or_id not in BY_ID:
            raise KeyError('unknown register id 0x%02X' % key_or_id)
        return BY_ID[key_or_id]
    return BY_KEY[key_or_id]


# ------------------------------------------------------------------------------------------------ conversion

def _round_half_up(x):
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def value_to_raw(reg, value):
    """Physical value -> raw data word, ValueError when it is out of range or not on the register's grid.
    centi values are rounded to the nearest 10 mV / 10 mA (the result says what was really sent)."""
    k = reg.kind
    if k in ('centi', 'int', 'opp', 'otp', 'cut') and not isinstance(value, bool):
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError('%s: not a number: %r' % (reg.key, value)) from None
        if not math.isfinite(f):
            raise ValueError('%s: not a finite number: %r' % (reg.key, value))
    if k == 'centi':
        v = float(value)
        raw = _round_half_up(v * 100.0 + 1e-9 * (1 if v >= 0 else -1))
    elif k == 'int':
        if isinstance(value, bool):
            raw = int(value)
        else:
            f = float(value)
            if f != int(f):
                raise ValueError('%s must be a whole number, got %r' % (reg.key, value))
            raw = int(f)
    elif k == 'opp':
        w = float(value)
        if w != int(w) or int(w) % 10:
            raise ValueError('OPP must be a multiple of 10 W (10..310), got %r' % value)
        raw = (int(w) - 10) // 10
    elif k == 'otp':
        c = float(value)
        if c != int(c) or int(c) % 10:
            raise ValueError('OTP must be 60, 70, 80, 90 or 100 C, got %r' % value)
        raw = int(c) // 10
    elif k == 'enum':
        s = str(value).strip().lower()
        if s not in reg.choices:
            raise ValueError('%s must be one of %s, got %r' % (reg.key, ', '.join(reg.choices), value))
        raw = reg.choices.index(s)
    elif k == 'rgb565':
        raw = parse_color(value)
    elif k == 'cut':
        f = float(value)
        if f != int(f):
            raise ValueError('%s must be a whole number, got %r' % (reg.key, value))
        raw = int(f)
        unit, lo = raw >> 8, raw & 0xFF
        if raw < 0 or unit > 2 or lo > CUT_FIELD_MAX[CUT_UNITS[unit]]:
            raise ValueError('%s word 0x%04X: field %d value %d out of range (s 0..60, min 0..60, h 0..99)'
                             % (reg.key, raw & 0xFFFF, unit, lo))
    else:                                                    # pragma: no cover
        raise ValueError('unknown kind %s' % k)
    if not reg.raw_min <= raw <= reg.raw_max:
        raise ValueError('%s %s out of range %s..%s %s' % (
            reg.key, _fmt(value), _fmt(reg.value_min), _fmt(reg.value_max), reg.unit))
    return raw


def raw_to_value(reg, raw):
    k = reg.kind
    if k == 'centi':
        return round(raw / 100.0, 2)
    if k == 'opp':
        return raw * 10 + 10
    if k == 'otp':
        return raw * 10
    if k == 'enum':
        return reg.choices[raw] if 0 <= raw < len(reg.choices) else raw
    if k == 'rgb565':
        return '0x%04X' % raw
    return raw


def _fmt(v):
    if isinstance(v, float):
        return ('%.3f' % v).rstrip('0').rstrip('.')
    return str(v)


# ------------------------------------------------------------------------------------------------ cut-off time

_CUT_RE = re.compile(r'^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s?)?\s*$', re.I)


def parse_cut_time(text):
    """'1h30m', '45m', '50s', '2h0m10s', 'H:M:S', 'off' (99:60:60) -> {'h', 'min', 's'} (only the fields given)."""
    s = str(text).strip().lower()
    if not s:
        raise ValueError('empty cut-off time')
    if s in ('off', 'none', 'disable', 'disabled'):
        return dict(CUT_OFF)
    if ':' in s:
        parts = s.split(':')
        if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
            raise ValueError('cut-off time H:M:S needs three whole numbers, got %r' % text)
        fields = {'h': int(parts[0]), 'min': int(parts[1]), 's': int(parts[2])}
    else:
        m = _CUT_RE.match(s)
        if not m or not any(m.groups()):
            raise ValueError('cannot parse cut-off time %r (use e.g. 1h30m, 45m, 90s or H:M:S)' % text)
        fields = {}
        for key, g in zip(('h', 'min', 's'), m.groups()):
            if g is not None:
                fields[key] = int(g)
    for k, v in fields.items():
        if not 0 <= v <= CUT_FIELD_MAX[k]:
            raise ValueError('cut-off time %s field %d out of 0..%d' % (k, v, CUT_FIELD_MAX[k]))
    return fields


def cut_raw(unit, value):
    if unit not in CUT_UNITS:
        raise ValueError('cut-off time unit must be one of %s' % ', '.join(CUT_UNITS))
    if not 0 <= int(value) <= CUT_FIELD_MAX[unit]:
        raise ValueError('cut-off time %s field %d out of 0..%d' % (unit, value, CUT_FIELD_MAX[unit]))
    return int(value) | (CUT_UNITS.index(unit) << 8)


# ------------------------------------------------------------------------------------------------ colours

NAMED_COLORS = {
    'black': 0x0000, 'white': 0xFFFF, 'red': 0xF800, 'green': 0x07E0, 'blue': 0x001F, 'yellow': 0xFFE0,
    'cyan': 0x07FF, 'magenta': 0xF81F, 'orange': 0xFD20, 'gray': 0x8410, 'grey': 0x8410,
}


def rgb_to_565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def rgb565_to_hex(v):
    r, g, b = ((v >> 11) & 0x1F) << 3, ((v >> 5) & 0x3F) << 2, (v & 0x1F) << 3
    return '#%02X%02X%02X' % (r, g, b)


def parse_color(text):
    """'#RRGGBB' / 'RRGGBB' (24-bit, converted like the vendor app), '0xF800' (raw RGB565), a name, or an int."""
    if isinstance(text, int) and not isinstance(text, bool):
        if not 0 <= text <= 0xFFFF:
            raise ValueError('RGB565 value out of 0..0xFFFF')
        return text
    s = str(text).strip().lower()
    if s in NAMED_COLORS:
        return NAMED_COLORS[s]
    if s.startswith('0x'):
        v = int(s, 16)
        if not 0 <= v <= 0xFFFF:
            raise ValueError('RGB565 value out of 0..0xFFFF: %s' % text)
        return v
    h = s[1:] if s.startswith('#') else s
    if re.fullmatch(r'[0-9a-f]{6}', h):
        return rgb_to_565(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    raise ValueError('cannot parse colour %r (use #RRGGBB, 0xRGB565 or a name: %s)'
                     % (text, ', '.join(sorted(NAMED_COLORS))))
