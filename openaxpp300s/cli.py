"""openaxpp300s command line: AX PP300S bench power supply over its USB serial port.

Every command prints JSON on stdout: one-shot commands print one object; watch, log and sequence print one object
per line (samples or events) and end with an "event": "done" summary. --text prints a human-readable form instead.
Errors: {"ok": false, "cmd": ..., "error": {"code", "message", "hint"}} and a non-zero exit code (see errors.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import traceback

from . import __version__, protocol as P, runner, state, transport
from .device import Supply
from .errors import PP300SError
from .paths import data_dir

FOREVER = ('forever', 'inf', 'until-stop')
_TEXT = [False]


# ------------------------------------------------------------------ output

def emit(obj, text=None):
    if _TEXT[0]:
        t = text(obj) if callable(text) else text
        if t is None:
            t = _default_text(obj)
        if t:
            print(t, flush=True)
    else:
        print(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), flush=True)


def _default_text(obj):
    if obj.get('ok') is False:
        e = obj.get('error', {})
        return 'error %s: %s' % (e.get('code'), e.get('message')) + ('\n  hint: %s' % e['hint'] if e.get('hint')
                                                                      else '')
    if obj.get('type') == 'sample':
        return _sample_text(obj)
    ev = obj.get('event')
    if ev and ev != 'done':
        rest = {k: v for k, v in obj.items() if k not in ('ok', 'cmd', 'event')}
        return '[%s] ' % ev + ', '.join('%s=%s' % (k, v) for k, v in rest.items())
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _sample_text(s):
    return '%s  %7.3f V  %6.3f A  %7.2f W  %-3s  in %5.2f V  %4.1f C' % (
        runner.local_time(s['t'])[11:], s['vout_v'], s['iout_a'], s['pout_w'], s['mode'], s['vin_v'], s['temp_c'])


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ argument helpers

def parse_duration(s, name='--duration'):
    """'90', '90s', '5m', '1.5h', '2d', 'forever' -> seconds (None = forever)."""
    if s is None:
        return None
    s = str(s).strip().lower()
    if s in FOREVER:
        return None
    m = re.fullmatch(r'([0-9]*\.?[0-9]+)\s*(ms|s|m|min|h|d)?', s)
    if not m:
        raise PP300SError('INVALID_ARG', '%s: cannot parse %r' % (name, s), 'use e.g. 30s, 5m, 2h or forever')
    return float(m.group(1)) * {'ms': 0.001, 's': 1, None: 1, 'm': 60, 'min': 60, 'h': 3600, 'd': 86400}[m.group(2)]


def parse_int(s, name='value'):
    t = str(s).strip().lower()
    try:
        return int(t, 16) if t.startswith('0x') else int(t)
    except ValueError:
        raise PP300SError('INVALID_ARG', '%s must be an integer (decimal or 0x hex), got %r' % (name, s)) from None


def default_out(prefix, ext):
    return os.path.join(data_dir(), time.strftime(prefix + '_%Y%m%d_%H%M%S') + ext)


def make_supply(args):
    trace = (lambda d: eprint('[frame] ' + json.dumps(d))) if args.verbose else None
    return Supply(args.port, lock_wait=args.lock_wait, timeout=args.timeout, retries=args.retries, trace=trace)


def _status_doc(sup, st):
    doc = {'ok': True, 'cmd': 'status', 'port': sup.port}
    doc.update(st)
    doc['last_sent'] = sup.known()
    lo = doc['last_sent'].get('output')
    if lo and lo['value'] is True and not st['output_on']:
        doc['note'] = ('this tool switched the output on (%s) but it is off now: DELAY/REV running, a protection '
                       'trip (OVP/OCP/OPP/OTP: the supply sounds an alarm until a key is pressed on its front panel), '
                       'a cut-off (Chinese screen), or switched off on the front panel' % runner.local_time(lo['t']))
    elif st['output_on'] and (lo is None or lo['value'] is False):
        doc['note'] = ('the output is ON but this tool did not switch it on%s: switched on on the front panel (or by '
                       'another program); the settings in force may differ from last_sent'
                       % (' (it last switched it off at %s)' % runner.local_time(lo['t']) if lo else ''))
    lim = state.get_limits()
    if any(v is not None for k, v in lim.items() if k != 'updated'):
        doc['limits'] = lim
    return doc


def _status_text(o):
    lines = ['%s  output %s  %s' % (o.get('port'), 'ON' if o['output_on'] else 'OFF', o['mode']),
             'out  %.3f V  %.3f A  %.2f W' % (o['vout_v'], o['iout_a'], o['pout_w']),
             'in   %.2f V   temp %.1f C   (reply %s ms)' % (o['vin_v'], o['temp_c'], o.get('latency_ms'))]
    ls = o.get('last_sent') or {}
    if ls:
        lines.append('last sent: ' + ', '.join('%s=%s' % (k, v['value']) for k, v in sorted(ls.items())))
    if o.get('limits'):
        lines.append('limits: %s' % o['limits'])
    if o.get('note'):
        lines.append('note: %s' % o['note'])
    return '\n'.join(lines)


# ------------------------------------------------------------------ commands

def cmd_ports(args):
    ports = transport.list_ports()
    if args.probe:
        for p in ports:
            if p['bluetooth'] and not args.all:
                p['probe'] = 'skipped (Bluetooth serial port)'
                continue
            if not p['usb'] and not args.all:
                p['probe'] = 'skipped (not USB)'
                continue
            st = transport.probe(p['port'])
            p['pp300s'] = st is not None
            if st:
                p['status'] = {k: st[k] for k in ('vout_v', 'iout_a', 'vin_v', 'temp_c', 'output_on', 'mode')}
                if p['port'] != transport.SIM_PORT:
                    state.remember_port(p['port'])
    emit({'ok': True, 'cmd': 'ports', 'ports': ports, 'remembered': state.remembered_port()},
         lambda o: '\n'.join('%-6s %-40s %s%s' % (p['port'], p['description'][:40],
                                                   'WCH ' if p['wch'] else '',
                                                   ('PP300S' if p.get('pp300s') else p.get('probe', ''))
                                                   if args.probe else '') for p in o['ports']) or 'no ports')


def cmd_status(args):
    sup = make_supply(args)
    emit(_status_doc(sup, sup.status()), _status_text)


def _sent_text(o):
    lines = ['sent ' + ', '.join('%s=%s%s' % (s['name'], s['value'], s['unit']) for s in o.get('sent', []))]
    if o.get('warnings'):
        lines += ['warning: ' + w for w in o['warnings']]
    if 'settled' in o:
        lines.append('settled: %s %s' % (o['settled'], o.get('note', '')))
    if o.get('status'):
        lines.append(_status_text(o['status']))
    return '\n'.join(lines)


def cmd_set(args):
    settings = {k: getattr(args, k) for k in ('voltage', 'current', 'ovp', 'ocp', 'delay', 'rev', 'opp', 'otp',
                                              'priority')}
    sup = make_supply(args)
    res = sup.apply(settings, wait=not args.no_wait)
    emit(dict({'ok': True, 'cmd': 'set', 'port': sup.port}, **res), _sent_text)


def cmd_output(args):
    sup = make_supply(args)
    on = args.cmd == 'on'
    res = sup.output(on, wait=not args.no_wait, timeout=args.wait_timeout)
    emit(dict({'ok': True, 'cmd': args.cmd, 'port': sup.port}, **res), _sent_text)


def cmd_list(args):
    sup = make_supply(args)
    if args.list_cmd == 'select':
        res = sup.select_list(args.n)
    else:
        res = sup.set_list(args.n, select=args.select, voltage=args.voltage, current=args.current, ovp=args.ovp,
                           ocp=args.ocp, cut_current=args.cut_current, cut_time=args.cut_time)
    emit(dict({'ok': True, 'cmd': 'list ' + args.list_cmd, 'port': sup.port, 'list': args.n}, **res), _sent_text)


def cmd_color(args):
    if args.color_cmd == 'list':
        last = state.last_sent(transport.resolve_port(args.port)[0]) if args.port or transport.sim_enabled() \
            or state.remembered_port() else {}
        items = []
        for el, dflt in P.COLOR_DEFAULTS.items():
            reg = P.BY_KEY['color_' + el]
            ls = last.get(reg.key)
            items.append({'element': el, 'id': '0x%02X' % reg.reg_id, 'doc': P.COLOR_DOCS[el],
                          'default': '0x%04X' % dflt, 'default_rgb': P.rgb565_to_hex(dflt),
                          'last_sent': ls['value'] if ls else None})
        emit({'ok': True, 'cmd': 'color list', 'elements': items})
        return
    sup = make_supply(args)
    if args.color_cmd == 'reset':
        res = sup.set_colors(dict(P.COLOR_DEFAULTS))
    else:
        colors = {}
        for item in args.assign:
            el, sep, c = item.partition('=')
            if not sep:
                raise PP300SError('INVALID_ARG', 'expected ELEMENT=COLOUR, got %r' % item,
                                  'e.g. vout=#FFFF00 iout=green; elements: %s' % ', '.join(P.COLOR_DEFAULTS))
            colors[el.strip().lower()] = c.strip()
        res = sup.set_colors(colors)
    emit(dict({'ok': True, 'cmd': 'color ' + args.color_cmd, 'port': sup.port}, **res), _sent_text)


def _long(cmd, args, fn):
    """Register a long-running command (for `stop`), run it, print the summary as the last line."""
    state.register_running(cmd, {'argv': sys.argv[1:]})
    try:
        try:
            out = fn()
        except PP300SError as e:                    # print before unregistering: a job never looks dead and silent
            emit({'ok': False, 'cmd': cmd, 'event': 'done', 'error': e.to_dict()})
            return e.exit_code
        emit(dict({'ok': True, 'cmd': cmd, 'event': 'done'}, **out))
        return 0
    finally:
        state.unregister_running()


def cmd_watch(args):
    sup = make_supply(args)
    count = args.count
    duration = parse_duration(args.for_, '--for') if args.for_ else None
    if count is None and args.for_ is None:
        count = 10
    return _long('watch', args, lambda: runner.watch(sup, lambda d: emit(d), every=args.every, count=count,
                                                     duration=duration, quiet=args.quiet,
                                                     reconnect_for=parse_duration(args.reconnect_for)))


def cmd_log(args):
    sup = make_supply(args)
    fmt = args.format
    out = args.out or default_out('log', '.' + (fmt or 'csv'))
    status_every = parse_duration(args.status_every, '--status-every') if args.status_every else None
    return _long('log', args, lambda: runner.log(
        sup, lambda d: emit(d), out, fmt=fmt, every=args.every, duration=parse_duration(args.duration),
        status_every=status_every, reconnect_for=parse_duration(args.reconnect_for, '--reconnect-for')))


def cmd_sequence(args):
    if bool(args.file) == bool(args.steps):
        raise PP300SError('INVALID_ARG', 'give a CSV file or --steps (one of them)',
                          'CSV rows: voltage,current,seconds (the vendor 序列模板.csv works); --steps "5,2,10;9,1,20"')
    steps = runner.read_steps(args.file) if args.file else runner.parse_steps(args.steps)
    sup = make_supply(args)
    warnings = runner.check_steps(sup, steps)
    if args.dry_run:
        doc = {'ok': True, 'cmd': 'sequence', 'dry_run': True, 'steps': steps,
               'total_s': sum(s['seconds'] for s in steps) * (args.repeat or 1)}
        if warnings:
            doc['warnings'] = warnings
        emit(doc)
        return 0
    return _long('sequence', args, lambda: runner.sequence(
        sup, lambda d: emit(d), steps, repeat=args.repeat, end=args.end, every=args.every, log_out=args.log,
        fmt=args.format))


def cmd_stop(args):
    asked = state.request_stop(pid=args.pid, cmd=args.only)
    emit({'ok': True, 'cmd': 'stop', 'asked': [{'pid': a['pid'], 'cmd': a['cmd'], 'started': a.get('started')}
                                               for a in asked],
          'note': 'each stops within about 0.1 s (a sequence switches the output off unless --end keep)'
          if asked else 'nothing running'})


def cmd_running(args):
    emit({'ok': True, 'cmd': 'running', 'running': state.list_running()})


def cmd_limits(args):
    if args.clear or any(v is not None for v in (args.max_voltage, args.max_current, args.note)):
        for name, v, hi in (('--max-voltage', args.max_voltage, 30.0), ('--max-current', args.max_current, 10.1)):
            if v is not None and not 0 <= v <= hi:
                raise PP300SError('INVALID_ARG', '%s must be 0..%g' % (name, hi))
        lim = state.set_limits(args.max_voltage, args.max_current, args.note, clear=args.clear)
        emit({'ok': True, 'cmd': 'limits', 'changed': True, 'limits': lim, 'file': state.limits_path()})
    else:
        emit({'ok': True, 'cmd': 'limits', 'changed': False, 'limits': state.get_limits(),
              'file': state.limits_path()})


def cmd_registers(args):
    regs = [r.describe() for r in P.REGISTERS]
    emit({'ok': True, 'cmd': 'registers', 'query': '0xFF', 'registers': regs},
         lambda o: '\n'.join('%-5s %-22s %-14s %8s..%-8s %-3s %-8s %s' % (
             r['id'], r['key'], r['name'], r['min'], r['max'], r['unit'], r['screen'], r['doc'][:60])
             for r in o['registers']))


def cmd_raw(args):
    sup = make_supply(args)
    res = sup.raw(parse_int(args.id, 'ID'), parse_int(args.data, 'DATA'), unsafe=args.unsafe)
    emit(dict({'ok': True, 'cmd': 'raw', 'port': sup.port}, **res))


def cmd_selftest(args):
    items = []

    def item(cid, name, ok, measured=None, expected=None, skipped=False):
        d = {'id': cid, 'name': name, 'pass': bool(ok)}
        if measured is not None:
            d['measured'] = measured
        if expected is not None:
            d['expected'] = expected
        if skipped:
            d['skipped'] = True
        items.append(d)
        return ok

    done = False
    try:
        sup = make_supply(args)
        item('S01', 'port resolved', True, {'port': sup.port, 'how': sup.port_how})
        try:
            st = sup.status()
        except PP300SError as e:
            item('S02', 'status reply', False, e.to_dict(), 'a valid 15-byte reply')
            raise _Abort()
        item('S02', 'status reply', True, st['raw'], 'A5 .. 5A, checksum ok')
        lat, bad = [], 0
        for _ in range(8):
            try:
                lat.append(sup.query()['latency_ms'])
            except PP300SError:
                bad += 1
        item('S03', '8 more replies valid', bad == 0, {'failed': bad}, {'failed': 0})
        med = statistics.median(lat) if lat else None
        item('S04', 'reply latency', med is not None and med <= 150, {'median_ms': med, 'max_ms': max(lat or [0])},
             {'median_ms': '<= 150'})
        item('S05', 'input voltage plausible', 4.5 <= st['vin_v'] <= 42, st['vin_v'], '4.5..42 V')
        item('S06', 'temperature plausible', -10 <= st['temp_c'] <= 95, st['temp_c'], '-10..95 C')
        perr = abs(st['pout_w'] - st['vout_v'] * st['iout_a'])
        item('S07', 'power = voltage x current', perr <= 0.05 + 0.03 * st['pout_w'],
             {'pout_w': st['pout_w'], 'v_x_i': round(st['vout_v'] * st['iout_a'], 4)}, 'within 0.05 W + 3 %')
        if not st['output_on']:
            item('S08', 'output off reads ~0 V', st['vout_v'] < 0.2, st['vout_v'], '< 0.2 V')
        else:
            item('S08', 'output off reads ~0 V', True, 'output is on', None, skipped=True)
        # S09: a frame with a bad checksum must get no answer, and the next good frame must
        with sup.session() as s:
            s.ser.reset_input_buffer()
            s.ser.write(bytes([P.FRAME_HEAD, P.QUERY_ID, 0, 0, 0x00, P.FRAME_TAIL]))
            t_end = time.perf_counter() + 0.15
            got = b''
            while time.perf_counter() < t_end:
                n = s.ser.in_waiting
                if n:
                    got += s.ser.read(n)
                time.sleep(0.002)
            ok_after = True
            try:
                s.query()
            except PP300SError:
                ok_after = False
        item('S09', 'bad checksum ignored, next frame answered', not got and ok_after,
             {'answer_to_bad': got.hex(' '), 'good_answered': ok_after}, {'answer_to_bad': '', 'good_answered': True})
        done = True
    except _Abort:
        pass
    except PP300SError as e:
        item('S01', 'port resolved', False, e.to_dict())
    failed = [i for i in items if not i['pass']]
    emit({'ok': not failed, 'cmd': 'selftest', 'passed': sum(i['pass'] and not i.get('skipped') for i in items),
          'failed': len(failed), 'complete': done, 'items': items},
         lambda o: '\n'.join('%s %-45s %s  %s' % (i['id'], i['name'], 'SKIP' if i.get('skipped') else
                                                   ('PASS' if i['pass'] else 'FAIL'), i.get('measured', ''))
                             for i in o['items']) + '\n%d passed, %d failed' % (o['passed'], o['failed']))
    return 0 if not failed else 1


class _Abort(Exception):
    pass


# ------------------------------------------------------------------ parser

HANDLERS = {'ports': cmd_ports, 'status': cmd_status, 'set': cmd_set, 'on': cmd_output, 'off': cmd_output,
            'list': cmd_list, 'color': cmd_color, 'watch': cmd_watch, 'log': cmd_log, 'sequence': cmd_sequence,
            'stop': cmd_stop, 'running': cmd_running, 'limits': cmd_limits, 'registers': cmd_registers,
            'raw': cmd_raw, 'selftest': cmd_selftest}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PP300SError('INVALID_ARG', message, 'see "openaxpp300s %s--help"'
                          % ((self.prog.split(' ', 1)[1] + ' ') if ' ' in self.prog else ''))


GLOBAL_DEFAULTS = {'port': None, 'sim': False, 'text': False, 'verbose': False, 'lock_wait': 5.0,
                   'timeout': transport.DEFAULT_TIMEOUT, 'retries': transport.DEFAULT_RETRIES}


def _add_globals(p, suppress):
    """The global options; on the subcommands too (default SUPPRESS), so they work before or after the command."""
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    p.add_argument('-p', '--port', default=d(None), help='serial port (default: $OPENAXPP300S_PORT, the port that '
                   'answered last, else the first USB serial port answering like a PP300S)')
    p.add_argument('--sim', action='store_true', default=d(False), help='use the virtual supply (also '
                   'OPENAXPP300S_SIM=1)')
    p.add_argument('--text', action='store_true', default=d(False), help='human-readable output instead of JSON')
    p.add_argument('--verbose', action='store_true', default=d(False), help='print every frame sent and received '
                   'on stderr')
    p.add_argument('--lock-wait', type=float, default=d(5.0), help='s to wait for another command using the port '
                   '(default 5)')
    p.add_argument('--timeout', type=float, default=d(transport.DEFAULT_TIMEOUT), help='s to wait for each '
                   'answer (default %g)' % transport.DEFAULT_TIMEOUT)
    p.add_argument('--retries', type=int, default=d(transport.DEFAULT_RETRIES), help='resends of a frame without '
                   'an answer (default %d)' % transport.DEFAULT_RETRIES)


class _Sub:
    """add_parser wrapper: every subcommand also takes the global options."""

    def __init__(self, sub):
        self.sub = sub

    def add_parser(self, *a, **k):
        s = self.sub.add_parser(*a, **k)
        _add_globals(s, suppress=True)
        return s


def build_parser():
    p = _Parser(prog='openaxpp300s', description='AX PP300S bench power supply over its USB serial port. JSON '
                'output (one object per line); errors carry a code, a hint and an exit code.')
    p.add_argument('--version', action='version', version='openaxpp300s ' + __version__)
    _add_globals(p, suppress=False)
    sub = _Sub(p.add_subparsers(dest='cmd', required=True, parser_class=_Parser))

    s = sub.add_parser('ports', help='list serial ports; --probe asks each USB port for a PP300S status')
    s.add_argument('--probe', action='store_true')
    s.add_argument('--all', action='store_true', help='with --probe: also non-USB and Bluetooth ports (slow)')

    sub.add_parser('status', help='measured output, input voltage, temperature, CV/CC, output state; plus the '
                   'values this tool sent last')

    s = sub.add_parser('set', help='send settings (Digital/Waveform screen + OPP/OTP/priority) in a safe order')
    s.add_argument('--voltage', '-V', type=float, help='VSET, V (0..30.00, 10 mV steps)')
    s.add_argument('--current', '-I', type=float, help='ISET, A (0.01..10.10, 10 mA steps)')
    s.add_argument('--ovp', type=float, help='over-voltage protection, V (0.50..30.50; at least VSET + max(0.2 V, '
                   '1.5 %%))')
    s.add_argument('--ocp', type=float, help='over-current protection, A (0.10..10.50; keep it above ISET)')
    s.add_argument('--delay', type=int, help='DELAY s (0..9999): ON switches the output on DELAY s later')
    s.add_argument('--rev', type=int, help='REV s (0..9999): after ON the output toggles every REV s')
    s.add_argument('--opp', type=int, help='over-power protection, W (10..310 in 10 W steps)')
    s.add_argument('--otp', type=int, help='over-temperature protection, C (60..100 in 10 C steps)')
    s.add_argument('--priority', choices=('cv', 'cc'), help='CV first or CC first (firmware >= 1.4.2)')
    s.add_argument('--no-wait', action='store_true', help='do not wait for the output to follow a new VSET')

    for name, hlp in (('on', 'switch the output on (honours DELAY/REV) and wait until the voltage is up'),
                      ('off', 'switch the output off and wait until it is down')):
        s = sub.add_parser(name, help=hlp)
        s.add_argument('--no-wait', action='store_true')
        s.add_argument('--wait-timeout', type=float, help='s to follow the output (default: 1.6 s soft start + '
                       'DELAY + REV + 2 s; with DELAY or REV over 20 s `on` returns right after the frame)')

    s = sub.add_parser('list', help='Chinese screen parameter lists 0..4')
    lsub = _Sub(s.add_subparsers(dest='list_cmd', required=True, parser_class=_Parser))
    x = lsub.add_parser('select', help='make list N the active one')
    x.add_argument('n', type=int, choices=range(5))
    x = lsub.add_parser('set', help='write values of list N')
    x.add_argument('n', type=int, choices=range(5))
    x.add_argument('--voltage', '-V', type=float, help='V (0..30.00)')
    x.add_argument('--current', '-I', type=float, help='A (0.01..10.10)')
    x.add_argument('--ovp', type=float, help='V (0.50..30.50)')
    x.add_argument('--ocp', type=float, help='A (0.10..10.50)')
    x.add_argument('--cut-current', type=float, help='A (0..10.00): output off after the current stays below it '
                   'for about 6 s; 0 disables')
    x.add_argument('--cut-time', help='output off when the accumulated on-time reaches this: 1h30m, 45m, 50s, '
                   'H:M:S or off (= 99:60:60); hours 0..99, minutes and seconds 0..60; fields not given are 0; '
                   '0:0:0 is refused (it would cut the output off at once)')
    x.add_argument('--select', action='store_true', help='also make it the active list (sent last, after the '
                   'values; refused if its values are unknown/over the limits where that matters)')

    s = sub.add_parser('color', help='display colours of the Digital screen (firmware >= 1.4.4, saved in EEPROM)')
    csub = _Sub(s.add_subparsers(dest='color_cmd', required=True, parser_class=_Parser))
    x = csub.add_parser('set', help='ELEMENT=COLOUR ... (#RRGGBB, 0xRGB565 or a name)')
    x.add_argument('assign', nargs='+')
    csub.add_parser('reset', help='send the vendor default colours')
    csub.add_parser('list', help='the elements, their defaults and what this tool sent last')

    s = sub.add_parser('watch', help='status lines every --every s (JSONL), then a summary')
    s.add_argument('--every', type=float, default=1.0, help='s between queries (default 1; every query also '
                   'makes the supply rewrite one EEPROM field, see docs)')
    s.add_argument('-n', '--count', type=int)
    s.add_argument('--for', dest='for_', help='duration (30s, 5m, 1h, forever)')
    s.add_argument('--quiet', action='store_true', help='summary only')
    s.add_argument('--reconnect-for', default='10s')

    s = sub.add_parser('log', help='log the status to CSV/JSONL with Ah/Wh until --duration or "stop"')
    s.add_argument('--out', '-o')
    s.add_argument('--format', choices=('csv', 'jsonl'))
    s.add_argument('--every', type=float, default=1.0)
    s.add_argument('--duration', default='forever')
    s.add_argument('--status-every', help='print a progress line this often (e.g. 30s)')
    s.add_argument('--reconnect-for', default='5m', help='keep retrying this long after the supply stops '
                   'answering (default 5m)')

    s = sub.add_parser('sequence', help='timed V/I steps (vendor CSV: voltage,current,seconds)')
    s.add_argument('file', nargs='?')
    s.add_argument('--steps', help='"5,2,10; 9,1,20" instead of a file')
    s.add_argument('--repeat', type=int, default=1, help='cycles (0 = until "stop")')
    s.add_argument('--end', choices=('off', 'keep'), default='off', help='output after the last step (default off)')
    s.add_argument('--every', type=float, default=1.0, help='status poll interval during a step (default 1 s)')
    s.add_argument('--log', help='also log the status to this CSV/JSONL file')
    s.add_argument('--format', choices=('csv', 'jsonl'))
    s.add_argument('--dry-run', action='store_true', help='check the steps against ranges and limits only')

    s = sub.add_parser('stop', help='stop running watch / log / sequence commands')
    s.add_argument('--pid', type=int)
    s.add_argument('--only', choices=('watch', 'log', 'sequence'))

    sub.add_parser('running', help='list running watch / log / sequence commands')

    s = sub.add_parser('limits', help='bench limits: settings above them are refused (show, or change)')
    s.add_argument('--max-voltage', type=float)
    s.add_argument('--max-current', type=float)
    s.add_argument('--note', help='what is connected to the output')
    s.add_argument('--clear', action='store_true')

    sub.add_parser('registers', help='the register table (ids, ranges, screens)')

    s = sub.add_parser('raw', help='send one frame: ID DATA (decimal or 0x..); ID 0xFF = status query')
    s.add_argument('id')
    s.add_argument('data')
    s.add_argument('--unsafe', action='store_true', help='allow undocumented registers and out-of-range data')

    sub.add_parser('selftest', help='read-only checks of the link and the supply (never changes settings)')
    return p


def _check_numbers(args):
    import math as _m
    for name, ok in (('--retries', lambda v: v >= 0), ('--timeout', lambda v: v > 0),
                     ('--lock-wait', lambda v: v >= 0), ('--every', lambda v: v >= 0.05),
                     ('--wait-timeout', lambda v: v > 0), ('--count', lambda v: v >= 1),
                     ('--repeat', lambda v: v >= 0)):
        v = getattr(args, name[2:].replace('-', '_'), None)
        if v is None:
            continue
        if isinstance(v, float) and not _m.isfinite(v) or not ok(v):
            raise PP300SError('INVALID_ARG', '%s out of range: %r' % (name, v),
                              '--every >= 0.05 s, --timeout > 0, --retries/--lock-wait/--repeat >= 0, --count >= 1')


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    cmd = None
    _TEXT[0] = False
    try:
        args = build_parser().parse_args(argv)
        cmd = args.cmd + (' ' + getattr(args, args.cmd + '_cmd') if getattr(args, args.cmd + '_cmd', None)
                          else '')
        _TEXT[0] = args.text
        _check_numbers(args)
        if args.sim:
            transport.use_sim(True)
        code = HANDLERS[args.cmd](args)
        return code or 0
    except PP300SError as e:
        emit({'ok': False, 'cmd': cmd, 'error': e.to_dict()})
        return e.exit_code
    except SystemExit as e:                        # --help / --version
        return e.code if isinstance(e.code, int) else 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        emit({'ok': False, 'cmd': cmd, 'error': {'code': 'INTERNAL', 'message': repr(e),
                                                 'trace': traceback.format_exc().splitlines()[-6:]}})
        return 1


if __name__ == '__main__':
    sys.exit(main())
