"""Pure functions of openaxpp300s.protocol: frames, the status reply, value conversion, colours, cut-off time and the
register table. No I/O, no simulator."""
import json
import math

import pytest

from openaxpp300s import protocol as P

VENDOR_REPLY = 'A5 88 13 00 00 00 00 B4 07 25 01 00 01 7D 5A'     # PDF: 5 V, 0 A, 0 W, 19.72 V in, 29.3 C, CV, on
REAL_OFF_REPLY = 'a5 00 00 00 00 00 00 bd 07 1b 01 01 00 e1 5a'   # real supply, output off (byte 11 reads 1)


def H(s):
    return bytes.fromhex(s)


def R(key):
    return P.BY_KEY[key]


# ------------------------------------------------------------------------------------------------ constants

def test_link_constants():
    assert P.BAUD == 115200
    assert (P.FRAME_HEAD, P.FRAME_TAIL, P.ACK, P.QUERY_ID) == (0xA5, 0x5A, 0x01, 0xFF)
    assert (P.FRAME_LEN, P.REPLY_LEN) == (6, 15)
    assert issubclass(P.ProtocolError, ValueError)


# ------------------------------------------------------------------------------------------------ frames

@pytest.mark.parametrize('reg_id,data,frame', [
    (0x00, 1000, 'A5 00 E8 03 EB 5A'),       # PDF: VSET 10 V
    (0x08, 1, 'A5 08 01 00 09 5A'),          # PDF: output on
    (0x0F, 0x0105, 'A5 0F 05 01 15 5A'),     # PDF: list 0 cut-off minutes = 5
    (0x1F, 0x0205, 'A5 1F 05 02 26 5A'),     # PDF: list 1 cut-off hours = 5
    (0xFF, 0, 'A5 FF 00 00 FF 5A'),          # status query
    (0xFF, 0xFFFF, 'A5 FF FF FF FD 5A'),     # checksum wraps modulo 256
], ids=['vset10', 'on', 'l0_cut_min5', 'l1_cut_h5', 'query', 'wrap'])
def test_encode_and_decode_frame(reg_id, data, frame):
    assert P.encode(reg_id, data) == H(frame)
    assert P.decode_frame(H(frame)) == (reg_id, data)


def test_vendor_examples_from_values():
    """The PDF examples built from physical values the way the CLI builds them."""
    v = R('voltage')
    assert P.encode(v.reg_id, P.value_to_raw(v, 10)) == H('A5 00 E8 03 EB 5A')
    o = R('output')
    assert P.encode(o.reg_id, P.value_to_raw(o, 1)) == H('A5 08 01 00 09 5A')
    c0 = R('list0_cut_time')
    assert P.encode(c0.reg_id, P.cut_raw('min', P.parse_cut_time('5m')['min'])) == H('A5 0F 05 01 15 5A')
    c1 = R('list1_cut_time')
    assert P.encode(c1.reg_id, P.cut_raw('h', P.parse_cut_time('5h')['h'])) == H('A5 1F 05 02 26 5A')


def test_query_frame():
    assert P.query_frame() == H('A5 FF 00 00 FF 5A')


def test_checksum():
    assert P.checksum(0x00, 0xE8, 0x03) == 0xEB
    assert P.checksum(0xFF, 0xFF, 0xFF) == 0xFD


def test_encode_decode_roundtrip():
    for reg_id in (0x00, 0x01, 0x0A, 0x10, 0x4F, 0x59, 0xA5, 0x5A, 0xFE, 0xFF):
        for data in (0, 1, 0xA5, 0x5A, 0x00FF, 0x0100, 0x5AA5, 0xA55A, 0xFFFF):
            f = P.encode(reg_id, data)
            assert len(f) == 6 and f[0] == 0xA5 and f[5] == 0x5A
            assert f[2] | (f[3] << 8) == data                       # little-endian data word
            assert P.decode_frame(f) == (reg_id, data)


@pytest.mark.parametrize('reg_id,data', [(-1, 0), (0x100, 0), (0, -1), (0, 0x10000)])
def test_encode_out_of_range(reg_id, data):
    with pytest.raises(ValueError):
        P.encode(reg_id, data)


@pytest.mark.parametrize('frame', [
    '', 'A5 00 E8 03 EB', 'A5 00 E8 03 EB 5A 00',           # length
    'A6 00 E8 03 EB 5A', '00 00 E8 03 EB 5A',               # head
    'A5 00 E8 03 EB 5B', 'A5 00 E8 03 EB 00',               # tail
    'A5 00 E8 03 EC 5A', 'A5 01 E8 03 EB 5A',               # checksum
], ids=['empty', 'short', 'long', 'head', 'head0', 'tail', 'tail0', 'sum', 'sum_id'])
def test_decode_frame_errors(frame):
    with pytest.raises(P.ProtocolError):
        P.decode_frame(H(frame))


# ------------------------------------------------------------------------------------------------ status reply

def test_decode_vendor_reply():
    d = P.decode_reply(H(VENDOR_REPLY))
    assert d['vout_v'] == pytest.approx(5.0)
    assert d['iout_a'] == 0.0 and d['pout_w'] == 0.0
    assert d['vin_v'] == pytest.approx(19.72)
    assert d['temp_c'] == pytest.approx(29.3)
    assert d['cc_flag'] == 0 and d['output_flag'] == 1
    assert d['output_on'] is True and d['mode'] == 'CV'
    assert d['raw'] == VENDOR_REPLY.lower()


def test_decode_real_reply_output_off():
    d = P.decode_reply(H(REAL_OFF_REPLY))
    assert d['vout_v'] == 0.0 and d['iout_a'] == 0.0 and d['pout_w'] == 0.0
    assert d['vin_v'] == pytest.approx(19.81)
    assert d['temp_c'] == pytest.approx(28.3)
    assert d['cc_flag'] == 1 and d['output_flag'] == 0
    assert d['output_on'] is False
    assert d['mode'] == 'OFF'                 # byte 11 reads 1 with the output off: not CC
    assert d['raw'] == REAL_OFF_REPLY


def test_reply_checksum():
    assert P.reply_checksum(H(VENDOR_REPLY)) == 0x7D
    assert P.reply_checksum(H(REAL_OFF_REPLY)) == 0xE1


def test_build_reply_reproduces_examples():
    assert P.build_reply(5000, 0, 0, 1972, 293, 0, 1) == H(VENDOR_REPLY)
    assert P.build_reply(0, 0, 0, 1981, 283, 1, 0) == H(REAL_OFF_REPLY)


def test_decode_cc_mode_and_units():
    d = P.decode_reply(P.build_reply(12000, 2500, 3000, 1980, 300, 1, 1))
    assert d['mode'] == 'CC' and d['output_on'] is True
    assert d['vout_v'] == pytest.approx(12.0)
    assert d['iout_a'] == pytest.approx(2.5)
    assert d['pout_w'] == pytest.approx(30.0)          # 10 mW units
    assert d['vin_v'] == pytest.approx(19.8)
    assert d['temp_c'] == pytest.approx(30.0)


def test_decode_soft_start_is_cc_flag_with_output_on():
    d = P.decode_reply(P.build_reply(11, 0, 0, 1980, 285, 1, 1))
    assert d['output_on'] is True and d['cc_flag'] == 1 and d['mode'] == 'CC'


def test_decode_negative_temperature_is_signed():
    assert P.decode_reply(P.build_reply(0, 0, 0, 1980, -55, 1, 0))['temp_c'] == pytest.approx(-5.5)


def test_decode_full_scale_words():
    d = P.decode_reply(P.build_reply(30000, 10100, 30300, 3200, 999, 0, 1))
    assert d['vout_v'] == pytest.approx(30.0) and d['iout_a'] == pytest.approx(10.1)
    assert d['pout_w'] == pytest.approx(303.0) and d['vin_v'] == pytest.approx(32.0)
    assert P.decode_reply(P.build_reply(0xFFFF, 0, 0, 0, 0, 0, 1))['vout_v'] == pytest.approx(65.535)


def test_decode_reply_accepts_bytearray_and_list():
    b = H(VENDOR_REPLY)
    assert P.decode_reply(bytearray(b)) == P.decode_reply(b) == P.decode_reply(list(b))


def _corrupt(s, i, xor=0x01):
    b = bytearray(H(s))
    b[i] ^= xor
    return bytes(b)


@pytest.mark.parametrize('reply', [
    b'',
    H(VENDOR_REPLY)[:14],
    H(VENDOR_REPLY) + b'\x00',
    H('A5 00 E8 03 EB 5A'),                       # a setting frame is not a reply
    _corrupt(VENDOR_REPLY, 0),                    # head
    _corrupt(VENDOR_REPLY, 14),                   # tail
    _corrupt(VENDOR_REPLY, 13),                   # checksum byte
    _corrupt(VENDOR_REPLY, 1),                    # data byte (checksum no longer matches)
    _corrupt(REAL_OFF_REPLY, 12),                 # output flag flipped
], ids=['empty', 'short', 'long', 'frame', 'head', 'tail', 'sum', 'data', 'flag'])
def test_decode_reply_errors(reply):
    with pytest.raises(P.ProtocolError):
        P.decode_reply(reply)


# ------------------------------------------------------------------------------------------------ centi (V, A)

@pytest.mark.parametrize('value,raw', [
    (10, 1000), ('5', 500), (' 12.5 ', 1250), (0, 0), (0.01, 1),
    (1.234, 123), (1.235, 124), (1.005, 101), (2.675, 268), (12.345, 1235), (0.29, 29), (4.35, 435),
    (0.004, 0), (0.005, 1), (29.995, 3000), (30.004, 3000), (30, 3000),
])
def test_centi_rounds_half_up_to_10mv(value, raw):
    assert P.value_to_raw(R('voltage'), value) == raw


@pytest.mark.parametrize('key,min_value,raw_min,max_value,raw_max', [
    # front-panel limits: ISET >= 0.01 A, OVP >= 0.50 V, OCP >= 0.10 A (the same for the parameter lists)
    ('voltage', 0.0, 0, 30.0, 3000), ('current', 0.01, 1, 10.1, 1010), ('ovp', 0.5, 50, 30.5, 3050),
    ('ocp', 0.1, 10, 10.5, 1050),
    ('list0_voltage', 0.0, 0, 30.0, 3000), ('list4_current', 0.01, 1, 10.1, 1010), ('list2_ovp', 0.5, 50, 30.5, 3050),
    ('list3_ocp', 0.1, 10, 10.5, 1050), ('list1_cut_current', 0.0, 0, 10.0, 1000),
])
def test_centi_range_edges(key, min_value, raw_min, max_value, raw_max):
    reg = R(key)
    assert (reg.raw_min, reg.raw_max) == (raw_min, raw_max)
    assert reg.value_min == pytest.approx(min_value) and reg.value_max == pytest.approx(max_value)
    assert P.value_to_raw(reg, min_value) == raw_min
    assert P.value_to_raw(reg, min_value + 0.004) == raw_min          # rounds down onto the minimum
    assert P.value_to_raw(reg, max_value) == raw_max
    assert P.value_to_raw(reg, max_value + 0.004) == raw_max          # rounds down onto the maximum
    if raw_min:
        assert P.value_to_raw(reg, min_value - 0.004) == raw_min      # rounds up onto the minimum
        with pytest.raises(ValueError):
            P.value_to_raw(reg, 0)                                    # 0 is below the front-panel minimum
    else:
        assert P.value_to_raw(reg, 0) == 0
    for bad in (max_value + 0.005, max_value + 0.01, max_value + 1, min_value - 0.006, min_value - 0.01, -0.01,
                -1):
        with pytest.raises(ValueError):
            P.value_to_raw(reg, bad)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf'), 'abc', '', None])
def test_centi_rejects_non_numbers(bad):
    with pytest.raises((ValueError, TypeError)):
        P.value_to_raw(R('voltage'), bad)


def test_centi_raw_to_value():
    v = R('voltage')
    assert P.raw_to_value(v, 1000) == 10.0
    assert P.raw_to_value(v, 1) == 0.01
    assert P.raw_to_value(R('ovp'), 3050) == 30.5
    assert P.raw_to_value(R('current'), 1010) == 10.1


@pytest.mark.parametrize('key', ['voltage', 'current', 'ovp', 'ocp', 'list0_cut_current'])
def test_centi_roundtrip_every_raw(key):
    reg = R(key)
    for raw in range(reg.raw_min, reg.raw_max + 1):
        value = P.raw_to_value(reg, raw)
        assert value == round(raw / 100.0, 2)
        assert P.value_to_raw(reg, value) == raw
        assert P.value_to_raw(reg, '%.2f' % value) == raw


# ------------------------------------------------------------------------------------------------ int

@pytest.mark.parametrize('key,value,raw', [
    ('delay', 0, 0), ('delay', 9999, 9999), ('delay', 2.0, 2), ('delay', '7', 7), ('rev', 9999, 9999),
    ('output', 0, 0), ('output', 1, 1), ('output', True, 1), ('output', False, 0),
    ('list', 0, 0), ('list', 4, 4),
])
def test_int_values(key, value, raw):
    assert P.value_to_raw(R(key), value) == raw
    assert P.raw_to_value(R(key), raw) == raw


@pytest.mark.parametrize('key,value', [
    ('delay', 10000), ('delay', -1), ('delay', 1.5), ('rev', 10000),
    ('output', 2), ('output', -1), ('output', 0.5), ('list', 5), ('list', -1), ('delay', 'x'),
])
def test_int_rejects(key, value):
    with pytest.raises(ValueError):
        P.value_to_raw(R(key), value)


@pytest.mark.parametrize('key', ['delay', 'opp', 'otp'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf')], ids=['nan', 'inf'])
def test_whole_number_kinds_reject_nan_and_inf_with_valueerror(key, bad):
    """value_to_raw documents ValueError for bad values; Supply.convert only turns ValueError into INVALID_ARG, so
    any other exception type escapes as a crash (e.g. "delay": Infinity in a JSON sequence file or MCP call)."""
    with pytest.raises(ValueError):
        P.value_to_raw(R(key), bad)


# ------------------------------------------------------------------------------------------------ OPP / OTP grids

def test_opp_grid():
    reg = R('opp')
    assert (reg.value_min, reg.value_max) == (10, 310)
    for w in range(10, 311, 10):
        raw = P.value_to_raw(reg, w)
        assert raw == (w - 10) // 10
        assert P.raw_to_value(reg, raw) == w
    assert P.value_to_raw(reg, 100.0) == 9 and P.value_to_raw(reg, '100') == 9
    assert P.raw_to_value(reg, 0) == 10 and P.raw_to_value(reg, 30) == 310


@pytest.mark.parametrize('w', [0, 5, 15, 105, 315, 320, 330, -10, 100.5, 2560])
def test_opp_rejects_off_grid_and_out_of_range(w):
    with pytest.raises(ValueError):
        P.value_to_raw(R('opp'), w)


def test_otp_grid():
    reg = R('otp')
    assert (reg.value_min, reg.value_max) == (60, 100)
    for c, raw in ((60, 6), (70, 7), (80, 8), (90, 9), (100, 10)):
        assert P.value_to_raw(reg, c) == raw
        assert P.raw_to_value(reg, raw) == c
    assert P.value_to_raw(reg, 80.0) == 8


@pytest.mark.parametrize('c', [0, 10, 50, 55, 65, 110, 1000, -60])
def test_otp_rejects_off_grid_and_out_of_range(c):
    """OTP 0 would trip at once on the firmware (T >= OTP*10 C); it must never be produced."""
    with pytest.raises(ValueError):
        P.value_to_raw(R('otp'), c)


# ------------------------------------------------------------------------------------------------ enum

@pytest.mark.parametrize('value,raw', [('cv', 0), ('cc', 1), ('CV', 0), (' Cc ', 1)])
def test_enum_priority(value, raw):
    assert P.value_to_raw(R('priority'), value) == raw


@pytest.mark.parametrize('bad', ['', 'cc first', 'c', 'cvcc', 0, 1, None])
def test_enum_rejects(bad):
    with pytest.raises(ValueError):
        P.value_to_raw(R('priority'), bad)


def test_enum_raw_to_value():
    reg = R('priority')
    assert P.raw_to_value(reg, 0) == 'cv' and P.raw_to_value(reg, 1) == 'cc'
    assert P.raw_to_value(reg, 7) == 7                    # unknown raw stays a number
    assert (reg.value_min, reg.value_max) == ('cv', 'cc')


# ------------------------------------------------------------------------------------------------ colours

@pytest.mark.parametrize('text,raw', [
    ('#FF0000', 0xF800), ('#00FF00', 0x07E0), ('#0000FF', 0x001F), ('#FFFFFF', 0xFFFF), ('#000000', 0x0000),
    ('ffff00', 0xFFE0), ('#ffa500', 0xFD20), ('#808080', 0x8410), (' #FF0000 ', 0xF800),
    ('#07FF07', 0x07E0),                                  # low bits dropped like the vendor app
    ('0xF800', 0xF800), ('0X07e0', 0x07E0), ('0x0', 0x0000), ('0xFFFF', 0xFFFF), ('0x1F', 0x001F),
    ('red', 0xF800), ('Red', 0xF800), (' white ', 0xFFFF), ('grey', 0x8410), ('gray', 0x8410),
    (0x07E0, 0x07E0), (0, 0), (0xFFFF, 0xFFFF),
])
def test_parse_color(text, raw):
    assert P.parse_color(text) == raw


@pytest.mark.parametrize('bad', ['#FFF', '#GGGGGG', 'FFE0', '#FF00000', '0x10000', '0xZZ', '0x', 'purple', '',
                                 '-0x1', -1, 0x10000, True, False, None, 1.5])
def test_parse_color_rejects(bad):
    with pytest.raises(ValueError):
        P.parse_color(bad)


def test_named_colors_match_their_rgb():
    rgb = {'black': (0, 0, 0), 'white': (255, 255, 255), 'red': (255, 0, 0), 'green': (0, 255, 0),
           'blue': (0, 0, 255), 'yellow': (255, 255, 0), 'cyan': (0, 255, 255), 'magenta': (255, 0, 255),
           'orange': (255, 165, 0), 'gray': (128, 128, 128), 'grey': (128, 128, 128)}
    assert set(P.NAMED_COLORS) == set(rgb)
    for name, (r, g, b) in rgb.items():
        assert P.NAMED_COLORS[name] == P.rgb_to_565(r, g, b), name


def test_rgb565_hex_roundtrip_all_words():
    assert P.rgb565_to_hex(0xF800) == '#F80000'
    assert P.rgb565_to_hex(0x07E0) == '#00FC00'
    assert P.rgb565_to_hex(0xFFFF) == '#F8FCF8'
    assert P.rgb565_to_hex(0x0000) == '#000000'
    for v in range(0x10000):
        assert P.parse_color(P.rgb565_to_hex(v)) == v


def test_color_registers_value_roundtrip():
    reg = R('color_vout')
    assert P.value_to_raw(reg, '#FFFF00') == 0xFFE0
    assert P.value_to_raw(reg, 'red') == 0xF800
    assert P.value_to_raw(reg, 0x1234) == 0x1234
    assert P.raw_to_value(reg, 0x1234) == '0x1234'
    assert (reg.value_min, reg.value_max) == ('0x0000', '0xFFFF')
    for raw in (0, 1, 0x1F, 0x7E0, 0xF800, 0xA5A5, 0xFFFF):
        assert P.value_to_raw(reg, P.raw_to_value(reg, raw)) == raw
    for bad in ('0x10000', 'nope', -1):
        with pytest.raises(ValueError):
            P.value_to_raw(reg, bad)


def test_color_defaults():
    assert list(P.COLOR_DEFAULTS) == list(P.COLOR_DOCS)
    assert len(P.COLOR_DEFAULTS) == 10
    assert all(0 <= v <= 0xFFFF for v in P.COLOR_DEFAULTS.values())
    assert P.COLOR_DEFAULTS['vout'] == P.NAMED_COLORS['yellow']
    assert P.COLOR_DEFAULTS['iout'] == P.NAMED_COLORS['green']


# ------------------------------------------------------------------------------------------------ cut-off time

@pytest.mark.parametrize('text,fields', [
    ('1h30m', {'h': 1, 'min': 30}),
    ('45m', {'min': 45}),
    ('45min', {'min': 45}),
    ('50s', {'s': 50}),
    ('60s', {'s': 60}),
    ('45', {'s': 45}),
    ('0s', {'s': 0}),
    ('5h', {'h': 5}),
    ('2h0m10s', {'h': 2, 'min': 0, 's': 10}),
    ('1h 30m 10s', {'h': 1, 'min': 30, 's': 10}),
    (' 1H30M ', {'h': 1, 'min': 30}),
    ('99h60m60s', {'h': 99, 'min': 60, 's': 60}),
    ('61h', {'h': 61}),
    ('1:30:00', {'h': 1, 'min': 30, 's': 0}),
    ('0:0:5', {'h': 0, 'min': 0, 's': 5}),
    ('99:60:60', {'h': 99, 'min': 60, 's': 60}),
    ('61:0:0', {'h': 61, 'min': 0, 's': 0}),
    (' 1 : 2 : 3 ', {'h': 1, 'min': 2, 's': 3}),
    ('off', {'h': 99, 'min': 60, 's': 60}),       # 99:60:60 = the factory default (= no cut-off)
    (' OFF ', {'h': 99, 'min': 60, 's': 60}),
    ('none', {'h': 99, 'min': 60, 's': 60}),
    ('disabled', {'h': 99, 'min': 60, 's': 60}),
])
def test_parse_cut_time(text, fields):
    assert P.parse_cut_time(text) == fields


def test_parse_cut_time_off_is_a_copy():
    f = P.parse_cut_time('off')
    assert f == P.CUT_OFF and f is not P.CUT_OFF
    f['h'] = 1
    assert P.CUT_OFF == {'h': 99, 'min': 60, 's': 60}


@pytest.mark.parametrize('bad', [
    '', '   ', 'abc', 'h', '1d', '1.5h', '-5m', '5ms', '1h30x', 'm30', ':', '1:30', '1:2:3:4', '1:x:3', ' : : ',
    '100s', '99s', '90s', '61s', '90', '61m', '100h', '1:61:0', '0:0:61', '0:0:99', '0:0:100', '100:0:0',
    '1:-1:0',
])
def test_parse_cut_time_rejects(bad):
    with pytest.raises(ValueError):
        P.parse_cut_time(bad)


def test_cut_field_max():
    """Front-panel limits: h 0..99, min 0..60, s 0..60; 99:60:60 is the factory default (= off)."""
    assert P.CUT_UNITS == ('s', 'min', 'h')
    assert P.CUT_FIELD_MAX == {'s': 60, 'min': 60, 'h': 99}
    assert P.CUT_OFF == {'h': 99, 'min': 60, 's': 60}
    assert R('list0_cut_time').raw_max == P.cut_raw('h', 99) == 0x0263


@pytest.mark.parametrize('unit,value,raw', [
    ('s', 0, 0x0000), ('s', 60, 0x003C), ('min', 0, 0x0100), ('min', 5, 0x0105), ('min', 60, 0x013C),
    ('h', 5, 0x0205), ('h', 60, 0x023C), ('h', 61, 0x023D), ('h', 99, 0x0263),
])
def test_cut_raw(unit, value, raw):
    assert P.cut_raw(unit, value) == raw


@pytest.mark.parametrize('unit,value', [('s', 61), ('s', 99), ('s', 100), ('min', 61), ('h', 100), ('s', -1),
                                        ('h', 255), ('d', 1), ('m', 5), ('sec', 1), ('', 0)])
def test_cut_raw_rejects(unit, value):
    with pytest.raises(ValueError):
        P.cut_raw(unit, value)


def test_parse_cut_time_to_frames():
    f = P.parse_cut_time('1h30m')
    words = [P.cut_raw(u, f[u]) for u in ('h', 'min', 's') if u in f]
    assert words == [0x0201, 0x011E]
    reg = R('list2_cut_time')
    assert [P.encode(reg.reg_id, w).hex(' ') for w in words] == ['a5 2f 01 02 32 5a', 'a5 2f 1e 01 4e 5a']


def test_cut_kind_value_to_raw_accepts_cut_raw_words():
    reg = R('list0_cut_time')
    for unit in P.CUT_UNITS:
        for v in (0, 1, P.CUT_FIELD_MAX[unit]):
            assert P.value_to_raw(reg, P.cut_raw(unit, v)) == P.cut_raw(unit, v)
    for bad in (-1, 0x0300, 0x03FF, 0xFFFF):              # unit byte > 2 or negative
        with pytest.raises(ValueError):
            P.value_to_raw(reg, bad)


@pytest.mark.parametrize('word', [0x003D, 0x0063, 0x0064, 0x00FF, 0x013D, 0x01FF, 0x0264, 0x02FF],
                         ids=['61s', '99s', '100s', '255s', '61min', '255min', '100h', '255h'])
def test_cut_kind_value_to_raw_rejects_field_beyond_documented_max(word):
    """A cut-off time word whose field is outside CUT_FIELD_MAX (front panel: s 0-60, min 0-60, h 0-99) is
    out-of-range data: cut_raw() and parse_cut_time() refuse it, so value_to_raw() for the same register must refuse
    it too."""
    with pytest.raises(ValueError):
        P.value_to_raw(R('list0_cut_time'), word)


@pytest.mark.parametrize('word', [0x003C, 0x013C, 0x023D, 0x0263], ids=['60s', '60min', '61h', '99h'])
def test_cut_kind_value_to_raw_accepts_front_panel_max(word):
    assert P.value_to_raw(R('list3_cut_time'), word) == word


# ------------------------------------------------------------------------------------------------ every register

@pytest.mark.parametrize('reg', [r for r in P.REGISTERS if r.kind != 'cut'], ids=lambda r: r.key)
def test_every_register_range_edges(reg):
    assert P.value_to_raw(reg, reg.value_min) == reg.raw_min
    assert P.value_to_raw(reg, reg.value_max) == reg.raw_max
    with pytest.raises(ValueError):
        P.value_to_raw(reg, P.raw_to_value(reg, reg.raw_max + 1))
    with pytest.raises(ValueError):
        P.value_to_raw(reg, P.raw_to_value(reg, reg.raw_min - 1))


@pytest.mark.parametrize('reg', [r for r in P.REGISTERS if r.kind not in ('centi', 'rgb565', 'cut')],
                         ids=lambda r: r.key)
def test_small_registers_roundtrip_every_raw(reg):
    for raw in range(reg.raw_min, reg.raw_max + 1):
        assert P.value_to_raw(reg, P.raw_to_value(reg, raw)) == raw


# ------------------------------------------------------------------------------------------------ register table

KINDS = {'centi', 'int', 'opp', 'otp', 'enum', 'rgb565', 'cut'}


def test_register_table_unique_and_indexed():
    ids = [r.reg_id for r in P.REGISTERS]
    keys = [r.key for r in P.REGISTERS]
    names = [r.name for r in P.REGISTERS]
    assert len(set(ids)) == len(ids) == len(P.BY_ID)
    assert len(set(keys)) == len(keys) == len(P.BY_KEY)
    assert len(set(names)) == len(names)
    assert len(P.REGISTERS) == 10 + 1 + 5 * 6 + 10
    assert P.QUERY_ID not in P.BY_ID
    assert P.KNOWN_IDS == frozenset(ids) | {0xFF}
    for unused in (0x11, 0x19, 0x20, 0x29, 0x49, 0x4A + 0x10, 0x5A, 0x60, 0xFE):
        assert unused not in P.KNOWN_IDS


def test_register_fields_sane():
    for r in P.REGISTERS:
        assert r.kind in KINDS, r.key
        assert 0 <= r.reg_id <= 0xFE, r.key
        assert 0 <= r.raw_min <= r.raw_max <= 0xFFFF, r.key
        assert r.screen in ('digital', 'chinese', 'any'), r.key
        assert r.source in ('pdf', 'app'), r.key
        assert r.doc and r.name, r.key
        if r.kind == 'enum':
            assert len(r.choices) == r.raw_max + 1 and r.raw_min == 0
        else:
            assert r.choices == ()


def test_digital_registers():
    expect = ['voltage', 'current', 'ovp', 'ocp', 'delay', 'rev', 'opp', 'otp', 'output', 'list']
    assert [P.BY_ID[i].key for i in range(0x0A)] == expect
    for i in range(0x06):                          # act only on the Digital/Waveform screens
        assert P.BY_ID[i].screen == 'digital'
    for i in (0x06, 0x07, 0x08):
        assert P.BY_ID[i].screen == 'any'
    assert P.BY_ID[0x09].screen == 'chinese'
    assert all(P.BY_ID[i].group is None for i in range(0x0A))


def test_priority_register():
    r = P.BY_ID[0x10]
    assert r.key == 'priority' and r.kind == 'enum' and r.choices == ('cv', 'cc')
    assert (r.raw_min, r.raw_max) == (0, 1) and r.source == 'app' and r.screen == 'any'


def test_list_registers():
    roles = ['voltage', 'current', 'ovp', 'ocp', 'cut_current', 'cut_time']
    digital = {'voltage': 'voltage', 'current': 'current', 'ovp': 'ovp', 'ocp': 'ocp'}
    for n in range(5):
        for off, role in enumerate(roles):
            reg_id = (n << 4) | (0x0A + off)       # 0xnA .. 0xnF
            r = P.BY_ID[reg_id]
            assert r.key == 'list%d_%s' % (n, role)
            assert r.group == n and r.screen == 'chinese'
            if role in digital:
                d = P.BY_KEY[digital[role]]
                assert (r.kind, r.raw_min, r.raw_max, r.unit, r.limit) == (d.kind, d.raw_min, d.raw_max, d.unit,
                                                                           d.limit)
        assert P.BY_KEY['list%d_cut_time' % n].kind == 'cut'
        assert P.BY_KEY['list%d_cut_current' % n].kind == 'centi'
    listed = [r for r in P.REGISTERS if r.group is not None]
    assert len(listed) == 30
    assert all((r.reg_id & 0x0F) >= 0x0A and (r.reg_id >> 4) <= 4 for r in listed)


def test_color_registers():
    colors = [r for r in P.REGISTERS if r.kind == 'rgb565']
    assert [r.reg_id for r in colors] == list(range(0x50, 0x5A))
    assert [r.key for r in colors] == ['color_' + k for k in P.COLOR_DEFAULTS]
    for r in colors:
        assert (r.raw_min, r.raw_max, r.source, r.screen) == (0, 0xFFFF, 'app', 'any')


def test_low_byte_registers_fit_in_one_byte():
    """The firmware stores only the low data byte for OPP, OTP, output, list select and priority."""
    for key in ('opp', 'otp', 'output', 'list', 'priority'):
        assert P.BY_KEY[key].raw_max <= 0xFF, key


def test_bench_limit_roles():
    for r in P.REGISTERS:
        role = r.key.split('_', 1)[1] if r.group is not None else r.key
        if role in ('voltage', 'ovp'):
            assert r.limit == 'voltage', r.key
        elif role in ('current', 'ocp'):
            assert r.limit == 'current', r.key
        else:
            assert r.limit is None, r.key


@pytest.mark.parametrize('reg', P.REGISTERS, ids=lambda r: r.key)
def test_describe(reg):
    d = reg.describe()
    assert {'key', 'id', 'name', 'unit', 'min', 'max', 'raw_min', 'raw_max', 'screen', 'source', 'doc'} <= set(d)
    assert d['key'] == reg.key and d['id'] == '0x%02X' % reg.reg_id
    assert d['min'] == reg.value_min and d['max'] == reg.value_max
    assert (d['raw_min'], d['raw_max']) == (reg.raw_min, reg.raw_max)
    assert ('choices' in d) == bool(reg.choices)
    assert ('list' in d) == (reg.group is not None)
    if reg.group is not None:
        assert d['list'] == reg.group
    json.dumps(d)                                   # the CLI prints it as JSON


def test_register_lookup():
    assert P.register(0x00) is P.BY_KEY['voltage']
    assert P.register('ovp').reg_id == 0x02
    assert P.register(0x4F).key == 'list4_cut_time'
    for bad in (0xFF, 0x11, 0x5A, 'nope', 'VSET'):
        with pytest.raises(KeyError):
            P.register(bad)


def test_value_min_max_are_finite():
    for r in P.REGISTERS:
        for v in (r.value_min, r.value_max):
            if isinstance(v, float):
                assert math.isfinite(v)
