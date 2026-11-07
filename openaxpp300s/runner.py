"""Long-running loops: watch (status lines), log (CSV/JSONL file with Ah/Wh integration) and sequence (timed
voltage/current steps, like the vendor app's 序列输出 tab).

Each loop takes the port only for one status query (or one step's frames) at a time, so other commands can change
settings while a log runs. Every loop ends on its duration/count, on `openaxpp300s stop` (a file in state/stop/
polled about every 50 ms), on Ctrl+C, or after the supply stopped answering for longer than reconnect_for.

Every status query makes the supply rewrite the front-panel field under its cursor in its EEPROM (firmware 1.4.4),
so loops poll at 1 s by default and warn when a run will send many frames.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time

from . import state
from .errors import PP300SError

LINK_ERRORS = ('NO_REPLY', 'BAD_REPLY', 'PORT_ERROR', 'BUSY', 'NOT_FOUND')
SAMPLE_FIELDS = ('vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'mode', 'output_on')
CSV_COLUMNS = ('pc_time', 'local_time', 'vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c', 'mode', 'output_on',
               'ah', 'wh', 'step')
EEPROM_WARN_FRAMES = 3600
FRAMES_PER_STEP = 6                  # VSET, ISET, ON (first step) and the queries around them
STEP_SETTLE_S = 2.5                  # after a step starts (soft start + settling), its voltage is checked


def local_time(t):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t)) + '.%03d' % int(t % 1 * 1000)


class Stopper:
    """Why a loop ended: None while running; 'stop' (stop request), 'interrupt' (Ctrl+C)."""

    def __init__(self):
        self.reason = None

    def check(self):
        if self.reason is None and state.stop_requested():
            self.reason = 'stop'
        return self.reason

    def sleep(self, seconds):
        """Sleep in slices, returning early (True) on a stop request."""
        t_end = time.monotonic() + max(0.0, seconds)
        while True:
            if self.check():
                return True
            left = t_end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.05, left))


class Stats:
    def __init__(self, max_gap=60.0):
        self.n = 0
        self.f = {k: [math.inf, -math.inf, 0.0] for k in ('vout_v', 'iout_a', 'pout_w', 'vin_v', 'temp_c')}
        self.ah = 0.0
        self.wh = 0.0
        self.on_s = 0.0
        self.max_gap = max_gap        # longer gaps between samples (link lost) are not integrated
        self._last = None
        self.modes = {}

    def add(self, s):
        self.n += 1
        for k, acc in self.f.items():
            v = s[k]
            acc[0] = min(acc[0], v)
            acc[1] = max(acc[1], v)
            acc[2] += v
        self.modes[s['mode']] = self.modes.get(s['mode'], 0) + 1
        if self._last is not None:
            dt = s['t'] - self._last['t']
            if 0 < dt <= self.max_gap and s['output_on'] and self._last['output_on']:     # trapezoid
                self.ah += (s['iout_a'] + self._last['iout_a']) / 2 * dt / 3600
                self.wh += (s['pout_w'] + self._last['pout_w']) / 2 * dt / 3600
                self.on_s += dt
        self._last = s

    def summary(self):
        out = {'samples': self.n, 'ah': round(self.ah, 6), 'wh': round(self.wh, 6), 'on_s': round(self.on_s, 2),
               'modes': self.modes}
        if self.n:
            out['stats'] = {k: {'min': round(a[0], 4), 'max': round(a[1], 4), 'mean': round(a[2] / self.n, 4)}
                            for k, a in self.f.items()}
        return out


def _gap(every):
    return max(60.0, 3.0 * every)


def _sample(supply):
    d = supply.query()
    out = {k: d[k] for k in ('t',) + SAMPLE_FIELDS}
    out['latency_ms'] = d['latency_ms']
    return out


def _poll(supply, emit, reconnect_for, lost):
    """One status sample, or None after a link error (reported once per outage). lost: dict with 'since'.
    Raises PP300SError once the outage is longer than reconnect_for (None = keep retrying)."""
    try:
        s = _sample(supply)
    except PP300SError as e:
        if e.code not in LINK_ERRORS:
            raise
        now = time.time()
        if lost.get('since') is None:
            lost['since'] = now
            emit({'event': 'link_lost', 'code': e.code, 'message': e.message, 't': round(now, 3)})
        if reconnect_for is not None and now - lost['since'] > reconnect_for:
            raise
        return None
    if lost.get('since') is not None:
        emit({'event': 'reconnected', 'gap_s': round(s['t'] - lost['since'], 2), 't': s['t']})
        lost['since'] = None
    return s


def eeprom_warning(frames, rate_per_h):
    """A warning when a run will send many frames: the firmware saves the front-panel field under the cursor to
    its EEPROM after EVERY valid frame, queries included (docs/PROTOCOL.md, 'EEPROM writes'). frames None =
    unbounded."""
    if frames is not None and frames <= EEPROM_WARN_FRAMES:
        return None
    return ('every frame (status queries too) makes the supply rewrite the setting under its front-panel cursor in '
            'its EEPROM (firmware 1.4.4, no wear levelling): %s at about %.0f/h; EEPROM cells last about 1e6 writes. '
            'Select the DATA (CRL) box on the Digital/Waveform screen during long runs (nothing is saved then), or '
            'poll less often' % ('%d frames planned' % frames if frames is not None else 'unbounded', rate_per_h))


def _query_warning(every, duration):
    n = None if duration is None else duration / max(every, 1e-3)
    return eeprom_warning(None if n is None else int(n), 3600 / max(every, 1e-3))


def watch(supply, emit, every=1.0, count=None, duration=None, reconnect_for=10.0, quiet=False):
    """Status every `every` s; emit({'type': 'sample', ...}) per sample unless quiet -> summary dict."""
    stopper = Stopper()
    stats = Stats(_gap(every))
    t0 = time.time()
    lost = {}
    last = None
    stopped_by = None
    n_planned = count if count is not None else (None if duration is None else duration / max(every, 1e-3))
    w = eeprom_warning(None if n_planned is None else int(n_planned), 3600 / max(every, 1e-3))
    if w and not quiet:
        emit({'event': 'warning', 'warnings': [w]})
    try:
        while True:
            if stopper.check():
                break
            t_tick = time.monotonic()
            try:
                s = _poll(supply, emit, reconnect_for, lost)
            except PP300SError:
                stopped_by = 'link_lost'
                break
            if s is not None:
                stats.add(s)
                last = s
                if not quiet:
                    emit(dict(type='sample', **s))
            if count is not None and stats.n >= count:
                stopped_by = 'count'
                break
            if duration is not None and time.time() - t0 >= duration:
                stopped_by = 'duration'
                break
            if stopper.sleep(every - (time.monotonic() - t_tick)):
                break
    except KeyboardInterrupt:
        stopper.reason = 'interrupt'
    out = {'stopped_by': stopper.reason or stopped_by or 'end', 'elapsed_s': round(time.time() - t0, 2),
           'last': last}
    if w:
        out['warnings'] = [w]
    out.update(stats.summary())
    return out


def default_format(path, fmt=None):
    if fmt:
        return fmt
    return 'jsonl' if path.lower().endswith(('.jsonl', '.json', '.ndjson')) else 'csv'


class LogFile:
    def __init__(self, path, fmt):
        self.path = path
        self.fmt = fmt
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            self.fh = open(path, 'a', newline='', encoding='utf-8')
        except OSError as e:
            raise PP300SError('INVALID_ARG', 'cannot write the log file %s: %s' % (path, e.strerror or e),
                              'give a writable file path (not a folder)') from None
        self.w = None
        if fmt == 'csv':
            self.w = csv.writer(self.fh)
            if new:
                self.w.writerow(CSV_COLUMNS)
        self.rows = 0

    def write(self, s, ah, wh, step=None):
        if self.fmt == 'csv':
            self.w.writerow([s['t'], local_time(s['t']), s['vout_v'], s['iout_a'], s['pout_w'], s['vin_v'],
                             s['temp_c'], s['mode'], int(s['output_on']), round(ah, 6), round(wh, 6),
                             '' if step is None else step])
        else:
            d = dict(s, local_time=local_time(s['t']), ah=round(ah, 6), wh=round(wh, 6))
            if step is not None:
                d['step'] = step
            self.fh.write(json.dumps(d, ensure_ascii=False) + '\n')
        self.rows += 1
        self.fh.flush()

    def event(self, d):
        if self.fmt == 'jsonl':
            self.fh.write(json.dumps(d, ensure_ascii=False) + '\n')
            self.fh.flush()

    def close(self):
        self.fh.close()


def log(supply, emit, out, fmt=None, every=1.0, duration=None, status_every=None, reconnect_for=300.0):
    """Status every `every` s into a CSV/JSONL file (pc_time, local_time, values, cumulative ah/wh) -> summary."""
    fmt = default_format(out, fmt)
    f = LogFile(out, fmt)
    stopper = Stopper()
    stats = Stats(_gap(every))
    t0 = time.time()
    lost = {}
    stopped_by = None
    next_status = t0 + status_every if status_every else None
    ev = {'event': 'started', 'file': os.path.abspath(out), 'format': fmt, 'every_s': every, 'duration_s': duration,
          'port': supply.port, 'pid': os.getpid(), 't': round(t0, 3)}
    w = _query_warning(every, duration)
    if w:
        ev['warnings'] = [w]
    emit(ev)
    f.event({'event': 'started', 't': round(t0, 3), 'port': supply.port, 'every_s': every})
    try:
        while True:
            if stopper.check():
                break
            t_tick = time.monotonic()
            try:
                s = _poll(supply, emit, reconnect_for, lost)
            except PP300SError as e:
                stopped_by = 'link_lost'
                f.event({'event': 'link_lost', 'code': e.code, 't': round(time.time(), 3)})
                break
            if s is not None:
                stats.add(s)
                f.write(s, stats.ah, stats.wh)
            now = time.time()
            if next_status and now >= next_status:
                emit({'event': 'status', 'rows': f.rows, 'elapsed_s': round(now - t0, 1), 'ah': round(stats.ah, 6),
                      'wh': round(stats.wh, 6), 'last': s})
                next_status += status_every
            if duration is not None and now - t0 >= duration:
                stopped_by = 'duration'
                break
            if stopper.sleep(every - (time.monotonic() - t_tick)):
                break
    except KeyboardInterrupt:
        stopper.reason = 'interrupt'
    finally:
        f.event({'event': 'done', 't': round(time.time(), 3)})
        f.close()
    out_d = {'stopped_by': stopper.reason or stopped_by or 'end', 'file': os.path.abspath(out), 'format': fmt,
             'rows': f.rows, 'elapsed_s': round(time.time() - t0, 2)}
    out_d.update(stats.summary())
    return out_d


# ------------------------------------------------------------------ sequence

def _finite_step(st, where):
    for k in ('voltage', 'current', 'seconds'):
        if not math.isfinite(st[k]):
            raise PP300SError('INVALID_ARG', '%s: %s must be a finite number' % (where, k))
    return st


def read_steps(path):
    """Vendor sequence CSV (header '电压(V),电流(A),时间(S)' or none; rows V,A,seconds) -> list of steps."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        raise PP300SError('INVALID_ARG', 'cannot read the sequence file %s: %s' % (path, e.strerror or e),
                          'give an existing CSV file with rows voltage,current,seconds') from None
    text = None
    for enc in ('utf-8-sig', 'gbk', 'latin-1'):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        cells = [c.strip().strip('"') for c in line.split(',')]
        try:
            vals = [float(c) for c in cells[:3]]
        except ValueError:
            if not rows:              # a header line
                continue
            raise PP300SError('INVALID_ARG', '%s line %d: voltage,current,seconds must be numbers: %r'
                              % (path, n, line)) from None
        if len(vals) < 3:
            raise PP300SError('INVALID_ARG', '%s line %d: needs three columns voltage,current,seconds' % (path, n))
        rows.append(_finite_step({'voltage': vals[0], 'current': vals[1], 'seconds': vals[2], 'line': n},
                                 '%s line %d' % (path, n)))
    return rows


def parse_steps(text):
    """'5,2,10; 9,1,20' -> steps."""
    rows = []
    for n, part in enumerate(str(text).replace('\n', ';').split(';'), 1):
        part = part.strip()
        if not part:
            continue
        cells = [c.strip() for c in part.split(',')]
        try:
            v, i, t = (float(c) for c in cells)
        except ValueError:
            raise PP300SError('INVALID_ARG', 'step %d: %r is not voltage,current,seconds' % (n, part)) from None
        rows.append(_finite_step({'voltage': v, 'current': i, 'seconds': t, 'line': n}, 'step %d' % n))
    return rows


def check_steps(supply, steps):
    """Ranges, bench limits and OVP headroom of every step, before anything is sent -> warnings."""
    if not steps:
        raise PP300SError('INVALID_ARG', 'the sequence has no steps')
    known = supply.known()
    for k, st in enumerate(steps):
        _finite_step(st, 'step %d' % (k + 1))
        if st['seconds'] < 0:
            raise PP300SError('INVALID_ARG', 'step %d: seconds must be >= 0' % (k + 1))
        for key in ('voltage', 'current'):
            reg, raw, val = supply.convert(key, st[key])
            supply.check_limits(reg, val)
        try:
            supply.check_ovp_headroom(None, round(st['voltage'], 2), None, known)
        except PP300SError as e:
            raise PP300SError(e.code, 'step %d: %s' % (k + 1, e.message),
                              'set an OVP above the highest step first: openaxpp300s set --ovp %g'
                              % min(30.5, max(round(s['voltage'], 2) + max(0.2, 0.015 * s['voltage']) + 0.01
                                              for s in steps))) from None
    warnings = []
    if 'ovp' not in known:
        warnings.append('OVP unknown to this tool (never sent by it): if it is below a step voltage the output '
                        'trips; set it first with "set --ovp"')
    ko = known.get('ocp')
    if ko and any(s['current'] > ko['value'] for s in steps):
        warnings.append('a step current is above the OCP sent last (%g A): a load drawing it trips OCP'
                        % ko['value'])
    return warnings


def sequence(supply, emit, steps, repeat=1, end='off', every=1.0, log_out=None, fmt=None, reconnect_for=10.0):
    """Run the steps (each: set VSET and ISET, switch the output on, hold `seconds`), `repeat` times (0 = until
    stopped); at the end switch the output off (end='off', default) or leave it (end='keep').

    Safety: every sample is checked against the bench voltage limit (over it: output off, UNSAFE_OPERATION); once a
    step has settled, an output in CV that is not at the step voltage means the setpoints are not in force (Chinese
    screen, front panel): output off, NOT_APPLIED; an output that went off without this sequence switching it off
    (protection trip, front panel, another command) ends the sequence (stopped_by 'output_off') instead of being
    switched on again. On any error the output is switched off (unless end='keep') and the error carries the end
    state."""
    from .device import v_tolerance
    warnings = check_steps(supply, steps)
    total_s = sum(s['seconds'] for s in steps) * repeat if repeat else None
    frames = None if total_s is None else int(total_s / max(every, 1e-3) + FRAMES_PER_STEP * len(steps) * repeat)
    w = eeprom_warning(frames, 3600 / max(every, 1e-3))
    if w:
        warnings.append(w)
    maxv = state.get_limits().get('max_voltage')
    f = LogFile(log_out, default_format(log_out, fmt)) if log_out else None
    stopper = Stopper()
    stats = Stats(_gap(every))
    t0 = time.time()
    lost = {}
    stopped_by = None
    done_steps = 0
    error = None
    ev = {'event': 'started', 'steps': len(steps), 'repeat': repeat, 'end': end, 'port': supply.port,
          'file': os.path.abspath(log_out) if log_out else None, 'pid': os.getpid(), 't': round(t0, 3)}
    if warnings:
        ev['warnings'] = warnings
    emit(ev)
    cycle = 0
    switched_on = False
    try:
        while repeat == 0 or cycle < repeat:
            cycle += 1
            for k, st in enumerate(steps):
                if stopper.check():
                    break
                r = supply.apply({'voltage': st['voltage'], 'current': st['current']}, wait=False)
                if switched_on and not r['status']['output_on']:
                    stopped_by = 'output_off'
                    emit({'event': 'output_off', 'step': k + 1, 'cycle': cycle, 't': round(time.time(), 3),
                          'note': 'the output went off without this sequence switching it off: protection trip '
                                  '(the supply sounds an alarm until a key is pressed on it), front panel or '
                                  'another command; not switched on again'})
                    break
                if not r['status']['output_on']:
                    supply.output(True, wait=False)
                    switched_on = True
                ev = {'event': 'step', 'cycle': cycle, 'step': k + 1, 'voltage': st['voltage'],
                      'current': st['current'], 'seconds': st['seconds'], 't': round(time.time(), 3)}
                if r.get('warnings'):
                    ev['warnings'] = r['warnings']
                emit(ev)
                if f:
                    f.event(ev)
                t_step = time.time()
                checked = False
                while True:
                    t_tick = time.monotonic()
                    try:
                        s = _poll(supply, emit, reconnect_for, lost)
                    except PP300SError:
                        stopped_by = 'link_lost'
                        break
                    if s is not None:
                        stats.add(s)
                        if f:
                            f.write(s, stats.ah, stats.wh, step=k + 1)
                        if maxv is not None and s['vout_v'] > maxv * 1.02 + 0.05:
                            raise PP300SError('UNSAFE_OPERATION', 'step %d: the output reads %g V, above the bench '
                                              'voltage limit %g V' % (k + 1, s['vout_v'], maxv),
                                              'the setpoint in force is not the step voltage (Chinese screen or '
                                              'front panel?)')
                        el = time.time() - t_step
                        if el >= STEP_SETTLE_S and not s['output_on']:
                            stopped_by = 'output_off'
                            emit({'event': 'output_off', 'step': k + 1, 'cycle': cycle, 't': s['t'],
                                  'note': 'the output went off during the step: protection trip (the supply sounds '
                                          'an alarm until a key is pressed on it), front panel or another command'})
                            break
                        if not checked and el >= STEP_SETTLE_S and s['mode'] == 'CV':
                            checked = True
                            if abs(s['vout_v'] - st['voltage']) > v_tolerance(st['voltage']) + 0.05:
                                raise PP300SError('NOT_APPLIED', 'step %d: the output is at %g V in CV, not at the '
                                                  'step voltage %g V' % (k + 1, s['vout_v'], st['voltage']),
                                                  'the supply ignores VSET/ISET on the Chinese screen; check the '
                                                  'screen and the front panel')
                    if time.time() - t_step >= st['seconds']:
                        break
                    left = st['seconds'] - (time.time() - t_step)
                    if stopper.sleep(min(every - (time.monotonic() - t_tick), left)):
                        break
                if stopped_by or stopper.reason:
                    break
                done_steps += 1
            if stopped_by or stopper.reason:
                break
    except KeyboardInterrupt:
        stopper.reason = 'interrupt'
    except PP300SError as e:
        error = e
    finally:
        end_note = None
        end_off_failed = False
        # over the bench limit / setpoints not in force: off even with end='keep'
        if end == 'off' or error is not None and error.code in ('UNSAFE_OPERATION', 'NOT_APPLIED'):
            for attempt in range(3):
                try:
                    supply.output(False, wait=False)
                    end_note = 'output switched off'
                    break
                except PP300SError as e:
                    end_note = 'could not switch the output off: %s' % e.message
                    end_off_failed = True
                    time.sleep(0.2)
            else:
                end_off_failed = True
            if end_note == 'output switched off':
                end_off_failed = False
        else:
            end_note = 'output left as it was'
        if f:
            f.event({'event': 'done', 't': round(time.time(), 3)})
            f.close()
    summary = {'stopped_by': stopper.reason or stopped_by or ('error' if error else 'end'),
               'steps_done': done_steps, 'cycles': cycle, 'end': end_note, 'elapsed_s': round(time.time() - t0, 2)}
    if f:
        summary['file'] = os.path.abspath(log_out)
        summary['rows'] = f.rows
    summary.update(stats.summary())
    if error is not None:
        error.extra = dict(error.extra, **{k: summary[k] for k in ('stopped_by', 'steps_done', 'cycles', 'end')})
        raise error
    if end_off_failed:
        raise PP300SError('PORT_ERROR', 'the sequence ended but the output could not be switched off: %s'
                          % end_note, 'switch it off now: openaxpp300s off', summary)
    return summary
