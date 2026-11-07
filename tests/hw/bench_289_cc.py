"""Hardware check of CV/CC under load: AX PP300S -> Fluke 287/289 (A DC, A jack, in series) -> electronic load in
constant-resistance mode (default 10 Ohm). Not part of the pytest suite, never run in CI.

Needs an open289 log running in the background (as for bench_289.py), the meter on DC current. Every step keeps OCP
well above the current that can flow (no trip), and the output is switched off at the end (also after a failure).
    python tests/hw/bench_289_cc.py --meter-log meter.jsonl --ohm 10 --out report.json
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_289 import ROOT, cli, meter_window  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--meter-log', required=True)
    ap.add_argument('--ohm', type=float, default=10.0, help='load resistance (CR mode)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'logs', 'hw_report_cc.json'))
    ap.add_argument('--hold', type=float, default=4.0)
    a = ap.parse_args()
    R = a.ohm
    rep = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'ohm': R, 'steps': []}

    def step(name, doc, extra=None):
        d = {'name': name, 'ok': doc.get('ok'), 'exit': doc.get('_exit')}
        for k in ('settled', 'on_after_s', 'off_after_s', 'note', 'warnings', 'error', 'stopped_by'):
            if k in doc:
                d[k] = doc[k]
        if extra:
            d.update(extra)
        rep['steps'].append(d)
        print(json.dumps(d, ensure_ascii=False), flush=True)
        return d

    def hold(name, expect_mode, expect_i, expect_v):
        w = cli('watch', '--for', '%gs' % a.hold, '--every', '0.5', '--quiet')
        vals = meter_window(a.meter_log, w['_t0'] + 1.5, w['_t1'] + 0.5)
        m = statistics.median(vals) if vals else None
        st = w.get('stats') or {}
        row = {'mode': w.get('modes'), 'expect_mode': expect_mode,
               'psu_i': st.get('iout_a', {}).get('mean'), 'psu_v': st.get('vout_v', {}).get('mean'),
               'psu_p': st.get('pout_w', {}).get('mean'), 'meter_i': m, 'meter_n': len(vals),
               'expect_i': expect_i, 'expect_v': expect_v, 'temp': st.get('temp_c', {}).get('max')}
        if m is not None and row['psu_i'] is not None:
            row['psu_minus_meter_ma'] = round((row['psu_i'] - m) * 1000, 1)
        step(name, w, row)
        return row

    table = []
    try:
        step('off at start', cli('off'))
        step('protections + 5 V / 1 A', cli('set', '--voltage', '5', '--current', '1', '--ovp', '5.5', '--ocp', '1.2',
                                           '--opp', '100', '--otp', '80', '--delay', '0', '--rev', '0'))
        step('on into the load (CV)', cli('on'))
        table.append(hold('CV 5 V on %g Ohm' % R, 'CV', 5 / R, 5.0))
        for iset in (0.4, 0.3, 0.2, 0.1):
            step('ISET %g' % iset, cli('set', '--current', str(iset)))
            table.append(hold('CC ISET %g A' % iset, 'CC', iset, iset * R))
        step('ISET 0.6 (back to CV)', cli('set', '--current', '0.6'))
        table.append(hold('CV again 5 V', 'CV', 5 / R, 5.0))
        step('off', cli('off'))
        step('12 V / 1 A, OCP 1.5', cli('set', '--voltage', '12', '--current', '1', '--ovp', '13', '--ocp', '1.5'))
        step('on into the load (CC)', cli('on'))
        table.append(hold('CC 12 V ISET 1 A', 'CC', 1.0, 1.0 * R))
        d = cli('log', '--every', '0.5', '--duration', '10s', '--out', os.path.join(ROOT, 'logs', 'cc_log.csv'))
        vals = meter_window(a.meter_log, d['_t0'] + 1.0, d['_t1'])
        m_ah = statistics.mean(vals) * (d['elapsed_s']) / 3600 if vals else None
        step('log 10 s at 1 A', d, {'ah': d.get('ah'), 'wh': d.get('wh'), 'on_s': d.get('on_s'),
                                    'meter_mean_a': statistics.mean(vals) if vals else None,
                                    'meter_ah_estimate': m_ah})
        step('off', cli('off'))
        rep['table'] = table
    finally:
        try:
            step('final off', cli('off', check=False))
        finally:
            rep['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(a.out, 'w', encoding='utf-8') as f:
                json.dump(rep, f, indent=1, ensure_ascii=False)
            print('report:', a.out)


if __name__ == '__main__':
    main()
