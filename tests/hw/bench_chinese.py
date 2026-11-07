"""Hardware check of the Chinese screen (parameter lists, cut-off current, cut-off time) on a real AX PP300S.
The user must have switched the supply to the Chinese screen (settings menu -> Interface -> Chinese). Load: anything
that draws ~nothing below 3 V (an e-load in CV mode at 3 V, or a meter). Never run in CI. Output off at the end.
    python tests/hw/bench_chinese.py [--out report.json]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_289 import ROOT, cli  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'logs', 'hw_report_chinese.json'))
    a = ap.parse_args()
    rep = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'steps': []}

    def step(name, doc, extra=None):
        d = {'name': name, 'ok': doc.get('ok'), 'exit': doc.get('_exit'), 't': round(doc.get('_t0', 0), 2)}
        for k in ('settled', 'on_after_s', 'off_after_s', 'note', 'warnings', 'stopped_by'):
            if k in doc:
                d[k] = doc[k]
        if 'error' in doc:
            d['error'] = {k: doc['error'].get(k) for k in ('code', 'message')}
        if 'sent' in doc:
            d['order'] = [s['name'] + '=' + str(s['value']) for s in doc['sent']]
        st = doc.get('status') or (doc if 'vout_v' in doc else None)
        if st:
            d['now'] = {k: st.get(k) for k in ('vout_v', 'iout_a', 'mode', 'output_on')}
        if extra:
            d.update(extra)
        rep['steps'].append(d)
        print(json.dumps(d, ensure_ascii=False), flush=True)
        return d

    def flag_series(seconds, every=0.5):
        w = cli('watch', '--for', '%gs' % seconds, '--every', str(every))
        return w

    try:
        step('status', cli('status'))
        step('list 0: 2.0 V, OVP 2.5, cut-off off, select', cli(
            'list', 'set', '0', '--voltage', '2', '--current', '0.5', '--ovp', '2.5', '--ocp', '1', '--cut-current',
            '0', '--cut-time', 'off', '--select'))
        step('on (expects list 0 = 2.0 V)', cli('on', check=False))
        w = cli('watch', '--for', '3s', '--every', '1', '--quiet')
        step('hold', w, {'vout_mean': w['stats']['vout_v']['mean'], 'modes': w.get('modes')})
        step('list 0 -> 2.4 V live (OVP 2.9)', cli('list', 'set', '0', '--voltage', '2.4', '--ovp', '2.9'))
        time.sleep(1.0)
        step('status after live change', cli('status'))
        d = cli('set', '--voltage', '1.5', check=False)
        step('Digital VSET 1.5 V must be ignored here (NOT_APPLIED)', d)
        step('list 1: 1.2 V with OVP 1.7 (written, not selected)', cli(
            'list', 'set', '1', '--voltage', '1.2', '--current', '0.5', '--ovp', '1.7', '--ocp', '1', '--cut-current',
            '0', '--cut-time', 'off'))
        d = cli('list', 'select', '1', check=False)
        step('select list 1 must be refused (its OVP 1.7 V < output 2.4 V)', d)
        step('list 1 OVP -> 3.0', cli('list', 'set', '1', '--ovp', '3'))
        step('select list 1 (output goes to 1.2 V)', cli('list', 'select', '1'))
        time.sleep(1.0)
        step('status on list 1', cli('status'))
        # cut-off current: almost no current flows below the e-load's 3 V -> output off after about 6 s
        step('list 1 cut-off current 0.1 A', cli('list', 'set', '1', '--cut-current', '0.1'))
        t0 = time.time()
        w = flag_series(10, 0.5)
        offs = [s for s in [] if s]
        step('cut-off current watch 10 s', w, {'modes': w.get('modes'), 'on_s': w.get('on_s')})
        st = cli('status')
        step('status after cut-off current', st)
        step('list 1 cut-off current 0', cli('list', 'set', '1', '--cut-current', '0'))
        # cut-off time 10 s: the on-time counter has run all session, so the first ON is cut at once; the next ON
        # restarts the counter and the output stays about 10 s
        step('list 1 cut-off time 0:00:10', cli('list', 'set', '1', '--cut-time', '0:0:10'))
        d = cli('on', '--wait-timeout', '4', check=False)
        step('on #1 (expect cut at once: accumulated on-time)', d)
        time.sleep(1.0)
        step('status after on #1', cli('status'))
        d = cli('on', '--wait-timeout', '4', check=False)
        step('on #2 (counter restarted)', d)
        t_on = time.time()
        w = flag_series(13, 0.5)
        step('cut-off time watch 13 s', w, {'modes': w.get('modes'), 'on_s': w.get('on_s'),
                                           'expect_on_s': 'about 10 minus the time on #2 took'})
        step('status after cut-off time', cli('status'))
        step('list 1 cut-off time off', cli('list', 'set', '1', '--cut-time', 'off'))
        # DELAY / REV are ignored on the Chinese screen: ON is immediate even with DELAY 3
        step('digital DELAY 3', cli('set', '--delay', '3'))
        d = cli('on', '--wait-timeout', '5', check=False)
        step('on with DELAY 3 (expect immediate on: ignored here)', d)
        step('off', cli('off'))
        step('digital DELAY 0', cli('set', '--delay', '0'))
        del t0, t_on, offs
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
