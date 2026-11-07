"""Hardware check of openaxpp300s against a Fluke 287/289 across the output (V DC), read with open289.

Not part of the pytest suite and never run in CI. Needs: the AX PP300S on its serial port, nothing but the meter
on the output (about 10 MOhm), and an open289 log running in the background, e.g.
    open289 log --duration 2h --every 0 --format jsonl --out meter.jsonl
then:
    python tests/hw/bench_289.py --meter-log meter.jsonl --out report.json [--max-voltage 15]

Every step runs one openaxpp300s CLI command (the installed package next to this file), records its JSON and the
wall-clock window in which the output was steady, and the meter readings in that window are compared with the
supply's own readback and the setpoint. It stays in normal operation: it never trips a protection on purpose.
The output is switched off at the end (also after a failure).
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def cli(*args, check=True):
    t0 = time.time()
    p = subprocess.run([sys.executable, '-m', 'openaxpp300s'] + list(args), cwd=ROOT, capture_output=True,
                       text=True, encoding='utf-8')
    lines = [json.loads(x) for x in p.stdout.splitlines() if x.startswith('{')]
    doc = lines[-1] if lines else {'ok': False, 'error': {'code': 'NO_OUTPUT', 'message': p.stderr[-500:]}}
    doc['_exit'] = p.returncode
    doc['_t0'], doc['_t1'] = t0, time.time()
    doc['_lines'] = len(lines)
    if check and p.returncode != 0:
        raise RuntimeError('%s failed (%s): %s' % (' '.join(args), p.returncode, json.dumps(doc)[:600]))
    return doc


def meter_window(path, t0, t1):
    vals = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if 'value' in d and d.get('t') and t0 <= d['t'] <= t1 and d.get('state') == 'NORMAL' \
                        and d.get('value') is not None:
                    vals.append(d['value'])
    except OSError:
        pass
    return vals


def psu_window(seconds, every=0.2):
    doc = cli('watch', '--for', '%gs' % seconds, '--every', str(every), '--quiet')
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--meter-log', required=True)
    ap.add_argument('--out', default=os.path.join(ROOT, 'logs', 'hw_report.json'))
    ap.add_argument('--max-voltage', type=float, default=15.0)
    ap.add_argument('--hold', type=float, default=3.0, help='s of steady output per voltage point')
    a = ap.parse_args()
    report = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'steps': []}

    def step(name, doc, extra=None):
        d = {'name': name, 'ok': doc.get('ok'), 'exit': doc.get('_exit')}
        for k in ('settled', 'on_after_s', 'off_after_s', 'settle_s', 'note', 'warnings', 'error', 'stopped_by'):
            if k in doc:
                d[k] = doc[k]
        if extra:
            d.update(extra)
        report['steps'].append(d)
        print(json.dumps(d, ensure_ascii=False), flush=True)
        return d

    try:
        step('selftest', cli('selftest'))
        step('off at start', cli('off'))
        step('set 1V 0.1A ovp 2', cli('set', '--voltage', '1', '--current', '0.1', '--ovp', '2', '--delay', '0',
                                      '--rev', '0'))
        step('on', cli('on'))
        points = [v for v in (0.5, 1.0, 2.5, 3.3, 5.0, 9.0, 12.0, 15.0, 20.0, 24.0, 30.0) if v <= a.max_voltage]
        table = []
        for v in points:
            ovp = min(30.5, round(v + 1.0, 2))
            d = cli('set', '--voltage', str(v), '--ovp', str(ovp))
            w = psu_window(a.hold)
            vals = meter_window(a.meter_log, w['_t0'] + 1.0, w['_t1'])
            m = statistics.median(vals) if vals else None
            row = {'vset': v, 'psu_mean': w['stats']['vout_v']['mean'] if w.get('stats') else None,
                   'psu_min': w['stats']['vout_v']['min'] if w.get('stats') else None,
                   'psu_max': w['stats']['vout_v']['max'] if w.get('stats') else None,
                   'meter_median': m, 'meter_n': len(vals), 'settle_s': d.get('settle_s'),
                   'order': [s['name'] for s in d['sent']], 'modes': w.get('modes')}
            if m is not None:
                row['meter_err_mv'] = round((m - v) * 1000, 1)
                row['psu_vs_meter_mv'] = round((row['psu_mean'] - m) * 1000, 1)
            table.append(row)
            step('point %g V' % v, d, row)
        report['accuracy'] = table
        # lowering VSET and OVP together while on: the OVP must be sent after the output came down
        top = points[-1]
        low = points[max(0, len(points) // 2 - 1)]
        d = cli('set', '--voltage', str(low), '--ovp', str(round(low + 1, 2)))
        w = psu_window(1.0)
        step('lower V and OVP together (%g -> %g)' % (top, low), d,
             {'order': [s['name'] for s in d['sent']], 'output_on_after': w['last']['output_on'],
              'vout_after': w['last']['vout_v']})
        # VSET above the OVP sent last must be refused before anything is sent
        d = cli('set', '--voltage', str(round(low + 2, 2)), check=False)
        step('VSET above last OVP refused', d, {'refused': d.get('error', {}).get('code') == 'UNSAFE_OPERATION'})
        step('off', cli('off'))
        # DELAY: ON switches the output on about DELAY s later
        step('set delay 3', cli('set', '--voltage', '2', '--ovp', '3', '--delay', '3'))
        d = cli('on')
        step('on with delay 3', d, {'expect': 'on_after_s about 2.3-3 + 1.2 s soft start'})
        step('off', cli('off'))
        step('set delay 0', cli('set', '--delay', '0'))
        # REV: toggles every REV s
        step('set rev 2', cli('set', '--rev', '2'))
        cli('on', '--no-wait')
        w = cli('watch', '--for', '9s', '--every', '0.2')
        step('rev 2 s toggling', w, {'modes': w.get('modes'), 'on_s': w.get('on_s'),
                                     'expect': 'about half of the 9 s on, OFF and CV both seen'})
        step('off', cli('off'))
        step('set rev 0', cli('set', '--rev', '0'))
        # sequence (OVP first: a step must never inherit a tight OVP from an earlier test step)
        step('ovp for the sequence', cli('set', '--ovp', '4'))
        d = cli('sequence', '--steps', '1,0.1,3;2,0.1,3;3,0.1,3', '--every', '0.2')
        vals = [(s['voltage'], d['_t0'] + 3 * k + 1.8, d['_t0'] + 3 * k + 2.8) for k, s in
                enumerate([{'voltage': 1}, {'voltage': 2}, {'voltage': 3}])]
        seq = []
        for v, t0, t1 in vals:
            mv = meter_window(a.meter_log, t0, t1)
            seq.append({'v': v, 'meter_median': statistics.median(mv) if mv else None, 'n': len(mv)})
        step('sequence 1/2/3 V x 3 s', d, {'meter': seq, 'end': d.get('end')})
        st = cli('status')
        step('output off after sequence', st, {'output_on': st['output_on']})
        # raw frames
        step('raw query', cli('raw', '0xFF', '0'))
        d = cli('raw', '0x77', '0', check=False)
        step('raw undocumented refused', d, {'refused': d.get('error', {}).get('code') == 'UNSAFE_OPERATION'})
        d = cli('raw', '0x00', '4000', check=False)
        step('raw out of range refused', d, {'refused': d.get('error', {}).get('code') == 'INVALID_ARG'})
    finally:
        try:
            step('final off', cli('off', check=False))
        finally:
            report['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
            os.makedirs(os.path.dirname(a.out), exist_ok=True)
            with open(a.out, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=1, ensure_ascii=False)
            print('report:', a.out)


if __name__ == '__main__':
    main()
