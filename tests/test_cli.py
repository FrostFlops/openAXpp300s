"""Tests of the openaxpp300s command line (cli.py): every command and option, run in-process through the 'cli'
fixture and, for a few, in a child process like the MCP server runs them.

Everything runs against the virtual supply (sim.py). Besides the conftest isolation (OPENAXPP300S_SIM=1,
OPENAXPP300S_NO_SERIAL=1), an autouse guard here replaces serial.Serial and the port enumeration so that any attempt
to reach a real port fails the test. Time on the virtual supply is moved forward with its clock offset (the 'clock'
fixture) instead of sleeping where the CLI allows it.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from conftest import ROOT, run_cli_proc
from openaxpp300s import protocol as P
from openaxpp300s import sim, state

VENDOR_HEADER = '电压(V),电流(A),时间(S)'
VENDOR_TEMPLATE = os.path.join(ROOT, 'Vendor', '序列模板.csv')


# ------------------------------------------------------------------ fixtures and helpers

@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    """Fail loudly if anything tries to open or enumerate a real serial port; reset the CLI text mode."""
    import serial
    import serial.tools.list_ports as lp

    def refuse(*a, **k):
        raise AssertionError('a test tried to open a real serial port')
    monkeypatch.setattr(serial, 'Serial', refuse)
    monkeypatch.setattr(lp, 'comports', lambda *a, **k: [])
    from openaxpp300s import cli as C
    monkeypatch.setattr(C, '_TEXT', [False])


class Clock:
    """The virtual supply's clock (PC clock + clock_offset in its state file)."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def advance(self, seconds):
        st = sim.load()
        st['clock_offset'] = st.get('clock_offset', 0.0) + seconds
        sim.save(st)

    def jump_on(self, seconds):
        """From now on, every output-ON frame the virtual supply receives moves its clock `seconds` forward right
        after it (skips the soft start / DELAY without sleeping, while a waiting `on` polls)."""
        orig = sim.receive

        def receive(burst, t_wall, pending):
            out = orig(burst, t_wall, pending)
            b = bytes(burst)
            if len(b) == 6 and b[0] == P.FRAME_HEAD and b[1] == 0x08 and b[2] != 0:
                self.advance(seconds)
            return out
        self.mp.setattr(sim, 'receive', receive)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def keys(doc):
    return [s['key'] for s in doc['sent']]


def err(docs):
    d = docs[-1]
    assert d['ok'] is False, d
    return d['error']


def output_up(cli, clock, v=5.0, i=1.0, ovp=6.0):
    """Set VSET/ISET/OVP, switch the output on and move the virtual clock past the soft start."""
    code, docs = cli('set', '--voltage', str(v), '--current', str(i), '--ovp', str(ovp))
    assert code == 0, docs
    code, docs = cli('on', '--no-wait')
    assert code == 0, docs
    clock.advance(2.0)
    code, docs = cli('status')
    assert code == 0 and docs[-1]['output_on'], docs
    return docs[-1]


def run_capture(capsys, *args):
    """main() in-process -> (exit code, stdout text, stderr text)."""
    from openaxpp300s.cli import main
    code = main(list(args))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def stop_when_running(cmd, delay=0.2, timeout=15.0):
    """A thread that waits until this process registered `cmd` as running, then asks it to stop."""
    pid = os.getpid()

    def run():
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                running = state.list_running()
            except OSError:
                running = []
            if any(r.get('pid') == pid and r.get('cmd') == cmd for r in running):
                time.sleep(delay)
                state.request_stop(pid=pid)
                return
            time.sleep(0.02)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def write_vendor_csv(path, rows, encoding):
    text = VENDOR_HEADER + '\r\n' + ''.join('%s,%s,%s\r\n' % r for r in rows)
    path.write_bytes(text.encode(encoding))


def frames(simstate):
    return simstate()['frames']


# ------------------------------------------------------------------ ports

def test_ports_lists_sim_without_probe(cli):
    code, docs = cli('ports')
    d = docs[-1]
    assert code == 0 and d['ok'] and d['cmd'] == 'ports'
    assert [p['port'] for p in d['ports']] == ['SIM']
    p = d['ports'][0]
    assert p['usb'] and p['wch'] and not p['bluetooth']
    assert 'pp300s' not in p and 'probe' not in p
    assert d['remembered'] is None


def test_ports_probe_finds_the_virtual_supply(cli):
    code, docs = cli('ports', '--probe')
    d = docs[-1]
    p = d['ports'][0]
    assert code == 0 and p['pp300s'] is True
    assert set(p['status']) == {'vout_v', 'iout_a', 'vin_v', 'temp_c', 'output_on', 'mode'}
    assert p['status']['vin_v'] == pytest.approx(19.8, abs=0.01)
    assert p['status']['output_on'] is False
    assert d['remembered'] is None                  # the virtual port is never remembered


def test_ports_probe_virtual_supply_powered_off(cli, simstate):
    simstate(powered=False)
    code, docs = cli('ports', '--probe')
    p = docs[-1]['ports'][0]
    assert code == 0 and p['pp300s'] is False and 'status' not in p


def test_ports_probe_skips_bluetooth_and_non_usb_unless_all(cli, monkeypatch):
    from openaxpp300s import transport
    fake = [
        dict(port='COM201', description='Standard Serial over Bluetooth link', hwid='BTHENUM\\{0000}', vid=None,
             pid=None, usb=False, wch=False, bluetooth=True),
        dict(port='COM202', description='Communications Port', hwid='ACPI\\PNP0501', vid=None, pid=None, usb=False,
             wch=False, bluetooth=False),
        dict(port='COM203', description='USB-SERIAL CH340', hwid='USB VID:PID=1A86:7523', vid=0x1A86, pid=0x7523,
             usb=True, wch=True, bluetooth=False),
    ]
    probed = []

    def probe(port, timeout=0.25):
        probed.append(port)
        if port == 'COM203':
            return {'vout_v': 0.0, 'iout_a': 0.0, 'pout_w': 0.0, 'vin_v': 19.8, 'temp_c': 28.5, 'output_on': False,
                    'mode': 'OFF', 'latency_ms': 20.0}
        return None
    monkeypatch.setattr(transport, 'list_ports', lambda: [dict(p) for p in fake])
    monkeypatch.setattr(transport, 'probe', probe)
    code, docs = cli('ports', '--probe')
    ports = {p['port']: p for p in docs[-1]['ports']}
    assert code == 0
    assert probed == ['COM203']
    assert 'Bluetooth' in ports['COM201']['probe'] and 'pp300s' not in ports['COM201']
    assert ports['COM202']['probe'] == 'skipped (not USB)'
    assert ports['COM203']['pp300s'] is True and ports['COM203']['status']['vin_v'] == 19.8
    assert docs[-1]['remembered'] == 'COM203'
    probed.clear()
    code, docs = cli('ports', '--probe', '--all')
    assert code == 0 and probed == ['COM201', 'COM202', 'COM203']
    assert all('probe' not in p for p in docs[-1]['ports'])


# ------------------------------------------------------------------ status

def test_status_fields(cli):
    code, docs = cli('status')
    assert code == 0 and len(docs) == 1
    d = docs[0]
    for k in ('ok', 'cmd', 'port', 'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'output_on', 'mode', 'cc_flag',
              'output_flag', 'raw', 'latency_ms', 't', 'last_sent'):
        assert k in d, k
    assert d['ok'] is True and d['cmd'] == 'status' and d['port'] == 'SIM'
    assert d['output_on'] is False and d['mode'] == 'OFF' and d['vout_v'] == 0
    assert d['vin_v'] == pytest.approx(19.8, abs=0.01) and d['temp_c'] == pytest.approx(28.5, abs=0.1)
    assert d['last_sent'] == {}
    assert 'limits' not in d and 'note' not in d
    raw = bytes.fromhex(d['raw'])
    assert len(raw) == 15 and raw[0] == 0xA5 and raw[-1] == 0x5A


def test_status_last_sent_and_limits(cli):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    assert cli('limits', '--max-voltage', '12', '--note', 'LED strip')[0] == 0
    d = cli('status')[1][-1]
    ls = d['last_sent']
    assert ls['voltage']['value'] == 5.0 and ls['voltage']['raw'] == 500
    assert ls['current']['value'] == 1.0 and ls['ovp']['value'] == 6.0 and ls['ovp']['raw'] == 600
    assert all(isinstance(v['t'], (int, float)) for v in ls.values())
    assert d['limits']['max_voltage'] == 12 and d['limits']['note'] == 'LED strip'
    assert d['limits']['max_current'] is None
    assert cli('limits', '--clear')[0] == 0
    assert 'limits' not in cli('status')[1][-1]


def test_status_note_after_the_output_went_off(cli, clock, simstate):
    output_up(cli, clock)
    d = cli('status')[1][-1]
    assert d['output_on'] and 'note' not in d
    simstate(on_cmd=False)                          # switched off on the front panel
    d = cli('status')[1][-1]
    assert d['output_on'] is False
    assert 'note' in d and 'OVP' in d['note'] and 'front panel' in d['note']
    assert d['last_sent']['output']['value'] is True


# ------------------------------------------------------------------ set

def test_set_output_off_sends_protections_first(cli, simstate):
    code, docs = cli('set', '--rev', '0', '--delay', '1', '--current', '1', '--voltage', '5', '--ocp', '2', '--ovp', '6',
                     '--priority', 'cc', '--otp', '80', '--opp', '100')
    d = docs[-1]
    assert code == 0, d
    assert keys(d) == ['opp', 'otp', 'priority', 'ovp', 'ocp', 'voltage', 'current', 'delay', 'rev']
    assert d['output_on_before'] is False and 'settled' not in d and d['status']['output_on'] is False
    assert all(s['ack'] == '01' for s in d['sent'])
    sent = {s['key']: s for s in d['sent']}
    assert sent['opp']['value'] == 100 and sent['opp']['raw'] == 9 and sent['opp']['id'] == '0x06'
    assert sent['otp']['value'] == 80 and sent['otp']['raw'] == 8
    assert sent['priority']['value'] == 'cc' and sent['priority']['raw'] == 1 and sent['priority']['id'] == '0x10'
    assert sent['voltage']['unit'] == 'V' and sent['voltage']['name'] == 'VSET'
    st = simstate()
    assert (st['vset'], st['iset'], st['ovp'], st['ocp'], st['opp'], st['otp'], st['priority'], st['delay'],
            st['rev']) == (500, 100, 600, 200, 9, 8, 1, 1, 0)


def test_set_output_off_lowering_still_sends_protections_first(cli):
    assert cli('set', '--voltage', '12', '--ovp', '13')[0] == 0
    code, docs = cli('set', '--voltage', '5', '--ovp', '6')
    assert code == 0 and keys(docs[-1]) == ['ovp', 'voltage']


def test_set_rounds_to_10mv_and_reports_what_was_sent(cli, simstate):
    code, docs = cli('set', '--voltage', '4.996', '--ovp', '6.004')
    sent = {s['key']: s for s in docs[-1]['sent']}
    assert code == 0 and sent['voltage']['value'] == 5.0 and sent['voltage']['raw'] == 500
    assert sent['ovp']['value'] == 6.0 and simstate()['vset'] == 500


def test_set_raising_with_output_on_sends_ovp_first(cli, clock, simstate):
    output_up(cli, clock, v=5, ovp=6)
    code, docs = cli('set', '--voltage', '9', '--ovp', '10')
    d = docs[-1]
    assert code == 0, d
    assert keys(d) == ['ovp', 'voltage']
    assert d['output_on_before'] is True and d['settled'] is True and 'settle_s' in d
    assert d['status']['vout_v'] == pytest.approx(9.0, abs=0.05)
    assert simstate()['alarm'] is None


def test_set_lowering_vset_and_ovp_with_output_on_sends_vset_first(cli, clock, simstate):
    output_up(cli, clock, v=12, ovp=13)
    code, docs = cli('set', '--voltage', '5', '--ovp', '6')
    d = docs[-1]
    assert code == 0, d
    assert keys(d) == ['voltage', 'ovp']
    assert d['settled'] is True and d['status']['vout_v'] == pytest.approx(5.0, abs=0.05)
    st = simstate()
    assert st['vset'] == 500 and st['ovp'] == 600 and st['alarm'] is None and st['on_cmd'] is True


def test_set_lowering_iset_and_ocp_under_load_sends_iset_first(cli, clock, simstate):
    simstate(load_ohm=10)                            # 5 V into 10 ohm: 0.5 A
    output_up(cli, clock, v=5, i=1, ovp=6)
    assert cli('status')[1][-1]['iout_a'] == pytest.approx(0.5, abs=0.01)
    code, docs = cli('set', '--current', '0.3', '--ocp', '0.4')
    d = docs[-1]
    assert code == 0, d
    assert keys(d) == ['current', 'ocp']
    st = simstate()
    assert st['iset'] == 30 and st['ocp'] == 40 and st['alarm'] is None and st['on_cmd'] is True
    assert d['status']['mode'] == 'CC' and d['status']['iout_a'] == pytest.approx(0.3, abs=0.01)


def test_set_no_wait_with_output_on(cli, clock):
    output_up(cli, clock, v=5, ovp=9)
    code, docs = cli('set', '--voltage', '6', '--no-wait')
    d = docs[-1]
    assert code == 0 and 'settled' not in d and d['status']['output_on'] is True


def test_set_refuses_ovp_too_close_to_vset_in_one_call(cli, simstate):
    before = frames(simstate)
    code, docs = cli('set', '--voltage', '5', '--ovp', '5.19')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and '5.2' in e['hint']
    assert frames(simstate) == before and state.last_sent('SIM') == {}
    assert cli('set', '--voltage', '5', '--ovp', '5.2')[0] == 0             # exactly VSET + 0.2 V
    assert cli('set', '--voltage', '20', '--ovp', '20.29')[0] == 7          # 1.5 % above 13.3 V
    assert cli('set', '--voltage', '20', '--ovp', '20.3')[0] == 0
    assert cli('set', '--voltage', '30', '--ovp', '30.5')[0] == 0           # the maximum still fits


def test_set_refuses_vset_above_or_too_close_to_the_ovp_sent_last(cli, simstate):
    assert cli('set', '--ovp', '6')[0] == 0
    for v in ('5.9', '7'):
        code, docs = cli('set', '--voltage', v)
        e = err(docs)
        assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'OVP sent last' in e['message'], e
        assert '--ovp' in e['hint']
    assert simstate()['vset'] == 1200 and 'voltage' not in state.last_sent('SIM')
    assert cli('set', '--voltage', '5.8')[0] == 0


def test_set_refuses_ovp_too_close_to_the_vset_sent_last(cli, simstate):
    assert cli('set', '--voltage', '5')[0] == 0
    code, docs = cli('set', '--ovp', '5.1')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'VSET sent last' in e['message']
    assert simstate()['ovp'] == 3050
    assert cli('set', '--voltage', '4', '--ovp', '5.1')[0] == 0               # lowering VSET in the same call


def test_set_refuses_ovp_below_the_present_output(cli, clock, simstate):
    simstate(vset=1200, ovp=3050)                   # 12 V set on the front panel: unknown to the tool
    assert cli('on', '--no-wait')[0] == 0
    clock.advance(2.0)
    assert cli('status')[1][-1]['vout_v'] == pytest.approx(12.0, abs=0.05)
    code, docs = cli('set', '--ovp', '10')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'delivers' in e['message'], e
    st = simstate()
    assert st['ovp'] == 3050 and st['alarm'] is None and st['on_cmd'] is True


def test_set_refuses_ocp_below_the_present_current(cli, clock, simstate):
    simstate(load_ohm=10)
    output_up(cli, clock, v=5, i=1, ovp=6)
    code, docs = cli('set', '--ocp', '0.4')
    assert code == 7 and err(docs)['code'] == 'UNSAFE_OPERATION'
    st = simstate()
    assert st['ocp'] == 1050 and st['alarm'] is None and st['on_cmd'] is True


def test_set_respects_bench_limits(cli, simstate):
    assert cli('limits', '--max-voltage', '12', '--max-current', '1', '--note', 'LED strip')[0] == 0
    for args in (('--voltage', '12.5', '--ovp', '13'), ('--ovp', '12.1'), ('--current', '1.5'), ('--ocp', '1.01')):
        code, docs = cli('set', *args)
        e = err(docs)
        assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'bench' in e['message'], (args, e)
        assert 'LED strip' in e['hint']
    st = simstate()
    assert (st['vset'], st['ovp'], st['iset'], st['ocp']) == (1200, 3050, 1010, 1050)
    assert cli('set', '--voltage', '11.8', '--ovp', '12', '--current', '1', '--ocp', '1')[0] == 0


@pytest.mark.parametrize('args', [
    ('--voltage', '30.01'), ('--voltage', '-0.01'), ('--voltage', 'nan'), ('--voltage', 'inf'), ('--voltage', 'abc'),
    ('--current', '10.11'), ('--ovp', '30.51'), ('--ocp', '10.51'), ('--delay', '10000'), ('--delay', '1.5'),
    ('--rev', '-1'), ('--opp', '5'), ('--opp', '0'), ('--opp', '320'), ('--opp', '105'), ('--otp', '50'),
    ('--otp', '110'), ('--otp', '65'), ('--priority', 'xx'), (),
    ('--current', '0'), ('--ovp', '0.49'), ('--ocp', '0.09'),            # below the front-panel minimums
])
def test_set_invalid_values_are_invalid_arg(cli, simstate, args):
    before = frames(simstate)
    code, docs = cli('set', *args)
    e = err(docs)
    assert code == 2 and e['code'] == 'INVALID_ARG' and e['message'], e
    assert frames(simstate) == before


def test_set_front_panel_minimums_are_accepted(cli, simstate):
    code, docs = cli('set', '--current', '0.01', '--ocp', '0.1', '--ovp', '0.5')
    assert code == 0 and 'warnings' not in docs[-1], docs
    st = simstate()
    assert (st['iset'], st['ocp'], st['ovp'], st['alarm']) == (1, 10, 50, None)
    for args in (('--current', '0'), ('--ovp', '0.49'), ('--ocp', '0.09')):
        assert cli('list', 'set', '0', *args)[0] == 2, args
    assert cli('list', 'set', '0', '--current', '0.01', '--ocp', '0.1', '--ovp', '0.5')[0] == 0


def test_set_range_error_has_a_hint(cli):
    e = err(cli('set', '--voltage', '31')[1])
    assert e['hint'] == 'voltage: 0..30 V'
    assert 'OPP' in err(cli('set', '--opp', '15')[1])['hint']


def test_set_warns_when_ocp_is_below_iset(cli):
    """OCP must sit at least max(0.05 A, 2 %) above ISET (given now or sent last), else a warning."""
    code, docs = cli('set', '--current', '2', '--ocp', '1')
    assert code == 0 and any('OCP 1 A is not above ISET 2 A' in w for w in docs[-1]['warnings'])
    code, docs = cli('set', '--current', '2', '--ocp', '2.04')                 # within the 0.05 A headroom
    assert code == 0 and any('OCP 2.04 A is not above ISET 2 A' in w for w in docs[-1]['warnings'])
    code, docs = cli('set', '--ocp', '1.5')                                    # against the ISET sent last
    assert code == 0 and any('OCP 1.5 A is not above ISET 2 A' in w for w in docs[-1]['warnings'])
    code, docs = cli('set', '--ocp', '2.05')
    assert code == 0 and 'warnings' not in docs[-1]


def test_set_not_applied_on_the_chinese_screen_with_output_on(cli, clock, simstate):
    simstate(screen='chinese')                       # list 0: 5 V, OVP 30.5 V
    assert cli('on', '--no-wait')[0] == 0
    clock.advance(2.0)
    assert cli('status')[1][-1]['vout_v'] == pytest.approx(5.0, abs=0.05)
    code, docs = cli('set', '--voltage', '8', '--ovp', '9')
    e = err(docs)
    assert code == 5 and e['code'] == 'NOT_APPLIED', e
    assert e['status']['vout_v'] == pytest.approx(5.0, abs=0.05)
    assert simstate()['alarm'] is None


# ------------------------------------------------------------------ on / off

def test_on_follows_the_soft_start_then_off(cli, simstate):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    code, docs = cli('on')
    d = docs[-1]
    assert code == 0 and d['ok'] and d['cmd'] == 'on' and d['settled'] is True, d
    s = d['sent'][0]
    assert s['key'] == 'output' and s['value'] is True and s['raw'] == 1 and s['id'] == '0x08'
    assert 1.0 <= d['on_after_s'] <= 3.0              # the ~1.1 s soft start (CC flag set) is not "up"
    assert d['status']['mode'] == 'CV' and d['status']['vout_v'] == pytest.approx(5.0, abs=0.05)
    assert 'note' not in d
    code, docs = cli('off')
    d = docs[-1]
    assert code == 0 and d['cmd'] == 'off' and d['settled'] is True and 'off_after_s' in d, d
    assert d['sent'][0]['value'] is False and d['sent'][0]['raw'] == 0
    assert d['status']['output_on'] is False and d['status']['vout_v'] < 0.05
    assert simstate()['on_cmd'] is False


def test_on_settles_fast_when_the_virtual_clock_skips_the_soft_start(cli, clock):
    """A CV reading is trusted only SOFT_START_S - 0.5 s (PC clock) after the ON frame (it keeps the bench limit
    check running through the soft start); with the soft start skipped on the virtual clock, `on` returns right
    after that window instead of waiting for the full soft start or the timeout."""
    from openaxpp300s import device
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    clock.jump_on(2.0)
    code, docs = cli('on')
    d = docs[-1]
    assert code == 0 and d['settled'] is True, d
    assert device.SOFT_START_S - 0.5 <= d['on_after_s'] < device.SOFT_START_S, d


def test_on_without_known_vset_notes_it(cli, clock):
    clock.jump_on(2.0)
    code, docs = cli('on')
    d = docs[-1]
    assert code == 0 and d['settled'] is True and 'VSET unknown' in d['note']
    assert d['status']['vout_v'] == pytest.approx(12.0, abs=0.05)    # first-boot VSET of the virtual supply


def test_on_off_no_wait(cli, simstate):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    code, docs = cli('on', '--no-wait')
    d = docs[-1]
    assert code == 0 and 'settled' not in d and d['status']['output_on'] is True
    assert d['status']['mode'] == 'CC'               # soft start: byte 11 reads 1
    code, docs = cli('off', '--no-wait')
    assert code == 0 and docs[-1]['status']['output_on'] is False and simstate()['on_cmd'] is False


def test_on_with_delay_follows_the_virtual_clock(cli, clock):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6', '--delay', '5')[0] == 0
    code, docs = cli('on', '--no-wait')
    assert code == 0 and docs[-1]['status']['output_on'] is False
    clock.advance(3.0)
    assert cli('status')[1][-1]['output_on'] is False
    clock.advance(3.5)
    d = cli('status')[1][-1]
    assert d['output_on'] is True and d['vout_v'] == pytest.approx(5.0, abs=0.05)


def test_on_waits_through_delay(cli, clock):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6', '--delay', '5')[0] == 0
    clock.jump_on(6.5)
    code, docs = cli('on')
    d = docs[-1]
    assert code == 0 and d['settled'] is True and d['status']['vout_v'] == pytest.approx(5.0, abs=0.05), d


def test_on_wait_timeout_reports_not_settled_during_delay(cli):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6', '--delay', '5')[0] == 0
    t0 = time.monotonic()
    code, docs = cli('on', '--wait-timeout', '0.3')
    d = docs[-1]
    assert code == 0 and d['settled'] is False and 'DELAY' in d['note'] and d['status']['output_on'] is False
    assert time.monotonic() - t0 < 2.0


def test_on_limit_safety_net_switches_the_output_off_again(cli, clock, simstate):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '5.5')[0] == 0
    assert cli('limits', '--max-voltage', '6')[0] == 0
    simstate(vset=1200, ovp=3050)                   # changed on the front panel
    clock.jump_on(2.0)
    code, docs = cli('on')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'switched off again' in e['message'], e
    assert e['status']['output_on'] is False
    st = simstate()
    assert st['on_cmd'] is False and st['alarm'] is None
    assert state.last_sent('SIM')['output']['value'] is False


def test_on_refuses_when_the_last_vset_is_above_the_limit(cli, simstate):
    assert cli('set', '--voltage', '8', '--current', '1', '--ovp', '9')[0] == 0
    assert cli('limits', '--max-voltage', '6')[0] == 0
    before = frames(simstate)
    code, docs = cli('on')
    e = err(docs)
    assert code == 7 and 'bench voltage limit' in e['message']
    assert simstate()['on_cmd'] is False and frames(simstate) == before


def test_on_refuses_a_vset_ovp_pair_without_headroom(cli, simstate):
    state.record_sent('SIM', 'voltage', 5.0, 500)
    state.record_sent('SIM', 'ovp', 5.1, 510)
    before = frames(simstate)
    code, docs = cli('on')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'would trip' in e['message']
    assert '--ovp 5.2' in e['hint']
    st = simstate()
    assert st['on_cmd'] is False and st['alarm'] is None and st['frames'] == before


def test_on_wait_timeout_must_be_positive(cli, simstate):
    before = frames(simstate)
    for v in ('0', '-1'):
        code, docs = cli('on', '--wait-timeout', v)
        assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert frames(simstate) == before and simstate()['on_cmd'] is False


def test_on_no_wait_with_a_limit_warns_not_monitored(cli):
    assert cli('set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    assert cli('limits', '--max-voltage', '12')[0] == 0
    d = cli('on', '--no-wait')[1][-1]
    assert any('not monitored' in w for w in d['warnings'])


# ------------------------------------------------------------------ list

def test_list_select(cli, simstate):
    code, docs = cli('list', 'select', '3')
    d = docs[-1]
    assert code == 0 and d['cmd'] == 'list select' and d['list'] == 3 and 'status' in d
    assert keys(d) == ['list'] and d['sent'][0]['raw'] == 3 and d['sent'][0]['id'] == '0x09'
    assert simstate()['list_sel'] == 3
    for bad in ('5', '-1', 'x'):
        assert cli('list', 'select', bad)[0] == 2
    assert simstate()['list_sel'] == 3


def test_list_set_with_select_and_cut_time(cli, simstate):
    code, docs = cli('list', 'set', '1', '--select', '--voltage', '9', '--current', '1', '--ovp', '10', '--ocp', '1.5',
                     '--cut-current', '0.1', '--cut-time', '1h30m')
    d = docs[-1]
    assert code == 0 and d['cmd'] == 'list set' and d['list'] == 1, d
    # values first, the list selection last; the cut-off time as 99 h, min, s, then the real hours
    assert keys(d) == ['list1_ovp', 'list1_ocp', 'list1_voltage', 'list1_current', 'list1_cut_current',
                       'list1_cut_time', 'list1_cut_time', 'list1_cut_time', 'list1_cut_time', 'list']
    cut = [s for s in d['sent'] if s['key'] == 'list1_cut_time']
    assert [c['value'] for c in cut] == ['99 h', '30 min', '0 s', '1 h']
    assert [c['raw'] for c in cut] == [0x0263, 0x011E, 0x0000, 0x0201]
    st = simstate()
    L = st['lists'][1]
    assert (L['v'], L['i'], L['ovp'], L['ocp'], L['cut_i'], L['cut_h'], L['cut_m'], L['cut_s']) == \
        (900, 100, 1000, 150, 10, 1, 30, 0)                                  # seconds not given: 0
    assert st['list_sel'] == 1 and st['lists'][0]['v'] == 500 and st['vset'] == 1200
    ls = state.last_sent('SIM')
    assert ls['list1_voltage']['value'] == 9.0 and ls['list']['value'] == 1
    assert ls['list1_cut_time']['value'] == '1:30:00' and ls['list1_cut_time']['raw'] is None


def test_list_set_select_only(cli, simstate):
    code, docs = cli('list', 'set', '2', '--select')
    assert code == 0 and keys(docs[-1]) == ['list'] and simstate()['list_sel'] == 2


@pytest.mark.parametrize('text,hms', [
    ('60s', (0, 0, 60)), ('2:15:30', (2, 15, 30)), ('45m', (0, 45, 0)), ('2h', (2, 0, 0)), ('1h0m5s', (1, 0, 5)),
    ('61h', (61, 0, 0)), ('99:60:60', (99, 60, 60)), ('off', (99, 60, 60)), ('99h', (99, 0, 0)),
])
def test_list_cut_time_forms(cli, simstate, text, hms):
    """Fields not given are 0; the hours go to 99 first (skipped when the new hours are 99), then minutes, seconds
    and the real hours."""
    h, m, s = hms
    code, docs = cli('list', 'set', '2', '--cut-time', text)
    assert code == 0, docs
    sent = docs[-1]['sent']
    expect_values = ([] if h == 99 else ['99 h']) + ['%d min' % m, '%d s' % s, '%d h' % h]
    assert [x['key'] for x in sent] == ['list2_cut_time'] * len(expect_values)
    assert [x['value'] for x in sent] == expect_values
    L = simstate()['lists'][2]
    assert (L['cut_h'], L['cut_m'], L['cut_s']) == hms
    rec = state.last_sent('SIM')['list2_cut_time']
    assert rec['value'] == '%d:%02d:%02d' % hms and rec['raw'] is None


@pytest.mark.parametrize('text', ['100s', '61s', '90s', '61m', '100h', 'abc', '1:2', '1:2:x', '', '0s', '0:0:0'])
def test_list_cut_time_invalid(cli, simstate, text):
    before = frames(simstate)
    code, docs = cli('list', 'set', '0', '--cut-time', text)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert frames(simstate) == before


def test_list_set_validation_and_guards(cli, simstate):
    assert cli('list', 'set', '0')[0] == 2                                   # nothing to set
    assert cli('list', 'set', '5', '--voltage', '5')[0] == 2                 # lists 0..4
    assert cli('list', 'set', '0', '--cut-current', '10.01')[0] == 2
    assert cli('list', 'set', '0', '--voltage', '30.01')[0] == 2
    assert cli('list', 'set', '0', '--voltage', '5', '--ovp', '5.1')[0] == 7  # headroom in one call
    assert cli('list', 'set', '0', '--ovp', '6')[0] == 0
    code, docs = cli('list', 'set', '0', '--voltage', '6')                   # against the list OVP sent last
    assert code == 7 and 'OVP sent last' in err(docs)['message']
    assert cli('list', 'set', '1', '--voltage', '6')[0] == 0                 # another list: its own OVP
    assert cli('set', '--voltage', '6')[0] == 0                              # Digital screen: its own OVP
    assert cli('limits', '--max-voltage', '10', '--max-current', '1')[0] == 0
    assert cli('list', 'set', '3', '--voltage', '12', '--ovp', '13')[0] == 7
    assert cli('list', 'set', '3', '--ocp', '2')[0] == 7
    assert simstate()['lists'][3] == sim.default_state()['lists'][3]


def test_list_select_refuses_a_list_whose_vset_is_above_the_limit(cli, simstate):
    assert cli('list', 'set', '4', '--voltage', '20', '--ovp', '21')[0] == 0
    assert cli('limits', '--max-voltage', '12')[0] == 0
    code, docs = cli('list', 'select', '4')
    assert code == 7, docs
    assert simstate()['list_sel'] == 0


# ------------------------------------------------------------------ colours

def test_color_set(cli, simstate):
    code, docs = cli('color', 'set', 'vout=#FF0000', 'IOUT=green', 'bar_select=0xF800')
    d = docs[-1]
    assert code == 0 and d['cmd'] == 'color set', d
    # every colour, then (after the EEPROM save time) the last one again
    assert keys(d) == ['color_vout', 'color_iout', 'color_bar_select', 'color_bar_select']
    assert [s['value'] for s in d['sent']] == ['0xF800', '0x07E0', '0xF800', '0xF800']
    assert d['sent'][0]['rgb'] == '#F80000' and d['sent'][0]['id'] == '0x50'
    assert 'note' in d['sent'][-1] and all('note' not in s for s in d['sent'][:-1])
    colors = simstate()['colors']
    assert colors[0] == 0xF800 and colors[1] == 0x07E0 and colors[9] == 0xF800
    assert colors[2] == P.COLOR_DEFAULTS['pout']


def test_color_reset_and_list(cli, simstate):
    assert cli('color', 'set', 'vout=red')[0] == 0
    d = cli('color', 'list')[1][-1]
    els = {e['element']: e for e in d['elements']}
    assert d['cmd'] == 'color list' and list(els) == list(P.COLOR_DEFAULTS)
    assert els['vout']['last_sent'] == '0xF800' and els['iout']['last_sent'] is None
    assert els['vout']['id'] == '0x50' and els['vout']['default'] == '0xFFE0' and els['vout']['doc']
    code, docs = cli('color', 'reset')
    sent = docs[-1]['sent']
    assert code == 0 and len(sent) == 11                    # ten colours, then the last one again
    assert sent[-1]['key'] == sent[-2]['key'] == 'color_' + list(P.COLOR_DEFAULTS)[-1] and 'note' in sent[-1]
    assert simstate()['colors'] == list(P.COLOR_DEFAULTS.values())
    d = cli('color', 'list')[1][-1]
    assert all(e['last_sent'] == e['default'] for e in d['elements'])


@pytest.mark.parametrize('assign', ['bogus=red', 'vout', 'vout=notacolour', 'vout=0x10000', 'vout=#12345'])
def test_color_set_invalid(cli, simstate, assign):
    before = frames(simstate)
    code, docs = cli('color', 'set', assign)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert frames(simstate) == before


def test_color_needs_a_subcommand(cli):
    assert cli('color')[0] == 2
    assert cli('color', 'set')[0] == 2


# ------------------------------------------------------------------ watch

SAMPLE_KEYS = {'type', 't', 'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'mode', 'output_on', 'latency_ms'}


def test_watch_count_prints_jsonl_samples_and_a_summary(cli):
    code, docs = cli('watch', '-n', '3', '--every', '0.05')
    assert code == 0 and len(docs) == 4                     # 3 queries: no EEPROM warning line
    assert 'warnings' not in docs[-1]
    samples = docs[:3]
    assert all(s['type'] == 'sample' and SAMPLE_KEYS <= set(s) for s in samples)
    assert [s['t'] for s in samples] == sorted(s['t'] for s in samples)
    done = docs[-1]
    assert done['ok'] and done['cmd'] == 'watch' and done['event'] == 'done'
    assert done['stopped_by'] == 'count' and done['samples'] == 3 and done['modes'] == {'OFF': 3}
    assert done['last'] == {k: v for k, v in samples[-1].items() if k != 'type'}
    assert set(done['stats']) == {'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c'}
    assert done['ah'] == 0 and done['wh'] == 0
    assert cli('running')[1][-1]['running'] == []


def test_watch_count_long_option_and_default(cli):
    code, docs = cli('watch', '--count', '2', '--every', '0.05')
    assert code == 0 and docs[-1]['samples'] == 2
    code, docs = cli('watch', '--every', '0.05')
    assert code == 0 and docs[-1]['samples'] == 10 and len(docs) == 11


def test_watch_default_every_is_1_s(cli, monkeypatch):
    from openaxpp300s import runner
    seen = {}

    def fake_watch(sup, emit, every=None, **kw):
        seen['every'] = every
        return {'stopped_by': 'count', 'samples': 0}
    monkeypatch.setattr(runner, 'watch', fake_watch)
    assert cli('watch', '-n', '1')[0] == 0 and seen['every'] == 1.0


def test_watch_for_duration(cli):
    t0 = time.monotonic()
    code, docs = cli('watch', '--for', '0.3s', '--every', '0.1')
    d = docs[-1]
    assert code == 0 and d['stopped_by'] == 'duration' and 2 <= d['samples'] <= 6 and d['elapsed_s'] >= 0.3
    assert time.monotonic() - t0 < 3


def test_watch_quiet_prints_only_the_summary(cli):
    code, docs = cli('watch', '-n', '3', '--every', '0.05', '--quiet')
    assert code == 0 and len(docs) == 1 and docs[0]['samples'] == 3


def test_watch_until_stop_request(cli):
    th = stop_when_running('watch')
    code, docs = cli('watch', '--for', 'forever', '--every', '0.05')
    th.join(5)
    d = docs[-1]
    assert code == 0 and d['stopped_by'] == 'stop' and d['samples'] >= 1
    assert state.list_running() == [] and not state.stop_requested()
    # unbounded: the first line is the EEPROM warning, and the summary repeats it
    assert docs[0]['event'] == 'warning' and any('unbounded' in w for w in docs[0]['warnings'])
    assert d['warnings'] == docs[0]['warnings']


def test_watch_eeprom_warning_above_3600_queries(cli):
    th = stop_when_running('watch')
    code, docs = cli('watch', '-n', '3601', '--every', '0.05')
    th.join(5)
    assert code == 0 and docs[0]['event'] == 'warning' and any('3601' in w for w in docs[0]['warnings'])
    assert docs[-1]['stopped_by'] == 'stop' and 'warnings' in docs[-1]
    th = stop_when_running('watch')
    code, docs = cli('watch', '-n', '3601', '--every', '0.05', '--quiet')     # quiet: only in the summary
    th.join(5)
    assert code == 0 and len(docs) == 1 and 'warnings' in docs[0]
    code, docs = cli('watch', '-n', '2', '--every', '0.05')                   # few queries: no warning
    assert code == 0 and all(d.get('event') != 'warning' for d in docs) and 'warnings' not in docs[-1]


def test_watch_link_lost(cli, simstate):
    simstate(powered=False)
    code, docs = cli('watch', '--for', 'forever', '--every', '0.05', '--reconnect-for', '0.1', '--timeout', '0.05',
                     '--retries', '0')
    assert code == 0
    lost = [d for d in docs if d.get('event') == 'link_lost']
    assert len(lost) == 1 and lost[0]['code'] == 'NO_REPLY'
    assert docs[-1]['stopped_by'] == 'link_lost' and docs[-1]['samples'] == 0


def test_watch_reconnects_after_a_gap(cli, simstate, monkeypatch):
    simstate(powered=False)
    orig = sim.receive
    calls = [0]

    def receive(burst, t_wall, pending):          # the supply comes back after two unanswered queries
        calls[0] += 1
        if calls[0] == 3:
            st = sim.load()
            st['powered'] = True
            sim.save(st)
        return orig(burst, t_wall, pending)
    monkeypatch.setattr(sim, 'receive', receive)
    code, docs = cli('watch', '-n', '2', '--every', '0.05', '--reconnect-for', '10', '--timeout', '0.05',
                     '--retries', '0')
    events = [d.get('event') or d.get('type') for d in docs]
    assert code == 0, docs
    assert events[0] == 'link_lost' and 'reconnected' in events and events.count('sample') == 2
    assert docs[-1]['stopped_by'] == 'count'


@pytest.mark.parametrize('args', [('--for', 'soon'), ('--reconnect-for', 'x'), ('--count', 'two'), ('--every', 'x'),
                                  ('--every', '0'), ('--every', '0.04'), ('--every', 'nan'), ('--count', '0'),
                                  ('--timeout', '0'), ('--retries', '-1'), ('--lock-wait', '-1')])
def test_watch_invalid_options(cli, args):
    code, docs = cli('watch', *args)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert state.list_running() == []


# ------------------------------------------------------------------ log

def test_log_csv_columns_and_ah_wh(cli, clock, simstate, tmp_path):
    simstate(load_ohm=10)                            # 5 V into 10 ohm: 0.5 A, 2.5 W
    output_up(cli, clock, v=5, i=1, ovp=6)
    out = tmp_path / 'log.csv'
    code, docs = cli('log', '--out', str(out), '--duration', '0.6', '--every', '0.1')
    assert code == 0, docs
    started, done = docs[0], docs[-1]
    assert started['event'] == 'started' and started['format'] == 'csv' and started['port'] == 'SIM'
    assert started['pid'] == os.getpid()
    assert started['file'] == os.path.abspath(str(out)) and started['every_s'] == 0.1
    assert started['duration_s'] == 0.6 and 'warnings' not in started
    assert done['ok'] and done['cmd'] == 'log' and done['event'] == 'done' and done['stopped_by'] == 'duration'
    assert done['format'] == 'csv' and done['rows'] == done['samples'] >= 4
    with open(out, encoding='utf-8', newline='') as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ['pc_time', 'local_time', 'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'mode', 'output_on',
                       'ah', 'wh', 'step']
    body = rows[1:]
    assert len(body) == done['rows']
    for r in body:
        assert float(r[2]) == pytest.approx(5.0, abs=0.05) and float(r[3]) == pytest.approx(0.5, abs=0.01)
        assert float(r[4]) == pytest.approx(2.5, abs=0.05) and r[7] == 'CV' and r[8] == '1' and r[11] == ''
    ah = [float(r[9]) for r in body]
    assert ah == sorted(ah) and ah[-1] == pytest.approx(done['ah'], abs=1e-6)
    assert float(body[-1][10]) == pytest.approx(done['wh'], abs=1e-6)
    assert done['on_s'] > 0.3
    assert done['ah'] == pytest.approx(0.5 * done['on_s'] / 3600, rel=0.05, abs=2e-6)
    assert done['wh'] == pytest.approx(2.5 * done['on_s'] / 3600, rel=0.05, abs=2e-6)


def test_log_jsonl(cli, tmp_path):
    out = tmp_path / 'sub' / 'log.jsonl'             # the folder is created
    code, docs = cli('log', '--out', str(out), '--duration', '0.3', '--every', '0.1')
    assert code == 0 and docs[-1]['format'] == 'jsonl'
    lines = [json.loads(x) for x in out.read_text(encoding='utf-8').splitlines()]
    assert lines[0]['event'] == 'started' and lines[-1]['event'] == 'done'
    samples = lines[1:-1]
    assert len(samples) == docs[-1]['rows'] >= 2
    need = {'t', 'local_time', 'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'mode', 'output_on', 'latency_ms',
            'ah', 'wh'}
    assert all(need <= set(s) for s in samples)


def test_log_format_option_overrides_the_extension(cli, tmp_path):
    out = tmp_path / 'log.txt'
    code, docs = cli('log', '--out', str(out), '--format', 'jsonl', '--duration', '0.1', '--every', '0.05')
    assert code == 0 and docs[-1]['format'] == 'jsonl'
    assert json.loads(out.read_text(encoding='utf-8').splitlines()[0])['event'] == 'started'
    out2 = tmp_path / 'log.jsonl'
    code, docs = cli('log', '-o', str(out2), '--format', 'csv', '--duration', '0.1', '--every', '0.05')
    assert code == 0 and out2.read_text(encoding='utf-8').startswith('pc_time,')


def test_log_default_file_in_the_data_folder(cli, isolated):
    code, docs = cli('log', '--duration', '0.1', '--every', '0.05')
    assert code == 0
    f = docs[-1]['file']
    assert os.path.exists(f) and f.endswith('.csv')
    assert os.path.dirname(f) == os.path.join(str(isolated / 'home'), 'data')


def test_log_status_every(cli, tmp_path):
    code, docs = cli('log', '--out', str(tmp_path / 'l.csv'), '--duration', '0.4', '--every', '0.05',
                     '--status-every', '0.1s')
    st = [d for d in docs if d.get('event') == 'status']
    assert code == 0 and len(st) >= 2
    assert all({'rows', 'elapsed_s', 'ah', 'wh', 'last'} <= set(s) for s in st)


def test_log_stop_request_from_a_thread_and_eeprom_warning(cli, tmp_path):
    th = stop_when_running('log')
    code, docs = cli('log', '--out', str(tmp_path / 'a.csv'), '--every', '0.05')
    th.join(5)
    started = docs[0]
    assert code == 0 and docs[-1]['stopped_by'] == 'stop' and docs[-1]['rows'] >= 1
    assert started['duration_s'] is None
    assert any('EEPROM' in w and 'unbounded' in w for w in started['warnings'])
    th = stop_when_running('log')
    code, docs = cli('log', '--out', str(tmp_path / 'b.csv'), '--every', '1', '--duration', '2h')
    th.join(5)
    assert code == 0 and docs[-1]['stopped_by'] == 'stop'
    assert any('EEPROM' in w and '7200 frames planned' in w for w in docs[0]['warnings'])
    th = stop_when_running('log')
    code, docs = cli('log', '--out', str(tmp_path / 'c.csv'), '--every', '1', '--duration', '1h')   # 3600: fine
    th.join(5)
    assert code == 0 and docs[-1]['stopped_by'] == 'stop' and 'warnings' not in docs[0]


def test_log_link_lost(cli, simstate, tmp_path):
    simstate(powered=False)
    out = tmp_path / 'lost.jsonl'
    code, docs = cli('log', '--out', str(out), '--every', '0.05', '--reconnect-for', '0.1', '--timeout', '0.05',
                     '--retries', '0')
    assert code == 0, docs
    assert any(d.get('event') == 'link_lost' and d['code'] == 'NO_REPLY' for d in docs)
    assert docs[-1]['stopped_by'] == 'link_lost' and docs[-1]['rows'] == 0
    events = [json.loads(x)['event'] for x in out.read_text(encoding='utf-8').splitlines()]
    assert events == ['started', 'link_lost', 'done']


def test_log_bounded_short_run_has_no_eeprom_warning(cli, tmp_path):
    code, docs = cli('log', '--out', str(tmp_path / 'c.csv'), '--every', '0.1', '--duration', '0.2')
    assert code == 0 and 'warnings' not in docs[0]


@pytest.mark.parametrize('args', [('--duration', 'soon'), ('--status-every', 'x'), ('--reconnect-for', 'x'),
                                  ('--format', 'xml'), ('--every', '0.04')])
def test_log_invalid_options(cli, tmp_path, args):
    code, docs = cli('log', '--out', str(tmp_path / 'x.csv'), *args)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert state.list_running() == []


# ------------------------------------------------------------------ sequence

@pytest.mark.parametrize('encoding', ['utf-8-sig', 'gbk'])
def test_sequence_vendor_csv_dry_run(cli, simstate, tmp_path, encoding):
    f = tmp_path / '序列模板.csv'
    write_vendor_csv(f, [('5.00', '2.00', '10'), ('9.00', '1.00', '20')], encoding)
    before = frames(simstate)
    code, docs = cli('sequence', str(f), '--dry-run', '--repeat', '3')
    d = docs[-1]
    assert code == 0 and len(docs) == 1 and d['dry_run'] is True and d['cmd'] == 'sequence', d
    assert [(s['voltage'], s['current'], s['seconds']) for s in d['steps']] == [(5.0, 2.0, 10.0), (9.0, 1.0, 20.0)]
    assert d['total_s'] == 90
    assert any('OVP unknown' in w for w in d['warnings'])
    assert frames(simstate) == before


@pytest.mark.skipif(not os.path.exists(VENDOR_TEMPLATE), reason='vendor template not in this checkout')
def test_sequence_dry_run_of_the_vendor_template(cli):
    code, docs = cli('sequence', VENDOR_TEMPLATE, '--dry-run')
    d = docs[-1]
    assert code == 0 and len(d['steps']) == 10 and d['total_s'] == 190
    assert d['steps'][0] == {'voltage': 5.0, 'current': 2.0, 'seconds': 10.0, 'line': 2}
    assert d['steps'][-1]['voltage'] == 30.0


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'gbk'])
def test_sequence_runs_a_vendor_csv(cli, simstate, tmp_path, encoding):
    f = tmp_path / 'seq.csv'
    write_vendor_csv(f, [('5.00', '1.00', '0.1'), ('6.00', '0.50', '0.1')], encoding)
    assert cli('set', '--ovp', '7')[0] == 0
    code, docs = cli('sequence', str(f), '--every', '0.05')
    assert code == 0, docs
    assert docs[0]['event'] == 'started' and docs[0]['steps'] == 2 and docs[0]['end'] == 'off'
    assert 'warnings' not in docs[0] and docs[0]['pid'] == os.getpid()
    steps = [d for d in docs if d.get('event') == 'step']
    assert [(s['step'], s['voltage'], s['current']) for s in steps] == [(1, 5.0, 1.0), (2, 6.0, 0.5)]
    done = docs[-1]
    assert done['event'] == 'done' and done['stopped_by'] == 'end' and done['steps_done'] == 2
    assert done['cycles'] == 1 and done['end'] == 'output switched off' and done['samples'] >= 2
    st = simstate()
    assert st['vset'] == 600 and st['iset'] == 50 and st['on_cmd'] is False and st['alarm'] is None
    assert state.last_sent('SIM')['output']['value'] is False


def test_sequence_steps_and_repeat(cli, simstate):
    assert cli('set', '--ovp', '7')[0] == 0
    code, docs = cli('sequence', '--steps', '5,1,0.05; 6,0.5,0.05', '--repeat', '2', '--every', '0.05')
    steps = [(d['cycle'], d['step']) for d in docs if d.get('event') == 'step']
    assert code == 0 and steps == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert docs[-1]['cycles'] == 2 and docs[-1]['steps_done'] == 4 and simstate()['on_cmd'] is False


def test_sequence_end_keep_leaves_the_output_on(cli, simstate):
    assert cli('set', '--ovp', '7')[0] == 0
    code, docs = cli('sequence', '--steps', '5,1,0.05', '--end', 'keep', '--every', '0.05')
    assert code == 0 and docs[-1]['end'] == 'output left as it was' and docs[0]['end'] == 'keep'
    assert simstate()['on_cmd'] is True


def test_sequence_end_off_explicit(cli, simstate):
    assert cli('set', '--ovp', '7')[0] == 0
    code, docs = cli('sequence', '--steps', '5,1,0.05', '--end', 'off', '--every', '0.05')
    assert code == 0 and docs[-1]['end'] == 'output switched off' and simstate()['on_cmd'] is False


def test_sequence_stop_mid_run_switches_the_output_off(cli, simstate):
    assert cli('set', '--ovp', '7')[0] == 0
    th = stop_when_running('sequence', delay=0.3)
    t0 = time.monotonic()
    code, docs = cli('sequence', '--steps', '5,1,30', '--every', '0.05')
    th.join(5)
    done = docs[-1]
    assert code == 0 and done['stopped_by'] == 'stop' and done['steps_done'] == 0
    assert done['end'] == 'output switched off' and time.monotonic() - t0 < 10
    assert simstate()['on_cmd'] is False and state.last_sent('SIM')['output']['value'] is False


def test_sequence_repeat_0_runs_until_stop(cli, simstate):
    assert cli('set', '--ovp', '7')[0] == 0
    th = stop_when_running('sequence', delay=0.3)
    code, docs = cli('sequence', '--steps', '5,1,0.05; 6,1,0.05', '--repeat', '0', '--every', '0.05')
    th.join(5)
    assert code == 0 and docs[0]['repeat'] == 0
    assert any('unbounded' in w for w in docs[0]['warnings'])
    assert docs[-1]['stopped_by'] == 'stop' and simstate()['on_cmd'] is False


def after_frames(monkeypatch, hook):
    """Call hook(frame bytes, counts) right after the virtual supply handled each frame from the host; counts has
    'on' (output-ON frames so far) and 'queries' (status queries since the first ON)."""
    orig = sim.receive
    counts = {'on': 0, 'queries': 0}

    def receive(burst, t_wall, pending):
        out = orig(burst, t_wall, pending)
        b = bytes(burst)
        if len(b) == 6 and b[0] == P.FRAME_HEAD:
            if b[1] == 0x08 and b[2] != 0:
                counts['on'] += 1
            elif b[1] == P.QUERY_ID and counts['on']:
                counts['queries'] += 1
            hook(b, counts)
        return out
    monkeypatch.setattr(sim, 'receive', receive)
    return counts


def test_sequence_ends_when_the_output_goes_off_by_itself(cli, simstate, monkeypatch):
    """An output that went off without the sequence switching it off (front panel, trip) ends the run with
    stopped_by 'output_off'; it is not switched on again."""
    assert cli('set', '--ovp', '7')[0] == 0

    def hook(b, counts):
        if b[1] == P.QUERY_ID and counts['queries'] == 2:
            st = sim.load()
            st['on_cmd'] = False                       # switched off on the front panel
            sim.save(st)
    counts = after_frames(monkeypatch, hook)
    code, docs = cli('sequence', '--steps', '5,1,0.2; 6,1,0.2', '--every', '0.05')
    done = docs[-1]
    assert code == 0 and done['ok'] and done['stopped_by'] == 'output_off', docs
    assert done['steps_done'] == 1 and done['end'] == 'output switched off'
    assert any(d.get('event') == 'output_off' and d['step'] == 2 for d in docs)
    assert counts['on'] == 1                           # no re-ON
    st = simstate()
    assert st['on_cmd'] is False and st['alarm'] is None


def test_sequence_over_the_voltage_limit_switches_off_and_prints_the_error(cli, simstate, monkeypatch, clock):
    """Every sample is checked against the bench voltage limit; the long-command wrapper prints the error as the
    'done' line with the end state, and exits with the error's code."""
    assert cli('set', '--ovp', '13')[0] == 0
    assert cli('limits', '--max-voltage', '6')[0] == 0

    def hook(b, counts):
        if b[1] == 0x08 and b[2] != 0:
            clock.advance(2.0)                         # skip the soft start
        elif b[1] == P.QUERY_ID and counts['queries'] == 2:
            st = sim.load()
            st['vset'] = 1200                          # changed on the front panel
            sim.save(st)
    after_frames(monkeypatch, hook)
    code, docs = cli('sequence', '--steps', '5,1,5', '--every', '0.05')
    d = docs[-1]
    assert code == 7 and d['cmd'] == 'sequence' and d['event'] == 'done', docs
    e = err(docs)
    assert e['code'] == 'UNSAFE_OPERATION' and 'bench voltage limit' in e['message']
    assert (e['stopped_by'], e['steps_done'], e['cycles'], e['end']) == ('error', 0, 1, 'output switched off')
    assert simstate()['on_cmd'] is False and state.last_sent('SIM')['output']['value'] is False
    assert state.list_running() == []


def test_sequence_refuses_a_step_without_ovp_headroom_before_sending(cli, simstate):
    assert cli('set', '--ovp', '6')[0] == 0
    before = frames(simstate)
    code, docs = cli('sequence', '--steps', '5,1,0.1; 6,1,0.1')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and e['message'].startswith('step 2'), e
    assert '--ovp 6.21' in e['hint']
    assert frames(simstate) == before and 'voltage' not in state.last_sent('SIM')
    assert simstate()['on_cmd'] is False
    assert cli('sequence', '--steps', '5,1,0.1; 6,1,0.1', '--dry-run')[0] == 7


def test_sequence_respects_bench_limits(cli, simstate):
    assert cli('set', '--ovp', '9')[0] == 0
    assert cli('limits', '--max-voltage', '5.5', '--max-current', '0.8')[0] == 0
    before = frames(simstate)
    for steps in ('5,0.5,0.1; 6,0.5,0.1', '5,1,0.1'):
        code, docs = cli('sequence', '--steps', steps)
        assert code == 7 and err(docs)['code'] == 'UNSAFE_OPERATION', steps
    assert frames(simstate) == before


def test_sequence_warns_about_a_step_current_above_the_ocp(cli):
    assert cli('set', '--ovp', '7', '--ocp', '0.5')[0] == 0
    d = cli('sequence', '--steps', '5,1,1', '--dry-run')[1][-1]
    assert any('OCP' in w for w in d['warnings'])


@pytest.mark.parametrize('args', [
    (), ('--steps', ''), ('--steps', '5,1'), ('--steps', '5,1,x'), ('--steps', '5,1,-1'), ('--steps', '31,1,1'),
    ('--steps', '5,10.2,1'), ('--steps', '5,1,1', '--end', 'later'), ('--steps', '5,1,1', '--repeat', 'x'),
    ('--steps', '5,1,1', '--repeat', '-1'), ('--steps', '5,1,1', '--every', '0.04'),
])
def test_sequence_invalid(cli, simstate, args):
    before = frames(simstate)
    code, docs = cli('sequence', *args)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG', docs
    assert frames(simstate) == before


def test_sequence_csv_problems(cli, tmp_path):
    f = tmp_path / 'both.csv'
    write_vendor_csv(f, [('5', '1', '1')], 'utf-8-sig')
    assert cli('sequence', str(f), '--steps', '5,1,1')[0] == 2                # a file and --steps
    bad = tmp_path / 'bad.csv'
    bad.write_text(VENDOR_HEADER + '\n5,1,1\nfive,1,1\n', encoding='utf-8')
    code, docs = cli('sequence', str(bad), '--dry-run')
    assert code == 2 and 'line 3' in err(docs)['message']
    empty = tmp_path / 'empty.csv'
    empty.write_text(VENDOR_HEADER + '\n', encoding='utf-8')
    assert cli('sequence', str(empty), '--dry-run')[0] == 2
    short = tmp_path / 'short.csv'
    short.write_text('5,1\n', encoding='utf-8')
    assert cli('sequence', str(short), '--dry-run')[0] == 2


def test_sequence_log_file(cli, tmp_path):
    assert cli('set', '--ovp', '7')[0] == 0
    out = tmp_path / 'seq.csv'
    code, docs = cli('sequence', '--steps', '5,1,0.1; 6,1,0.1', '--every', '0.05', '--log', str(out))
    assert code == 0 and docs[0]['file'] == os.path.abspath(str(out))
    with open(out, encoding='utf-8', newline='') as fh:
        rows = list(csv.DictReader(fh))
    assert {r['step'] for r in rows} == {'1', '2'}
    assert docs[-1]['rows'] == len(rows) and docs[-1]['file'] == os.path.abspath(str(out))
    out2 = tmp_path / 'seq.txt'
    code, docs = cli('sequence', '--steps', '5,1,0.05', '--every', '0.05', '--log', str(out2), '--format', 'jsonl')
    lines = [json.loads(x) for x in out2.read_text(encoding='utf-8').splitlines()]
    assert code == 0 and any(x.get('event') == 'step' for x in lines) and lines[-1]['event'] == 'done'
    assert any(x.get('step') == 1 and 'vout_v' in x for x in lines)


# ------------------------------------------------------------------ stop / running

def test_stop_and_running_when_idle(cli):
    assert cli('running')[1][-1] == {'ok': True, 'cmd': 'running', 'running': []}
    d = cli('stop')[1][-1]
    assert d['ok'] and d['asked'] == [] and d['note'] == 'nothing running'


def test_stop_command_ends_a_log_running_in_another_process(cli, tmp_path):
    out = tmp_path / 'bg.csv'
    p = subprocess.Popen([sys.executable, '-m', 'openaxpp300s', 'log', '--out', str(out), '--every', '0.1'], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                         env=dict(os.environ))
    try:
        entry = None
        t_end = time.time() + 30
        while time.time() < t_end and entry is None:
            running = cli('running')[1][-1]['running']
            entry = next((r for r in running if r['cmd'] == 'log'), None)
            if entry is None:
                assert p.poll() is None, p.communicate()
                time.sleep(0.05)
        assert entry is not None, 'the background log never showed up in "running"'
        assert entry['argv'][0] == 'log' and isinstance(entry['started'], float)
        assert cli('stop', '--only', 'watch')[1][-1]['asked'] == []
        assert cli('stop', '--pid', str(entry['pid'] + 100000))[1][-1]['asked'] == []
        d = cli('stop', '--pid', str(entry['pid']))[1][-1]
        assert [(a['pid'], a['cmd']) for a in d['asked']] == [(entry['pid'], 'log')]
        assert 'stops within' in d['note']
        stdout, stderr = p.communicate(timeout=30)
        assert p.returncode == 0, stderr
        docs = [json.loads(x) for x in stdout.splitlines() if x.startswith('{')]
        assert docs[0]['event'] == 'started' and docs[-1]['event'] == 'done' and docs[-1]['stopped_by'] == 'stop'
        assert cli('running')[1][-1]['running'] == []
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()


def test_stop_only_option_is_validated(cli):
    assert cli('stop', '--only', 'status')[0] == 2
    assert cli('stop', '--pid', 'x')[0] == 2


# ------------------------------------------------------------------ limits

def test_limits_show_set_update_clear(cli, isolated):
    d = cli('limits')[1][-1]
    assert d['changed'] is False and d['cmd'] == 'limits'
    assert d['limits'] == {'max_voltage': None, 'max_current': None, 'note': None, 'updated': None}
    assert d['file'] == os.path.join(str(isolated / 'home'), 'state', 'limits.json')
    d = cli('limits', '--max-voltage', '12', '--max-current', '2', '--note', 'LED strip')[1][-1]
    assert d['changed'] is True and d['limits']['max_voltage'] == 12 and d['limits']['max_current'] == 2
    assert d['limits']['note'] == 'LED strip' and d['limits']['updated']
    d = cli('limits', '--note', 'motor')[1][-1]                       # a partial change keeps the rest
    assert d['limits']['max_voltage'] == 12 and d['limits']['note'] == 'motor'
    d = cli('limits')[1][-1]
    assert d['changed'] is False and d['limits']['max_current'] == 2
    with open(d['file'], encoding='utf-8') as fh:
        assert json.load(fh)['max_voltage'] == 12
    d = cli('limits', '--clear')[1][-1]
    assert d['changed'] is True
    assert (d['limits']['max_voltage'], d['limits']['max_current'], d['limits']['note']) == (None, None, None)
    d = cli('limits', '--clear', '--max-voltage', '5')[1][-1]
    assert d['limits']['max_voltage'] == 5 and d['limits']['max_current'] is None
    assert cli('limits', '--max-voltage', '0', '--max-current', '10.1')[0] == 0                  # the bounds


@pytest.mark.parametrize('args', [('--max-voltage', '30.01'), ('--max-voltage', '-1'), ('--max-voltage', 'nan'),
                                  ('--max-current', '10.2'), ('--max-current', '-0.1'), ('--max-voltage', 'x')])
def test_limits_validation(cli, args):
    code, docs = cli('limits', *args)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'
    assert cli('limits')[1][-1]['limits']['updated'] is None


# ------------------------------------------------------------------ registers

def test_registers(cli):
    code, docs = cli('registers')
    d = docs[-1]
    regs = d['registers']
    assert code == 0 and d['query'] == '0xFF' and len(regs) == len(P.REGISTERS) == 51
    by = {r['key']: r for r in regs}
    v = by['voltage']
    assert (v['id'], v['min'], v['max'], v['unit'], v['screen']) == ('0x00', 0.0, 30.0, 'V', 'digital')
    assert by['ovp']['max'] == 30.5 and by['ocp']['max'] == 10.5 and by['current']['max'] == 10.1
    assert (by['opp']['min'], by['opp']['max']) == (10, 310) and (by['otp']['min'], by['otp']['max']) == (60, 100)
    assert by['priority']['choices'] == ['cv', 'cc'] and by['priority']['id'] == '0x10'
    assert by['list3_voltage']['id'] == '0x3A' and by['list3_voltage']['list'] == 3
    assert by['list4_cut_time']['id'] == '0x4F' and by['color_bar_select']['id'] == '0x59'
    assert len({r['id'] for r in regs}) == len(regs)


# ------------------------------------------------------------------ raw

def test_raw_query(cli):
    for rid in ('0xFF', '255', '0xff'):
        code, docs = cli('raw', rid, '0')
        d = docs[-1]
        assert code == 0 and d['cmd'] == 'raw' and 'vout_v' in d['reply'] and 'sent' not in d, d
    code, docs = cli('raw', '0xFF', '1')
    assert code == 7 and err(docs)['code'] == 'UNSAFE_OPERATION'
    assert cli('raw', '0xFF', '1', '--unsafe')[0] == 0


def test_raw_documented_registers(cli, simstate):
    """Without --unsafe, documented registers go through the guarded paths (result 'routed')."""
    d = cli('raw', '0x00', '500')[1][-1]
    s = d['sent'][0]
    assert (s['key'], s['value'], s['raw'], s['id'], s['ack']) == ('voltage', 5.0, 500, '0x00', '01')
    assert d['routed'] == 'set' and 'unsafe' not in d
    assert simstate()['vset'] == 500 and state.last_sent('SIM')['voltage']['value'] == 5.0
    d = cli('raw', '0x50', '0xF800')[1][-1]
    assert d['routed'] == 'color' and keys(d) == ['color_vout', 'color_vout'] and 'note' in d['sent'][-1]
    assert simstate()['colors'][0] == 0xF800
    d = cli('raw', '0x1F', '0x0105')[1][-1]                          # list 1 cut-off time: 5 minutes
    assert d['sent'][0]['key'] == 'list1_cut_time' and simstate()['lists'][1]['cut_m'] == 5
    d = cli('raw', '0x1F', '0x0263')[1][-1]                          # 99 h: the top of the hours field
    assert d['sent'][0]['raw'] == 0x0263 and simstate()['lists'][1]['cut_h'] == 99
    d = cli('raw', '0x09', '2')[1][-1]
    assert d['routed'] == 'list select' and keys(d) == ['list'] and simstate()['list_sel'] == 2
    d = cli('raw', '0x08', '0')[1][-1]
    assert d['routed'] == 'output' and keys(d) == ['output'] and d['settled'] is True


def test_raw_undocumented_register_refused_unless_unsafe(cli, simstate):
    before = frames(simstate)
    code, docs = cli('raw', '0x60', '1')
    e = err(docs)
    assert code == 7 and e['code'] == 'UNSAFE_OPERATION' and 'not documented' in e['message'] and '--unsafe' in e['hint']
    assert frames(simstate) == before
    d = cli('raw', '0x60', '1', '--unsafe')[1][-1]
    s = d['sent'][0]
    assert s['key'] is None and s['id'] == '0x60' and s['ack'] == '01' and d['unsafe'] is True
    assert frames(simstate) == before + 1


@pytest.mark.parametrize('rid,data', [
    ('0x00', '3001'), ('0x01', '1011'), ('0x02', '3051'), ('0x06', '31'), ('0x07', '5'), ('0x07', '11'),
    ('0x08', '2'), ('0x08', '0x0100'), ('0x09', '5'), ('0x10', '2'), ('0x0A', '3001'), ('0x0E', '1001'),
    ('0x00', '0x10000'), ('0x100', '0'), ('-1', '0'),
])
def test_raw_out_of_range_refused(cli, simstate, rid, data):
    before = frames(simstate)
    code, docs = cli('raw', rid, data)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG', docs
    assert frames(simstate) == before


@pytest.mark.parametrize('data', ['0x0300', '0x00FF', '0x0163', '0x003D', '0x013D', '0x0264'])
def test_raw_cut_time_out_of_range_refused(cli, simstate, data):
    """The cut-off time register: high byte 0 s / 1 min / 2 h, low byte 0..60 s, 0..60 min, 0..99 h. 0x0300 (unit 3),
    0x00FF (255 s), 0x0163 (99 min), 0x003D (61 s), 0x013D (61 min) and 0x0264 (100 h) are out of the documented
    range and need --unsafe."""
    before = frames(simstate)
    code, docs = cli('raw', '0x0F', data)
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG', docs
    assert frames(simstate) == before


def test_raw_out_of_range_allowed_with_unsafe(cli, simstate):
    code, docs = cli('raw', '0x00', '3001', '--unsafe')
    assert code == 0 and simstate()['vset'] == 3001
    assert cli('raw', '0x100', '0', '--unsafe')[0] == 2           # still one byte of id


def test_raw_respects_bench_limits(cli, simstate):
    assert cli('limits', '--max-voltage', '5', '--max-current', '1')[0] == 0
    for rid, data in (('0x00', '600'), ('0x02', '600'), ('0x01', '200'), ('0x03', '150'), ('0x0A', '600'),
                      ('0x3D', '150')):
        code, docs = cli('raw', rid, data)
        assert code == 7 and err(docs)['code'] == 'UNSAFE_OPERATION', (rid, data)
    assert cli('raw', '0x00', '500')[0] == 0


def test_raw_refuses_an_ovp_below_the_present_output(cli, clock, simstate):
    """raw without --unsafe must not knowingly trip a protection: OVP 10 V while the output delivers 12 V."""
    output_up(cli, clock, v=12, ovp=13)
    code, docs = cli('raw', '0x02', '1000')
    assert code == 7, docs
    st = simstate()
    assert st['ovp'] == 1300 and st['alarm'] is None


# ------------------------------------------------------------------ selftest

def test_selftest_all_pass_and_changes_nothing(cli, simstate):
    before = simstate()
    code, docs = cli('selftest')
    d = docs[-1]
    assert code == 0 and d['ok'] and d['failed'] == 0 and d['complete'] is True, d
    assert [i['id'] for i in d['items']] == ['S0%d' % n for n in range(1, 10)]
    assert all(i['pass'] for i in d['items']) and d['passed'] == 9
    items = {i['id']: i for i in d['items']}
    assert items['S01']['measured'] == {'port': 'SIM', 'how': 'sim'}
    assert items['S09']['measured'] == {'answer_to_bad': '', 'good_answered': True}
    after = simstate()
    for k in ('vset', 'iset', 'ovp', 'ocp', 'opp', 'otp', 'on_cmd', 'colors', 'lists', 'list_sel'):
        assert after[k] == before[k], k
    assert state.last_sent('SIM') == {}


def test_selftest_with_output_on_skips_s08(cli, clock):
    output_up(cli, clock)
    code, docs = cli('selftest')
    d = docs[-1]
    items = {i['id']: i for i in d['items']}
    assert code == 0 and items['S08'].get('skipped') is True and d['passed'] == 8 and d['failed'] == 0


def test_selftest_fails_when_the_virtual_supply_is_off(cli, simstate):
    simstate(powered=False)
    code, docs = cli('selftest', '--timeout', '0.05', '--retries', '0')
    d = docs[-1]
    assert code == 1 and d['ok'] is False and d['complete'] is False and d['failed'] == 1
    items = {i['id']: i for i in d['items']}
    assert items['S01']['pass'] is True and items['S02']['pass'] is False
    assert items['S02']['measured']['code'] == 'NO_REPLY'


# ------------------------------------------------------------------ --text

TEXT_CASES = [
    ('ports',), ('ports', '--probe'), ('status',), ('set', '--voltage', '5', '--current', '1', '--ovp', '6'),
    ('on', '--no-wait'), ('off', '--no-wait'), ('off',), ('list', 'select', '1'),
    ('list', 'set', '1', '--voltage', '5', '--ovp', '6', '--cut-time', '1:2:3'), ('color', 'set', 'vout=red'),
    ('color', 'reset'), ('color', 'list'), ('watch', '-n', '2', '--every', '0.05'),
    ('watch', '-n', '2', '--every', '0.05', '--quiet'),
    ('log', '--duration', '0.15', '--every', '0.05', '--status-every', '0.05'),
    ('sequence', '--steps', '5,1,0.05', '--every', '0.05'), ('sequence', '--steps', '5,1,1', '--dry-run'),
    ('stop',), ('running',), ('limits',), ('limits', '--max-voltage', '20'), ('registers',), ('raw', '0xFF', '0'),
    ('raw', '0x00', '500'), ('selftest',),
]


@pytest.mark.parametrize('args', TEXT_CASES, ids=['-'.join(a) for a in TEXT_CASES])
def test_text_output(capsys, args):
    from openaxpp300s.cli import main
    if args[0] == 'sequence':
        assert main(['set', '--ovp', '7']) == 0
        capsys.readouterr()
    code, out, _ = run_capture(capsys, '--text', *args)
    assert code == 0, out
    assert out.strip() and 'INTERNAL' not in out and 'Traceback' not in out


def test_text_output_of_on_with_status_and_after_the_subcommand(capsys, clock):
    assert run_capture(capsys, 'set', '--voltage', '5', '--current', '1', '--ovp', '6')[0] == 0
    clock.jump_on(2.0)
    code, out, _ = run_capture(capsys, 'on', '--text')
    assert code == 0 and 'settled: True' in out and 'output ON' in out and not out.startswith('{')
    code, out, _ = run_capture(capsys, 'status', '--text')
    assert code == 0 and 'last sent:' in out and 'output ON' in out


def test_text_output_of_an_error(capsys):
    code, out, _ = run_capture(capsys, '--text', 'set', '--voltage', '99')
    assert code == 2 and out.startswith('error INVALID_ARG:') and 'hint:' in out


def test_text_mode_does_not_leak_into_the_next_call(capsys):
    """main() is also used in-process (tests, library); a --text call must not change the next call's output."""
    assert run_capture(capsys, '--text', 'registers')[0] == 0
    code, out, _ = run_capture(capsys, 'set', '--priority', 'xx')
    assert code == 2
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc['ok'] is False and doc['error']['code'] == 'INVALID_ARG'


# ------------------------------------------------------------------ global options

def test_sim_option_after_the_subcommand(cli, monkeypatch):
    monkeypatch.delenv('OPENAXPP300S_SIM')
    code, docs = cli('status', '--sim')
    assert code == 0 and docs[-1]['port'] == 'SIM'


def test_port_option_before_and_after_the_subcommand(cli):
    for args in (('status', '-p', 'SIM'), ('status', '--port', 'SIM'), ('--port', 'SIM', 'status'),
                 ('list', 'set', '0', '--voltage', '5', '--port', 'SIM'), ('color', 'list', '-p', 'SIM')):
        code, docs = cli(*args)
        assert code == 0 and docs[-1]['ok'], (args, docs)


def test_timeout_and_retries_before_and_after_the_subcommand(cli, simstate):
    simstate(powered=False)
    for args in (('--timeout', '0.05', '--retries', '0', 'status'), ('status', '--timeout', '0.05', '--retries', '0')):
        t0 = time.monotonic()
        code, docs = cli(*args)
        dt = time.monotonic() - t0
        assert code == 6 and err(docs)['code'] == 'NO_REPLY' and dt < 0.7, (args, dt)


def test_lock_wait_after_the_subcommand_and_busy(cli):
    from openaxpp300s.lock import PortLock
    lk = PortLock('SIM', 0).acquire()
    try:
        t0 = time.monotonic()
        code, docs = cli('status', '--lock-wait', '0.2')
        assert code == 4 and err(docs)['code'] == 'BUSY' and time.monotonic() - t0 < 2
    finally:
        lk.release()
    assert cli('status')[0] == 0


def test_verbose_after_the_subcommand_traces_frames(capsys):
    code, out, errtext = run_capture(capsys, 'status', '--verbose')
    assert code == 0 and json.loads(out.splitlines()[-1])['ok']
    traces = [json.loads(x[len('[frame] '):]) for x in errtext.splitlines() if x.startswith('[frame] ')]
    assert traces and traces[0]['tx'] == 'a5 ff 00 00 ff 5a' and len(bytes.fromhex(traces[0]['rx'])) == 15


# ------------------------------------------------------------------ error shape and exit codes

def test_error_json_shape(cli):
    code, docs = cli('set', '--voltage', '99')
    assert code == 2 and len(docs) == 1
    d = docs[0]
    assert set(d) == {'ok', 'cmd', 'error'} and d['ok'] is False and d['cmd'] == 'set'
    e = d['error']
    assert e['code'] == 'INVALID_ARG' and isinstance(e['message'], str) and e['message']
    assert isinstance(e['hint'], str) and e['hint']


@pytest.mark.parametrize('args', [('bogus',), (), ('status', '--bogus'), ('list',), ('raw', '0x00')])
def test_parse_errors_are_invalid_arg_with_a_hint(cli, args):
    code, docs = cli(*args)
    assert code == 2 and len(docs) == 1
    d = docs[0]
    assert d['ok'] is False and 'cmd' in d and d['error']['code'] == 'INVALID_ARG' and d['error']['hint']


def test_version_and_help_exit_0(capsys):
    code, out, _ = run_capture(capsys, '--version')
    assert code == 0 and 'openaxpp300s' in out
    assert run_capture(capsys, '--help')[0] == 0
    assert run_capture(capsys, 'set', '--help')[0] == 0
    assert run_capture(capsys, 'list', 'set', '--help')[0] == 0


def test_not_found_exit_3(cli, monkeypatch):
    monkeypatch.setenv('OPENAXPP300S_SIM', '0')           # no virtual supply, and no ports (see no_hardware)
    code, docs = cli('status')
    e = err(docs)
    assert code == 3 and e['code'] == 'NOT_FOUND' and e['ports_tried'] == [] and e['hint']


def test_port_error_exit_6_for_a_real_port_when_serial_is_disabled(cli, monkeypatch):
    monkeypatch.setenv('OPENAXPP300S_SIM', '0')           # a real port name only reaches the serial layer without sim
    code, docs = cli('status', '--port', 'COM250')
    assert code == 6 and err(docs)['code'] == 'PORT_ERROR'


def test_sim_wins_over_an_explicit_or_env_port(cli, monkeypatch, simstate):
    """With the simulator selected, --port other than SIM is refused, and OPENAXPP300S_PORT is ignored."""
    before = frames(simstate)
    for args in (('status', '--port', 'COM250'), ('--port', 'COM250', 'status'), ('set', '--voltage', '5', '-p', 'COM3')):
        code, docs = cli(*args)
        e = err(docs)
        assert code == 2 and e['code'] == 'INVALID_ARG' and 'COM' in e['message'], (args, e)
    assert frames(simstate) == before
    monkeypatch.setenv('OPENAXPP300S_PORT', 'COM250')
    code, docs = cli('status')
    assert code == 0 and docs[-1]['port'] == 'SIM'
    monkeypatch.delenv('OPENAXPP300S_SIM')
    code, docs = cli('status', '--sim', '--port', 'COM250')
    assert code == 2 and err(docs)['code'] == 'INVALID_ARG'


def test_no_reply_exit_6(cli, simstate):
    simstate(powered=False)
    code, docs = cli('set', '--voltage', '5', '--timeout', '0.05', '--retries', '0')
    assert code == 6 and err(docs)['code'] == 'NO_REPLY' and err(docs)['hint']


@pytest.mark.parametrize('args', [('raw', 'xyz', '0'), ('raw', '0x00', '5V'), ('sequence', 'no_such_file.csv'),
                                  ('log', '--out', '.', '--duration', '0.1')])
def test_user_input_errors_are_invalid_arg_not_internal(cli, tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    code, docs = cli(*args)
    e = err(docs)
    assert e['code'] == 'INVALID_ARG' and code == 2, e


# ------------------------------------------------------------------ child processes (like the MCP server)

def test_child_process_status_set_on_off_share_state(cli):
    code, docs, stderr = run_cli_proc('status')
    assert code == 0 and docs[-1]['port'] == 'SIM', stderr
    code, docs, stderr = run_cli_proc('set', '--voltage', '5', '--current', '1', '--ovp', '6')
    assert code == 0, stderr
    code, docs, stderr = run_cli_proc('on')
    assert code == 0 and docs[-1]['settled'] is True, (docs, stderr)
    d = cli('status')[1][-1]                               # in-process sees what the child sent
    assert d['output_on'] and d['last_sent']['voltage']['value'] == 5.0
    p = subprocess.run([sys.executable, '-m', 'openaxpp300s', 'off', '--text'], cwd=ROOT, capture_output=True,
                       text=True, encoding='utf-8', env=dict(os.environ), timeout=60)
    assert p.returncode == 0 and 'settled: True' in p.stdout and 'output OFF' in p.stdout, (p.stdout, p.stderr)
    assert cli('status')[1][-1]['output_on'] is False


def test_child_process_error_codes():
    code, docs, _ = run_cli_proc('set', '--voltage', '99')
    assert code == 2 and docs[-1]['error']['code'] == 'INVALID_ARG'
    code, docs, _ = run_cli_proc('raw', '0x60', '1')
    assert code == 7 and docs[-1]['error']['code'] == 'UNSAFE_OPERATION'
    code, docs, _ = run_cli_proc('--version')
    assert code == 0


def test_child_process_unicode_path_and_gbk_csv(tmp_path):
    f = tmp_path / '序列模板.csv'
    write_vendor_csv(f, [('5.00', '2.00', '10')], 'gbk')
    code, docs, stderr = run_cli_proc('sequence', str(f), '--dry-run')
    assert code == 0 and docs[-1]['steps'][0]['voltage'] == 5.0, stderr
    with open(f, 'ab') as fh:
        fh.write('五,1,1\r\n'.encode('gbk'))
    code, docs, stderr = run_cli_proc('sequence', str(f), '--dry-run')
    assert code == 2 and '序列模板.csv' in docs[-1]['error']['message'] and '五' in docs[-1]['error']['message']
