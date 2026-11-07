"""The virtual PP300S (openaxpp300s/sim.py) checked against the firmware V1.4.4 analysis and the hardware measurements.

Most tests feed bytes to sim.receive() at chosen wall-clock times and materialise the scheduled answers, so nothing
sleeps and every time is exact. The SimSerial tests replace the simulator's clock with a fake one. Nothing here opens
a serial port (conftest sets OPENAXPP300S_SIM=1, OPENAXPP300S_NO_SERIAL=1, OPENAXPP300S_SIM_DELAY=0 and a private
OPENAXPP300S_HOME for every test).

Time bookkeeping: a frame written at wall time t has its last byte at t + 5 byte times, which is when the simulator
applies it. A status query written at t + dt is answered (latency scale 0) with the state at t + dt + 5 byte times.
So "on(T0)" followed by "status(T0 + dt)" reads the output exactly dt after the ON frame.
"""
import json
import math
import os
import subprocess
import sys

import pytest

from openaxpp300s import protocol as P
from openaxpp300s import sim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

T0 = 1000.012               # 12 ms into a 50 ms TIM3 tick: no tick boundary near any time used below
ACK = bytes([P.ACK])
Q = P.query_frame()
VSET, ISET, OVP, OCP, DELAY, REV, OPP, OTP, OUT, LIST, PRIO = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, \
    0x09, 0x10
L0_V, L0_OVP, L0_CUT_I, L0_CUT_T = 0x0A, 0x0C, 0x0E, 0x0F
NOT_SETTINGS = {'eeprom_writes', 'frames', 'audit', 'rx_state', 'rx_buf', 'rx_t_a5'}


# ------------------------------------------------------------------------------------------------ helpers

def F(reg, data=0):
    return P.encode(reg, data)


def send(burst, t, pending=None):
    """Bytes written at wall time t -> [(deliver_at, answer bytes)] (status replies computed at their send time)."""
    return [(at, sim.materialize(item)) for at, item in sim.receive(bytes(burst), t, pending)]


def rx(burst, t):
    """Everything the supply sends back for burst written at t."""
    return b''.join(b for _, b in send(burst, t))


def write(reg, data, t):
    got = rx(F(reg, data), t)
    assert got == ACK, (hex(reg), data, got)


def status(t):
    got = rx(Q, t)
    assert len(got) == P.REPLY_LEN, got.hex(' ')
    return P.decode_reply(got)


def on(t):
    write(OUT, 1, t)


def off(t):
    write(OUT, 0, t)


def state():
    return sim.load()


def setstate(**kw):
    st = sim.load()
    st.update(kw)
    sim.save(st)


def settings(st):
    return {k: v for k, v in st.items() if k not in NOT_SETTINGS}


def run_sim(capsys, *args):
    code = sim.main(list(args))
    return code, capsys.readouterr().out


class FakeClock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    """Replace the simulator's clock (sim uses time.time() for SimSerial and 'now')."""
    c = FakeClock(T0)
    monkeypatch.setattr(sim, 'time', c)
    return c


@pytest.fixture
def latency(monkeypatch):
    """Real answer latency (ACK 13 ms, reply on the next 50 ms tick)."""
    monkeypatch.setenv('OPENAXPP300S_SIM_DELAY', '1')


# ------------------------------------------------------------------------------------------------ byte parser

def test_whole_frame_is_acked_and_stored():
    out = send(F(VSET, 500), T0)
    assert [b for _, b in out] == [ACK]
    assert state()['vset'] == 500


@pytest.mark.parametrize('cut', [1, 2, 3, 4, 5])
@pytest.mark.parametrize('gap', [0.002, 0.009])
def test_frame_split_over_two_writes_within_10ms_is_accepted(cut, gap):
    f = F(VSET, 500)
    assert rx(f[:cut], T0) == b''
    assert rx(f[cut:], T0 + gap) == ACK
    assert state()['vset'] == 500


@pytest.mark.parametrize('cut', [1, 2, 3, 4, 5])
@pytest.mark.parametrize('gap', [0.0115, 0.5])
def test_frame_split_over_more_than_10ms_is_dropped(cut, gap):
    f = F(VSET, 500)                                  # a5 00 f4 01 f5 5a: no A5 in the remainder
    assert rx(f[:cut], T0) == b''
    assert rx(f[cut:], T0 + gap) == b''
    assert state()['vset'] == 1200
    assert rx(F(VSET, 700), T0 + gap + 0.001) == ACK  # the parser is hunting again
    assert state()['vset'] == 700


@pytest.mark.parametrize('garbage', [b'\xa5', b'\x33\xa5\x10', b'\x00\x00\xa5'])
def test_garbage_containing_a5_eats_the_frame(garbage):
    assert rx(garbage + F(VSET, 500), T0) == b''
    assert state()['vset'] == 1200
    audit = state()['audit'][-1]
    assert audit['frames'] and audit['frames'][0].get('dropped')
    assert rx(F(VSET, 500), T0 + 0.05) == ACK
    assert state()['vset'] == 500


@pytest.mark.parametrize('garbage', [b'\x00', b'\x5a\x5a', b'\x00\x11\x5a\xff\x01\x13'])
def test_garbage_without_a5_is_skipped(garbage):
    assert rx(garbage + F(VSET, 500), T0) == ACK
    assert state()['vset'] == 500


def test_trailing_a5_eats_a_frame_sent_within_10ms_but_not_later():
    assert rx(F(VSET, 500) + b'\xa5', T0) == ACK      # the stray A5 leaves the parser collecting
    assert rx(F(ISET, 300), T0 + 0.005) == b''          # taken as the 5 bytes after the stray A5
    assert state()['iset'] == 1010
    assert rx(F(VSET, 600) + b'\xa5', T0 + 1) == ACK
    assert rx(F(ISET, 300), T0 + 1.05) == ACK           # the stray A5 timed out after ~10 ms
    assert state()['iset'] == 300


@pytest.mark.parametrize('pos', [4, 5], ids=['checksum', 'tail'])
def test_bad_checksum_or_tail_gets_no_answer(pos):
    f = bytearray(F(VSET, 500))
    f[pos] ^= 0x01
    assert rx(f, T0) == b''
    assert state()['vset'] == 1200
    assert rx(F(VSET, 500), T0 + 0.001) == ACK          # all 5 bytes consumed: hunting again at once


def test_bad_query_checksum_gets_no_reply():
    q = bytearray(Q)
    q[4] ^= 0x01
    assert rx(q, T0) == b''
    assert state()['eeprom_writes'] == 0


def test_two_frames_in_one_write_are_both_processed():
    out = send(F(VSET, 500) + F(ISET, 200), T0)
    assert [b for _, b in out] == [ACK, ACK]
    st = state()
    assert st['vset'] == 500 and st['iset'] == 200


def test_setting_and_query_in_one_write_get_ack_and_reply():
    out = [b for _, b in send(F(VSET, 500) + Q, T0)]
    assert out[0] == ACK and len(out) == 2 and len(out[1]) == P.REPLY_LEN
    P.decode_reply(out[1])


def test_query_is_not_acked_and_its_data_is_ignored():
    assert len(rx(Q, T0)) == P.REPLY_LEN                  # no 01 in front of the reply
    q = bytes([P.FRAME_HEAD, 0xFF, 0x01, 0x00, 0x00, P.FRAME_TAIL])   # data 1, checksum (FF + 01 + 00) & FF = 00
    got = rx(q, T0 + 0.1)
    assert len(got) == P.REPLY_LEN
    P.decode_reply(got)


@pytest.mark.parametrize('reg', [0x11, 0x19, 0x20, 0x29, 0x30, 0x39, 0x40, 0x49, 0x5A, 0x5F, 0x6A, 0x80, 0xFE])
def test_unknown_id_is_acked_and_changes_nothing(reg):
    before = settings(state())
    assert rx(F(reg, 0x1234), T0) == ACK
    st = state()
    assert settings(st) == before
    assert st['eeprom_writes'] == 1 and st['frames'] == 1


@pytest.mark.parametrize('reg,data,key,stored', [
    (VSET, 0xFFFF, 'vset', 0xFFFF),
    (ISET, 5000, 'iset', 5000),
    (OVP, 9999, 'ovp', 9999),
    (OCP, 0xFFFF, 'ocp', 0xFFFF),
    (DELAY, 0xFFFF, 'delay', 0xFFFF),
    (REV, 40000, 'rev', 40000),
    (OPP, 0x1234, 'opp', 0x34),                     # low byte only
    (OTP, 0x0203, 'otp', 0x03),
    (LIST, 0x0107, 'list_sel', 7),
    (PRIO, 0x0302, 'priority', 2),
])
def test_out_of_range_data_is_stored_as_sent_and_acked(reg, data, key, stored):
    assert rx(F(reg, data), T0) == ACK
    assert state()[key] == stored


def test_list_colour_and_cut_time_registers_store_as_sent():
    write(0x2A, 4000, T0)                               # list 2 VSET, out of range
    write(0x4E, 2000, T0)                               # list 4 cut-off current
    write(0x3F, P.cut_raw('min', 45), T0)               # list 3 cut-off minutes
    write(0x1F, 200, T0)                                # list 1 cut-off seconds 200 (unit 0, no range check)
    write(0x55, 0xBEEF, T0)
    st = state()
    assert st['lists'][2]['v'] == 4000
    assert st['lists'][4]['cut_i'] == 2000
    assert st['lists'][3]['cut_m'] == 45
    assert st['lists'][1]['cut_s'] == 200
    assert st['colors'][5] == 0xBEEF


def test_cut_time_with_unknown_unit_is_acked_but_stores_nothing():
    before = settings(state())
    assert rx(F(0x0F, 0x0305), T0) == ACK
    assert settings(state()) == before


@pytest.mark.parametrize('data,on_cmd', [(0x0001, True), (0x00FF, True), (0x0201, True), (0x0100, False),
                                         (0x0000, False)])
def test_output_register_uses_the_low_byte_only(data, on_cmd):
    if not on_cmd:
        on(T0 - 1)
        assert state()['on_cmd']
    assert rx(F(OUT, data), T0) == ACK
    assert state()['on_cmd'] is on_cmd


def test_supply_switched_off_answers_nothing():
    setstate(powered=False)
    assert rx(F(VSET, 500), T0) == b''
    assert rx(Q, T0 + 0.1) == b''
    st = state()
    assert st['vset'] == 1200 and st['eeprom_writes'] == 0


# ------------------------------------------------------------------------------------------------ EEPROM side effect

def test_every_valid_frame_including_queries_saves_the_cursor_field():
    write(VSET, 500, T0)                               # 1
    write(OVP, 800, T0)                                # 2
    status(T0 + 0.1)                                   # 3
    status(T0 + 0.2)                                   # 4
    assert len(rx(Q + Q, T0 + 0.3)) == P.REPLY_LEN     # 5, 6: one reply, but both are valid frames
    assert rx(F(0x11, 5), T0 + 0.4) == ACK             # 7: unknown id
    bad = bytearray(F(VSET, 600))
    bad[4] ^= 1
    assert rx(bad, T0 + 0.5) == b''                    # bad checksum: not a valid frame
    f = F(ISET, 300)
    rx(f[:3], T0 + 0.6)
    rx(f[3:], T0 + 0.62)                               # split over 20 ms: dropped
    st = state()
    assert st['eeprom_writes'] == 7
    assert st['frames'] == 7


# ------------------------------------------------------------------------------------------------ query replies

def test_status_reply_with_the_output_off():
    r = status(T0)
    assert r['output_flag'] == 0 and r['output_on'] is False and r['mode'] == 'OFF'
    assert r['cc_flag'] == 1                           # byte 11 reads 1 with the output off
    assert r['vout_v'] == 0 and r['iout_a'] == 0 and r['pout_w'] == 0
    assert r['vin_v'] == pytest.approx(19.80) and r['temp_c'] == pytest.approx(28.5)


def test_ack_arrives_13ms_after_the_frame(latency):
    out = sim.receive(F(VSET, 500), T0, None)
    assert len(out) == 1 and out[0][1] == ACK
    assert out[0][0] - T0 == pytest.approx(5 * sim.BYTE_TIME + 0.013, abs=1e-6)


def test_reply_is_sent_on_the_next_50ms_tick(latency):
    out = sim.receive(Q, T0, None)                     # T0 = 1000.012: next tick 1000.050
    assert len(out) == 1
    at, item = out[0]
    assert 1000.050 < at <= 1000.050 + 0.002
    P.decode_reply(sim.materialize(item))


def test_queries_within_one_tick_get_one_reply(latency):
    (at1, _), = sim.receive(Q, T0, None)
    assert sim.receive(Q, T0 + 0.018, at1) == []
    assert sim.receive(Q, T0 + 0.033, at1) == []
    assert state()['eeprom_writes'] == 3


def test_queries_in_successive_ticks_get_one_reply_each(latency):
    (at1, _), = sim.receive(Q, T0, None)
    (at2, _), = sim.receive(Q, T0 + 0.05, at1)         # next tick, first reply not delivered yet
    assert at2 - at1 == pytest.approx(0.05, abs=1e-6)
    (at3, _), = sim.receive(Q, T0 + 0.1, None)         # earlier replies delivered
    assert at3 - at2 == pytest.approx(0.05, abs=1e-6)


@pytest.mark.parametrize('scale', ['0', '1'])
def test_two_queries_in_one_write_get_one_reply(monkeypatch, scale):
    monkeypatch.setenv('OPENAXPP300S_SIM_DELAY', scale)
    out = sim.receive(Q + Q, T0, None)
    assert len(out) == 1


def _wr(s, clock, data):
    """Write, then let 1 ms pass (the answers are due once the last byte has gone out, 87 us per byte)."""
    n = s.write(data)
    clock.t += 0.001
    return n


def test_simserial_without_latency(clock):
    s = sim.SimSerial()
    assert _wr(s, clock, F(VSET, 500)) == 6
    assert s.in_waiting == 1 and s.read(1) == ACK
    _wr(s, clock, F(VSET, 500) + F(ISET, 100))
    assert s.read(10) == ACK + ACK
    _wr(s, clock, Q)
    P.decode_reply(s.read(100))
    _wr(s, clock, Q + Q)
    assert s.in_waiting == P.REPLY_LEN
    s.reset_input_buffer()
    assert s.in_waiting == 0
    s.close()
    assert not s.is_open


def test_simserial_latency_and_coalescing(clock, latency):
    s = sim.SimSerial()
    s.write(F(VSET, 500))                              # at 1000.012
    assert s.in_waiting == 0
    clock.t = T0 + 0.013                               # 13 ms after the first byte: the frame took 0.43 ms more
    assert s.in_waiting == 0
    clock.t = T0 + 0.0145
    assert s.read(5) == ACK
    clock.t = 1000.020
    s.write(Q)
    clock.t = 1000.030
    s.write(Q)                                         # same tick as the first query
    clock.t = 1000.049
    assert s.in_waiting == 0                           # replies go out on the 50 ms tick
    clock.t = 1000.0535
    assert s.in_waiting == P.REPLY_LEN
    clock.t = 1000.300
    assert s.in_waiting == P.REPLY_LEN                 # still one reply for the two queries
    s.read(100)
    clock.t = 1000.312
    s.write(Q)                                         # reply at the 1000.350 tick
    clock.t = 1000.362
    s.write(Q)                                         # next tick, first reply not read yet: own reply
    clock.t = 1000.500
    data = s.read(100)
    assert len(data) == 2 * P.REPLY_LEN
    P.decode_reply(data[:15])
    P.decode_reply(data[15:])


# ------------------------------------------------------------------------------------------------ output on / off

def test_output_on_soft_start():
    r = status(T0 - 1)
    assert r['output_flag'] == 0
    on(T0)
    r = status(T0 + 0.01)
    assert r['output_flag'] == 1 and r['cc_flag'] == 1  # flag at once, byte 11 = 1 during the soft start
    assert r['vout_v'] == pytest.approx(0.011, abs=0.003)
    r = status(T0 + 1.0)
    assert r['output_flag'] == 1 and r['cc_flag'] == 1 and r['vout_v'] < 0.05
    r = status(T0 + 1.35)
    assert r['output_flag'] == 1 and r['cc_flag'] == 0 and r['mode'] == 'CV'
    assert r['vout_v'] == pytest.approx(12.0, abs=0.01)


def test_second_on_does_not_restart_the_soft_start():
    on(T0)
    on(T0 + 1.0)
    assert status(T0 + 1.35)['vout_v'] == pytest.approx(12.0, abs=0.01)


def test_output_off_decay():
    on(T0)
    assert status(T0 + 2)['vout_v'] == pytest.approx(12.0, abs=0.01)
    off(T0 + 2)
    r1, r2, r3 = status(T0 + 2.01), status(T0 + 2.1), status(T0 + 3.0)
    for r in (r1, r2, r3):
        assert r['output_flag'] == 0 and r['cc_flag'] == 1 and r['mode'] == 'OFF' and r['iout_a'] == 0
    assert 9.0 < r1['vout_v'] < 12.0                   # ~0.1 s time constant with a 10 MOhm load
    assert 2.5 < r2['vout_v'] < 7.0
    assert r3['vout_v'] == 0.0


def test_readings_below_9mV_read_zero():
    on(T0)
    status(T0 + 2)
    off(T0 + 2)
    t10 = sim.OFF_TAU * math.log(12.0 / 0.0105)        # where the decay passes 10.5 mV and 8 mV
    t8 = sim.OFF_TAU * math.log(12.0 / 0.008)
    assert status(T0 + 2 + t10)['vout_v'] == pytest.approx(0.0105, abs=0.0011)
    assert status(T0 + 2 + t8)['vout_v'] == 0.0


def test_delay():
    write(DELAY, 3, T0 - 1)
    on(T0)
    for dt in (0.01, 1.0, 2.9):
        r = status(T0 + dt)
        assert r['output_flag'] == 0 and r['cc_flag'] == 1 and r['vout_v'] == 0, (dt, r)
    r = status(T0 + 3.05)
    assert r['output_flag'] == 1 and r['vout_v'] < 0.05  # soft start after the delay
    r = status(T0 + 4.5)
    assert r['output_flag'] == 1 and r['vout_v'] == pytest.approx(12.0, abs=0.01)


def test_rev_toggles_every_rev_seconds():
    write(REV, 2, T0 - 1)
    on(T0)
    expect = [(0.5, 0), (1.9, 0), (2.5, 1), (3.5, 1), (3.9, 1), (4.5, 0), (5.9, 0), (6.5, 1), (8.5, 0)]
    for dt, flag in expect:
        assert status(T0 + dt)['output_flag'] == flag, dt
    assert status(T0 + 7.9)['vout_v'] == pytest.approx(12.0, abs=0.01)   # on half: up after the soft start


def test_delay_then_rev():
    write(DELAY, 1, T0 - 1)
    write(REV, 2, T0 - 1)
    on(T0)
    for dt, flag in [(0.5, 0), (2.5, 0), (3.5, 1), (4.9, 1), (5.5, 0), (7.5, 1)]:
        assert status(T0 + dt)['output_flag'] == flag, dt


def test_off_during_delay_keeps_the_output_off():
    write(DELAY, 2, T0 - 1)
    on(T0)
    off(T0 + 1)
    assert status(T0 + 5)['output_flag'] == 0


# ------------------------------------------------------------------------------------------------ CV / CC

def test_cv_and_cc_with_a_resistive_load():
    write(VSET, 1200, T0 - 1)
    write(ISET, 100, T0 - 1)                           # 1 A
    setstate(load_ohm=5.0)                             # 12 V / 5 Ohm = 2.4 A > 1 A: CC
    on(T0)
    r = status(T0 + 2)
    assert r['mode'] == 'CC' and r['cc_flag'] == 1 and r['output_flag'] == 1
    assert r['vout_v'] == pytest.approx(5.0, abs=0.01) and r['iout_a'] == pytest.approx(1.0, abs=0.001)
    assert r['pout_w'] == pytest.approx(5.0, abs=0.01)
    setstate(load_ohm=100.0)                           # 0.12 A: CV
    r = status(T0 + 3)
    assert r['mode'] == 'CV' and r['cc_flag'] == 0
    assert r['vout_v'] == pytest.approx(12.0, abs=0.01) and r['iout_a'] == pytest.approx(0.12, abs=0.001)
    assert r['pout_w'] == pytest.approx(1.44, abs=0.01)


# ------------------------------------------------------------------------------------------------ protections

def _trip_ovp():
    write(VSET, 300, T0 - 1)
    write(OVP, 300, T0 - 1)                            # VSET == OVP: tripped on the real supply (overshoot)
    on(T0)
    r = status(T0 + 0.5)
    assert r['output_flag'] == 1                       # still in the soft start, near 0 V
    r = status(T0 + 2)
    assert r['output_flag'] == 0, r
    return r


def test_ovp_trips_when_vset_equals_ovp():
    _trip_ovp()
    st = state()
    assert st['alarm'] == 'OVP' and not st['on_cmd']
    r = status(T0 + 5)
    assert r['output_flag'] == 0 and r['vout_v'] == 0
    assert state()['alarm'] == 'OVP'                   # latched


@pytest.mark.parametrize('vset', [0.5, 1.0, 3.0, 5.0, 12.0, 13.33, 20.0, 29.5, 30.0])
def test_ovp_with_the_device_headroom_does_not_trip(vset):
    """device.py requires OVP >= VSET + max(0.2 V, 1.5 %)."""
    write(VSET, round(vset * 100), T0 - 1)
    write(OVP, math.ceil((vset + max(0.2, 0.015 * vset)) * 100 - 1e-6), T0 - 1)
    on(T0)
    r = status(T0 + 2)
    assert r['output_flag'] == 1 and r['vout_v'] == pytest.approx(vset, abs=0.01)
    assert state()['alarm'] is None


@pytest.mark.parametrize('ocp,trip', [(200, True), (590, True), (700, False)])
def test_ocp(ocp, trip):
    write(ISET, 1000, T0 - 1)
    write(OCP, ocp, T0 - 1)
    setstate(load_ohm=2.0)                             # 12 V / 2 Ohm = 6 A (CV, under ISET 10 A)
    on(T0)
    r = status(T0 + 3)
    assert r['output_flag'] == (0 if trip else 1), r
    assert state()['alarm'] == ('OCP' if trip else None)


def test_ocp_trips_only_after_100ms_of_overcurrent():
    """Firmware 0x0800B06A: OCP needs 100 consecutive 1 ms samples with OCP <= I (counter 0x2000015A)."""
    write(ISET, 1000, T0 - 1)
    write(OCP, 200, T0 - 1)
    setstate(load_ohm=2.0)
    on(T0)
    r = status(T0 + sim.SOFT_START + 0.03)             # current up (6 A > 2 A) for 30 ms only
    assert r['output_flag'] == 1 and r['iout_a'] == pytest.approx(6.0, abs=0.01), r
    assert state()['alarm'] is None
    r = status(T0 + sim.SOFT_START + 0.3)
    assert r['output_flag'] == 0
    assert state()['alarm'] == 'OCP'


@pytest.mark.parametrize('vset_raw,trip', [(1000, False), (1022, False), (1025, True), (1050, True)])
def test_opp_trips_when_floor_of_power_exceeds_limit(vset_raw, trip):
    """OPP data 1 = 20 W; trip when floor(P) > 20 W. 5 Ohm load: 10.00 V 20.0 W, 10.22 V 20.9 W, 10.25 V 21.0 W."""
    assert rx(F(OPP, 0x0101), T0 - 1) == ACK            # high byte dropped: OPP 1
    write(VSET, vset_raw, T0 - 1)
    write(ISET, 500, T0 - 1)
    setstate(load_ohm=5.0)
    on(T0)
    r = status(T0 + 2)
    assert r['output_flag'] == (0 if trip else 1), r
    assert state()['alarm'] == ('OPP' if trip else None)


@pytest.mark.parametrize('temp,trip', [(59.9, False), (60.0, True), (75.0, True)])
def test_otp_trips_at_or_above_otp_times_10C(temp, trip):
    write(OTP, 6, T0 - 1)                               # 60 C
    setstate(temp=temp)
    on(T0)
    r = status(T0 + 2)
    assert r['output_flag'] == (0 if trip else 1), r
    assert state()['alarm'] == ('OTP' if trip else None)


def test_otp_compares_the_temperature_the_reply_reports():
    """Reply bytes 9-10 and the OTP check (0x0800195E: trip when [0x200001FC] >= OTP*100) read the same variable,
    so a reply cannot keep showing T >= OTP*10 C with the output on."""
    write(VSET, 3000, T0 - 1)
    write(OTP, 6, T0 - 1)                               # 60 C
    setstate(load_ohm=3.0)                             # 30 V, 10 A, 300 W: under OVP 30.5 V, OCP 10.5 A, OPP 310 W
    on(T0)
    for dt in (2.0, 3.0):
        r = status(T0 + dt)
        assert not (r['output_on'] and r['temp_c'] >= 60.0), r


@pytest.mark.parametrize('reg,name', [(OVP, 'OVP'), (OCP, 'OCP'), (OTP, 'OTP')])
def test_zero_threshold_trips_even_with_the_output_off(reg, name):
    """The OVP check (0x0800B040: trip when OVP <= measured V), the OCP check (0x0800B06A, 100 ms) and the OTP check
    (0x0800195E: T >= OTP*100) are not gated by the output state; the firmware report: 'OVP=0 trips instantly and
    permanently, even with the output off' and '0x02/0x03/0x07 = 0: permanent trip plus alarm'."""
    write(reg, 0, T0)
    status(T0 + 0.5)
    assert state()['alarm'] == name


def test_ovp_set_below_the_decaying_voltage_after_off_trips():
    """OVP <= measured V trips with the output off too; after OFF the output decays with ~0.1 s (measured), so an OVP
    under the still-present voltage trips."""
    on(T0)
    assert status(T0 + 2)['vout_v'] == pytest.approx(12.0, abs=0.01)
    off(T0 + 2)
    write(OVP, 500, T0 + 2.02)                         # 5 V while the output still has ~9.8 V
    status(T0 + 2.05)
    assert state()['alarm'] == 'OVP'


def test_ovp_set_after_the_output_has_decayed_does_not_trip():
    on(T0)
    status(T0 + 2)
    off(T0 + 2)
    write(OVP, 500, T0 + 4)
    status(T0 + 4.1)
    assert state()['alarm'] is None


def test_alarm_stays_latched_until_a_front_panel_key_press(capsys):
    _trip_ovp()
    status(T0 + 10)
    assert state()['alarm'] == 'OVP'
    code, out = run_sim(capsys, 'press')
    assert code == 0 and json.loads(out) == {'ok': True, 'cleared': 'OVP'}
    st = state()
    assert st['alarm'] is None and not st['on_cmd']
    assert status(T0 + 11)['output_flag'] == 0         # a key press does not switch the output back on


def test_remote_on_after_a_trip_switches_the_output_on():
    _trip_ovp()
    write(OVP, 400, T0 + 3)
    on(T0 + 3)
    r = status(T0 + 5)
    assert r['output_flag'] == 1 and r['vout_v'] == pytest.approx(3.0, abs=0.01)
    assert state()['alarm'] == 'OVP'                   # the warning stays until a key press


def test_remote_on_after_a_trip_trips_again_if_the_cause_remains():
    _trip_ovp()
    status(T0 + 2.5)
    on(T0 + 3)
    assert status(T0 + 3.5)['output_flag'] == 1        # soft start
    assert status(T0 + 5)['output_flag'] == 0
    assert state()['alarm'] == 'OVP'


# ------------------------------------------------------------------------------------------------ screens

def test_chinese_screen_uses_the_selected_list_not_the_digital_registers():
    setstate(screen='chinese')
    write(VSET, 1500, T0 - 1)                           # digital registers: stored, not used on this screen
    write(OVP, 100, T0 - 1)                             # (OVP 1 V would trip if it were used)
    st = state()
    assert st['vset'] == 1500 and st['ovp'] == 100
    on(T0)
    r = status(T0 + 2)
    assert r['output_flag'] == 1 and r['vout_v'] == pytest.approx(5.0, abs=0.01)   # list 0: 5 V
    write(L0_V, 800, T0 + 2.5)
    assert status(T0 + 4)['vout_v'] == pytest.approx(8.0, abs=0.01)
    write(LIST, 2, T0 + 4.5)
    assert status(T0 + 6)['vout_v'] == pytest.approx(20.0, abs=0.01)                # list 2: 20 V
    assert state()['alarm'] is None


def test_chinese_screen_list_ovp_trips():
    setstate(screen='chinese')
    write(L0_OVP, 500, T0 - 1)                          # list 0 OVP = its VSET 5.00 V
    on(T0)
    assert status(T0 + 2)['output_flag'] == 0
    assert state()['alarm'] == 'OVP'


@pytest.mark.parametrize('screen', ['digital', 'waveform'])
def test_lists_are_ignored_on_the_digital_and_waveform_screens(screen):
    setstate(screen=screen)
    write(L0_V, 800, T0 - 1)
    write(L0_OVP, 100, T0 - 1)                          # list 0 OVP 1 V: would trip if it were used
    write(LIST, 1, T0 - 1)
    st = state()
    assert st['lists'][0]['v'] == 800 and st['lists'][0]['ovp'] == 100 and st['list_sel'] == 1
    on(T0)
    r = status(T0 + 2)
    assert r['output_flag'] == 1 and r['vout_v'] == pytest.approx(12.0, abs=0.01)
    assert state()['alarm'] is None


def test_chinese_screen_ignores_delay_and_rev():
    setstate(screen='chinese')
    write(DELAY, 5, T0 - 1)
    write(REV, 3, T0 - 1)
    on(T0)
    for dt in (0.01, 2.0, 4.0, 7.0, 10.0):
        assert status(T0 + dt)['output_flag'] == 1, dt
    assert status(T0 + 11)['vout_v'] == pytest.approx(5.0, abs=0.01)


def _cut_time(seconds, t):
    for unit, val in (('h', 0), ('min', 0), ('s', seconds)):
        write(L0_CUT_T, P.cut_raw(unit, val), t)


def test_chinese_screen_cut_off_time():
    setstate(screen='chinese')
    _cut_time(10, T0 - 1)
    L = state()['lists'][0]
    assert (L['cut_h'], L['cut_m'], L['cut_s']) == (0, 0, 10)
    on(T0)
    assert status(T0 + 5)['output_flag'] == 1
    assert status(T0 + 11)['output_flag'] == 0
    st = state()
    assert st['cut'] == 'time' and st['alarm'] is None   # a cut-off is not a protection alarm
    on(T0 + 12)                                        # the on-time restarts after a cut-off
    assert status(T0 + 15)['output_flag'] == 1
    assert state()['cut'] is None
    assert status(T0 + 23)['output_flag'] == 0


def test_chinese_screen_cut_off_time_counts_the_accumulated_on_time():
    """The on-time [0x20000128] counts seconds while byte 12 == 1 (0x0800C70A) and is reset only after a time cut-off
    (0x0800B2AC-0x0800B2BC) or by the front panel, not by OFF/ON: 6.5 s + 6 s on reach a 10 s cut-off."""
    setstate(screen='chinese')
    _cut_time(10, T0 - 1)
    on(T0)
    assert status(T0 + 6)['output_flag'] == 1
    off(T0 + 6.5)
    on(T0 + 20)
    assert status(T0 + 26)['output_flag'] == 0
    assert state()['cut'] == 'time'


def test_chinese_screen_cut_off_current():
    setstate(screen='chinese')
    write(L0_CUT_I, 10, T0 - 1)                         # 100 mA; the 10 MOhm default load draws ~0.5 uA
    on(T0)
    assert status(T0 + 3)['output_flag'] == 1
    assert status(T0 + 20)['output_flag'] == 0
    st = state()
    assert st['cut'] == 'current' and st['alarm'] is None


def test_chinese_screen_no_cut_off_while_the_current_is_above_it():
    setstate(screen='chinese', load_ohm=10.0)          # 5 V / 10 Ohm = 0.5 A
    write(L0_CUT_I, 10, T0 - 1)
    on(T0)
    r = status(T0 + 20)
    assert r['output_flag'] == 1 and r['iout_a'] == pytest.approx(0.5, abs=0.001)
    assert state()['cut'] is None


def test_cut_offs_are_ignored_on_the_digital_screen():
    _cut_time(5, T0 - 1)
    write(L0_CUT_I, 10, T0 - 1)
    on(T0)
    assert status(T0 + 30)['output_flag'] == 1
    assert state()['cut'] is None


# ------------------------------------------------------------------------------------------------ clock offset

def test_clock_offset_moves_device_time_without_sleeping(capsys):
    on(T0)
    assert status(T0 + 0.01)['vout_v'] < 0.05
    code, out = run_sim(capsys, 'advance', '2')
    assert code == 0
    r = status(T0 + 0.02)                              # wall time 20 ms later, device time 2 s later
    assert r['vout_v'] == pytest.approx(12.0, abs=0.01)


def test_simserial_with_advance(clock, capsys):
    s = sim.SimSerial()
    _wr(s, clock, F(OUT, 1))
    assert s.read(1) == ACK
    _wr(s, clock, Q)
    r = P.decode_reply(s.read(100))
    assert r['output_flag'] == 1 and r['vout_v'] < 0.05
    assert run_sim(capsys, 'advance', '2')[0] == 0
    _wr(s, clock, Q)
    r = P.decode_reply(s.read(100))
    assert r['output_flag'] == 1 and r['cc_flag'] == 0 and r['vout_v'] == pytest.approx(12.0, abs=0.01)


# ------------------------------------------------------------------------------------------------ sim control CLI

def test_cli_reset(capsys):
    setstate(vset=100, alarm='OVP', clock_offset=7.0, eeprom_writes=9, on_cmd=True, screen='chinese')
    code, out = run_sim(capsys, 'reset')
    d = json.loads(out)
    assert code == 0 and d['ok'] and d['sim'] == 'reset' and d['file'] == sim.state_file()
    st, default = state(), sim.default_state()
    for k in ('vset', 'alarm', 'clock_offset', 'eeprom_writes', 'on_cmd', 'screen', 'audit'):
        assert st[k] == default[k], k


def test_cli_show(capsys, clock):
    setstate(on_cmd=True, t_on=T0 - 5)
    code, out = run_sim(capsys, 'show')
    d = json.loads(out)
    assert code == 0 and 'audit' not in d
    assert d['vset'] == 1200 and d['screen'] == 'digital'
    assert d['now']['on'] is True and d['now']['cc'] is False
    assert d['now']['vout'] == pytest.approx(12.0, abs=0.01)
    assert sim.main([]) == 0                            # no command: show
    assert 'now' in json.loads(capsys.readouterr().out)


def test_cli_set_coerces_values(capsys):
    code, out = run_sim(capsys, 'set', 'temp=45.5', 'otp=9', 'screen=chinese', 'alarm=none', 'on_cmd=true',
                        'load_ohm=1e3')
    assert code == 0 and json.loads(out) == {'ok': True}
    st = state()
    assert st['temp'] == 45.5 and st['otp'] == 9 and isinstance(st['otp'], int)
    assert st['screen'] == 'chinese' and st['alarm'] is None and st['on_cmd'] is True
    assert st['load_ohm'] == 1000.0


def test_cli_set_unknown_key_changes_nothing(capsys):
    code, out = run_sim(capsys, 'set', 'temp=50', 'bogus=1')
    assert code == 2 and json.loads(out)['ok'] is False
    assert state()['temp'] == 28.5


def test_cli_advance(capsys):
    code, out = run_sim(capsys, 'advance', '2.5')
    assert code == 0 and json.loads(out) == {'ok': True, 'clock_offset': 2.5}
    code, out = run_sim(capsys, 'advance', '1')
    assert json.loads(out)['clock_offset'] == pytest.approx(3.5)
    assert state()['clock_offset'] == pytest.approx(3.5)


def test_cli_press(capsys):
    setstate(alarm='OCP')
    code, out = run_sim(capsys, 'press')
    assert code == 0 and json.loads(out) == {'ok': True, 'cleared': 'OCP'}
    assert state()['alarm'] is None
    code, out = run_sim(capsys, 'press')
    assert json.loads(out) == {'ok': True, 'cleared': None}


def test_cli_audit(capsys):
    rx(F(VSET, 500), T0)
    bad = bytearray(F(ISET, 100))
    bad[4] ^= 1
    rx(bad, T0 + 0.1)
    code, out = run_sim(capsys, 'audit')
    entries = json.loads(out)
    assert code == 0 and len(entries) == 2
    assert entries[0]['rx'] == F(VSET, 500).hex(' ') and entries[0]['frames'] == [{'id': 0, 'data': 500}]
    assert entries[1]['frames'][0]['dropped']
    code, out = run_sim(capsys, 'audit', '--clear')
    assert len(json.loads(out)) == 2                   # printed, then cleared
    code, out = run_sim(capsys, 'audit')
    assert json.loads(out) == []


def test_cli_unknown_command(capsys):
    code, out = run_sim(capsys, 'frobnicate')
    assert code == 2 and 'usage' in out


def test_cli_as_a_module():
    def run(*args):
        p = subprocess.run([sys.executable, '-m', 'openaxpp300s.sim'] + list(args), cwd=ROOT, capture_output=True,
                           text=True, encoding='utf-8', env=dict(os.environ), timeout=60)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)

    assert run('set', 'vset=500', 'alarm=OVP') == {'ok': True}
    assert state()['vset'] == 500
    d = run('show')
    assert d['vset'] == 500 and d['alarm'] == 'OVP' and 'now' in d
    assert run('press') == {'ok': True, 'cleared': 'OVP'}
    assert state()['alarm'] is None
