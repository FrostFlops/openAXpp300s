"""Hardware check of the current limit (CC): AX PP300S -> Fluke 287/289 (A DC, A jack, in series) -> electronic load
in constant-VOLTAGE mode below VSET (default 3.0 V), so the supply sits in CC and must deliver exactly ISET.
(A constant-resistance load oscillates against the supply's CC loop: two control loops fighting.)

Also checks the ordering when ISET and OCP are lowered together under current (ISET first, OCP after the current
came down), and the switch-on with CV-first vs CC-first priority. Not part of the pytest suite, never run in CI.
    python tests/hw/bench_289_cc_cvload.py --meter-log meter.jsonl --load-v 3 --out report.json
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
    ap.add_argument('--load-v', type=float, default=3.0)
    ap.add_argument('--out', default=os.path.join(ROOT, 'logs', 'hw_report_cc_cvload.json'))
    ap.add_argument('--hold', type=float, default=4.0)
    a = ap.parse_args()
    rep = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'load_v': a.load_v, 'steps': []}

    def step(name, doc, extra=None):
        d = {'name': name, 'ok': doc.get('ok'), 'exit': doc.get('_exit')}
        for k in ('settled', 'on_after_s', 'off_after_s', 'note', 'warnings', 'error', 'stopped_by'):
            if k in doc:
                d[k] = doc[k]
        if 'sent' in doc:
            d['order'] = [s['name'] for s in doc['sent']]
        if extra:
            d.update(extra)
        rep['steps'].append(d)
        print(json.dumps(d, ensure_ascii=False), flush=True)
        return d

    def hold(name, iset):
        w = cli('watch', '--for', '%gs' % a.hold, '--every', '0.5', '--quiet')
        vals = meter_window(a.meter_log, w['_t0'] + 1.5, w['_t1'] + 0.5)
        m = statistics.median(vals) if vals else None
        st = w.get('stats') or {}
        row = {'iset': iset, 'mode': w.get('modes'), 'psu_i': st.get('iout_a', {}).get('mean'),
               'psu_i_min': st.get('iout_a', {}).get('min'), 'psu_i_max': st.get('iout_a', {}).get('max'),
               'psu_v': st.get('vout_v', {}).get('mean'), 'meter_i': m, 'meter_n': len(vals),
               'meter_spread_ma': round((max(vals) - min(vals)) * 1000, 1) if vals else None,
               'temp': st.get('temp_c', {}).get('max')}
        if m is not None:
            row['meter_minus_iset_ma'] = round((m - iset) * 1000, 1)
            row['psu_minus_meter_ma'] = round((row['psu_i'] - m) * 1000, 1)
        step(name, w, row)
        return row

    table = []
    try:
        step('off at start', cli('off'))
        step('5 V, ISET 0.1, OVP 5.5, OCP 1.5', cli('set', '--voltage', '5', '--current', '0.1', '--ovp', '5.5',
                                                   '--ocp', '1.5', '--opp', '100', '--otp', '80', '--delay', '0',
                                                   '--rev', '0'))
        step('on into the CV load', cli('on'))
        for iset in (0.1, 0.25, 0.5, 1.0):
            if iset != 0.1:
                step('ISET %g' % iset, cli('set', '--current', str(iset)))
            table.append(hold('CC at ISET %g A' % iset, iset))
        # lower ISET 1.0 -> 0.3 and OCP 1.5 -> 0.6 together while 1 A flows: OCP must go after the current came down
        d = cli('set', '--current', '0.3', '--ocp', '0.6')
        step('lower ISET and OCP together under 1 A', d)
        table.append(hold('CC at ISET 0.3 A (after the lowering)', 0.3))
        st = cli('status')
        step('still on, no trip', st, {'output_on': st['output_on'], 'mode': st['mode'], 'iout': st['iout_a']})
        step('OCP back to 1.5', cli('set', '--ocp', '1.5'))
        # switch-on with each priority: fast samples (20 Hz, the supply's reply tick) for 2.5 s
        for prio in ('cv', 'cc'):
            step('off', cli('off'))
            step('ISET 0.5, priority %s' % prio, cli('set', '--current', '0.5', '--priority', prio))
            cli('on', '--no-wait')
            w = cli('watch', '--for', '2.5s', '--every', '0.05')
            st = w.get('stats') or {}
            step('switch-on with priority %s' % prio, w, {'modes': w.get('modes'),
                                                         'i_max': st.get('iout_a', {}).get('max'),
                                                         'v_max': st.get('vout_v', {}).get('max'),
                                                         'last': w.get('last')})
        step('priority back to cc (factory default)', cli('set', '--priority', 'cc'))
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
