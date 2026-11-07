"""PP300S: the supply as an object. Status, settings (validated, limit-checked, sent in a safe order, recorded),
output switching with readback, parameter lists, display colours and raw frames.

The supply cannot report its settings: the status reply has only the measured output, input voltage, temperature,
the CC flag and the output flag. So every value this tool sends is recorded (state/last_sent.json) and shown with
the status, and changes that should be visible on the output (VSET with the output on, output on/off) are checked
against the readback.

Never trip a protection: a trip latches a loud alarm that only a key press on the supply clears. The firmware
compares OVP with the measured terminal voltage and OCP with the measured current all the time (output on or off),
OPP with the power and OTP with the temperature. So:
- OVP must sit at least max(0.2 V, 1.5 %) above VSET (VSET = OVP = 3.00 V tripped on hardware), checked against
  the values given and the ones sent last;
- a new OVP/OCP must be above what the terminals show now (plus a margin); one that is below (lowering VSET and OVP
  together) is sent after the setpoints, once the output has come down; OPP must be above the present power and OTP
  above the present temperature (with margins);
- OVP/OCP never go below the front-panel minimums (0.50 V / 0.10 A): 0 trips at once, even with the output off.
Bench limits (state/limits.json, `openaxpp300s limits`): voltage-like values (VSET, OVP, list VSET/OVP) and
current-like values (ISET, OCP, list ISET/OCP) above them are refused, and `output on` / list selection switch the
output off again at once if the measured voltage exceeds the voltage limit (the front panel may have set anything).
Changes from several processes are serialised by a settings lock, so two commands cannot each pass their checks and
together leave VSET above OVP.
"""
from __future__ import annotations

import math
import time

from . import protocol as P
from . import state
from .errors import PP300SError
from .lock import PortLock
from .transport import (DEFAULT_RETRIES, DEFAULT_TIMEOUT, SIM_PORT, Session, discover, resolve_port)

DIGITAL_ORDER = ('opp', 'otp', 'priority', 'ovp', 'ocp', 'voltage', 'current', 'delay', 'rev', 'cut_current')
SETTLE_TIMEOUT = 3.0          # s for the output to reach a new VSET
SOFT_START_S = 1.6            # s from the ON command to the voltage being up (measured ~1.1-1.3 s)
POLL_S = 0.1                  # fast poll while something is expected to change (each query costs an EEPROM write)
SLOW_POLL_S = 1.0             # poll while waiting out DELAY / REV
LONG_WAIT_S = 20.0            # DELAY/REV longer than this: `on` returns after the frame instead of waiting
COLOR_SAVE_S = 0.35           # the supply saves all colours to EEPROM in ~210 ms after a colour frame
OPP_MARGIN_W = 5.0
OTP_MARGIN_C = 5.0


def v_tolerance(target):
    """Readback tolerance for 'settled': spec is 0.1 % + 5 mV for setting and readback each; allow 0.5 % + 20 mV."""
    return 0.005 * abs(target) + 0.02


def ovp_margin(v):
    return 0.01 * abs(v) + 0.05


def ocp_margin(i):
    return 0.01 * abs(i) + 0.02


def ovp_headroom(vset):
    """How far OVP must sit above VSET: a setpoint step overshoots, and VSET 3.00 V with OVP 3.00 V tripped on
    hardware. 0.2 V or 1.5 %, whichever is larger (VSET 30.00 V still fits under the 30.50 V maximum OVP)."""
    return max(0.2, 0.015 * abs(vset))


def min_ovp(vset):
    return round(vset + ovp_headroom(vset) + 0.004, 2)


def ocp_headroom(iset):
    """OCP trips when the current stays at or above it for 100 ms; in CC the current sits at ISET."""
    return max(0.05, 0.02 * abs(iset))


class Supply:
    def __init__(self, port=None, lock_wait=5.0, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES, trace=None):
        self.port, self.port_how = resolve_port(port)
        self.lock_wait = lock_wait
        self.timeout = timeout
        self.retries = retries
        self.trace = trace
        self.frames = []              # every setting frame sent by this object (with its acknowledge)
        self._ok_once = False
        self._settings_depth = 0
        self._settings_lock = None

    # ------------------------------------------------------------------ port access
    def session(self):
        return Session(self.port, self.lock_wait, self.timeout, self.retries, self.trace)

    def _do(self, fn):
        """Run fn(session) in a fresh session; a remembered port that no longer answers is re-discovered once."""
        try:
            with self.session() as s:
                out = fn(s)
        except PP300SError as e:
            if self._ok_once or self.port_how != 'remembered' or e.code not in ('NOT_FOUND', 'NO_REPLY',
                                                                                 'BAD_REPLY', 'PORT_ERROR'):
                raise
            self.port, self.port_how = discover(), 'probe'
            with self.session() as s:
                out = fn(s)
        if not self._ok_once:
            self._ok_once = True
            if self.port != SIM_PORT and self.port_how != 'remembered':
                state.remember_port(self.port)
        return out

    class _Changing:
        """Re-entrant cross-process lock around a change (checks + frames), one per port."""

        def __init__(self, sup):
            self.sup = sup

        def __enter__(self):
            s = self.sup
            if s._settings_depth == 0:
                s._settings_lock = PortLock('settings_%s' % state.port_key(s.port), wait=max(15.0, s.lock_wait))
                s._settings_lock.acquire()
            s._settings_depth += 1
            return s

        def __exit__(self, *exc):
            s = self.sup
            s._settings_depth -= 1
            if s._settings_depth == 0 and s._settings_lock is not None:
                s._settings_lock.release()
                s._settings_lock = None

    def changing(self):
        return Supply._Changing(self)

    def query(self):
        d = self._do(lambda s: s.query())
        d['t'] = round(time.time(), 3)
        return d

    def _write(self, reg, raw, value=None):
        ans = self._do(lambda s: s.write(reg.reg_id, raw))
        rec_value = P.raw_to_value(reg, raw) if value is None else value
        state.record_sent(self.port, reg.key, rec_value, raw)
        item = {'key': reg.key, 'name': reg.name, 'value': rec_value, 'unit': reg.unit, 'raw': raw,
                'id': '0x%02X' % reg.reg_id, 'ack': ans['ack'], 'latency_ms': ans['latency_ms']}
        if ans['attempts'] > 1:
            item['attempts'] = ans['attempts']
        self.frames.append(item)
        return item

    # ------------------------------------------------------------------ status
    def status(self):
        d = self.query()
        d['port'] = self.port
        return d

    def known(self):
        """Values this tool sent last (value, raw, t) by register key."""
        return state.last_sent(self.port)

    def _recent_on(self, known=None):
        """Seconds since this tool switched the output on, if that was less than SOFT_START_S ago, else None."""
        ko = (known or self.known()).get('output')
        if ko and ko['value'] is True:
            dt = time.time() - ko['t']
            if 0 <= dt < SOFT_START_S:
                return dt
        return None

    # ------------------------------------------------------------------ validation
    @staticmethod
    def convert(key, value):
        """(register, raw, effective value) for a user value; INVALID_ARG when out of range."""
        if key not in P.BY_KEY:
            raise PP300SError('INVALID_ARG', 'unknown setting %r' % key,
                              'settings: %s' % ', '.join(sorted(P.BY_KEY)))
        reg = P.BY_KEY[key]
        try:
            raw = P.value_to_raw(reg, value)
        except ValueError as e:
            raise PP300SError('INVALID_ARG', str(e), _range_hint(reg)) from None
        return reg, raw, P.raw_to_value(reg, raw)

    @staticmethod
    def check_limits(reg, value):
        lim = state.get_limits()
        if reg.limit == 'voltage' and lim.get('max_voltage') is not None and value > lim['max_voltage'] + 1e-9:
            raise PP300SError('UNSAFE_OPERATION', '%s %g V is above the bench voltage limit %g V'
                              % (reg.key, value, lim['max_voltage']),
                              'the user set this limit for what is connected (%s); ask the user before raising '
                              'it with "openaxpp300s limits --max-voltage ..."' % (lim.get('note') or 'no note'))
        if reg.limit == 'current' and lim.get('max_current') is not None and value > lim['max_current'] + 1e-9:
            raise PP300SError('UNSAFE_OPERATION', '%s %g A is above the bench current limit %g A'
                              % (reg.key, value, lim['max_current']),
                              'the user set this limit for what is connected (%s); ask the user before raising '
                              'it with "openaxpp300s limits --max-current ..."' % (lim.get('note') or 'no note'))

    @staticmethod
    def check_ovp_headroom(group, vset, ovp, known):
        """Refuse a VSET / OVP pair (given now, or the other one as sent last) that would trip the output: OVP must
        be at least ovp_headroom(VSET) above VSET."""
        if vset is not None and ovp is not None:
            if ovp < vset + ovp_headroom(vset) - 1e-9:
                raise PP300SError('UNSAFE_OPERATION', 'OVP %g V is too close to VSET %g V: the output would trip'
                                  % (ovp, vset), 'give OVP >= %g V (VSET + %g V headroom)'
                                  % (min_ovp(vset), round(ovp_headroom(vset), 2)))
            return
        if vset is not None:
            kv = known.get(_role_key(group, 'ovp'))
            if kv and kv['value'] < vset + ovp_headroom(vset) - 1e-9:
                raise PP300SError('UNSAFE_OPERATION', 'VSET %g V needs OVP >= %g V, but the OVP sent last is %g V '
                                  '(%s): the output would trip' % (vset, min_ovp(vset), kv['value'], _ago(kv['t'])),
                                  'raise the OVP in the same call (e.g. --ovp %g)' % min(30.5, min_ovp(vset)))
        if ovp is not None:
            kv = known.get(_role_key(group, 'voltage'))
            if kv and ovp < kv['value'] + ovp_headroom(kv['value']) - 1e-9:
                raise PP300SError('UNSAFE_OPERATION', 'OVP %g V is too close to the VSET sent last (%g V, %s): the '
                                  'output would trip' % (ovp, kv['value'], _ago(kv['t'])),
                                  'lower the voltage in the same call, or give OVP >= %g V' % min_ovp(kv['value']))

    # ------------------------------------------------------------------ settings
    def apply(self, settings, wait=True):
        """Send several settings in a safe order. settings: {key: value} with keys from protocol.REGISTERS (not
        output/list/colour/cut time; see output(), select_list(), set_list(), set_colors()). -> result dict."""
        settings = {k: v for k, v in settings.items() if v is not None}
        if not settings:
            raise PP300SError('INVALID_ARG', 'nothing to set', 'give at least one setting, e.g. --voltage 5')
        conv = {}
        for k, v in settings.items():
            if k in ('output', 'list') or k.startswith('color_') or k.endswith('_cut_time'):
                raise PP300SError('INVALID_ARG', '%s is not set with apply()' % k)
            reg, raw, val = self.convert(k, v)
            self.check_limits(reg, val)
            conv[k] = (reg, raw, val)
        sent = []
        with self.changing():
            try:
                warnings = []
                known = self.known()
                groups = self._groups(conv)
                for g, roles in groups.items():
                    v, ovp = roles.get('voltage'), roles.get('ovp')
                    self.check_ovp_headroom(g, v[2] if v else None, ovp[2] if ovp else None, known)
                    if v and not ovp and _role_key(g, 'ovp') not in known:
                        warnings.append('OVP%s is unknown to this tool (never sent by it): if it is not at least %g V '
                                        'the output trips when it reaches %g V; send --ovp with the voltage'
                                        % ('' if g is None else ' of list %d' % g, min_ovp(v[2]), v[2]))
                    i = roles.get('current') or _known_item(known, g, 'current')
                    ocp = roles.get('ocp') or _known_item(known, g, 'ocp')
                    if (roles.get('current') or roles.get('ocp')) and i and ocp and ocp[2] < i[2] + ocp_headroom(i[2]):
                        warnings.append('OCP %g A is not above ISET %g A: the output trips (alarm) as soon as a load '
                                        'pulls it into CC; keep OCP >= %g A' % (ocp[2], i[2],
                                                                                 round(i[2] + ocp_headroom(i[2]), 2)))
                before = self.query()
                self._check_opp_otp(conv, before)
                on = before['output_on']
                order, deferred = self._order(conv, groups, before, known)
                for k in order:
                    reg, raw, val = conv[k]
                    sent.append(self._write(reg, raw, val))
                for k in deferred:                    # protections that must wait for the output to come down
                    reg, raw, val = conv[k]
                    self._wait_below(reg, val)
                    sent.append(self._write(reg, raw, val))
            except PP300SError as e:
                if sent:
                    e.extra = dict(e.extra, sent=sent)
                raise
        res = {'sent': sent, 'output_on_before': on}
        vreg = conv.get('voltage')
        try:
            if wait and on and vreg is not None:
                res.update(self._settle(vreg[2]))
            else:
                res['status'] = self.status()
        except PP300SError as e:
            e.extra = dict(e.extra, sent=sent)
            raise
        if on and not res['status']['output_on'] and 'note' not in res:
            warnings.append('the output was on and is off now: a protection trip (the supply sounds an alarm until '
                            'a key is pressed on it), DELAY/REV, or the front panel')
        if warnings:
            res['warnings'] = warnings
        return res

    @staticmethod
    def _check_opp_otp(conv, st):
        if 'opp' in conv:
            w = conv['opp'][2]
            if math.floor(st['pout_w']) >= w - OPP_MARGIN_W:
                raise PP300SError('UNSAFE_OPERATION', 'OPP %d W would trip the output: it delivers %g W now'
                                  % (w, st['pout_w']), 'switch the output off first, or keep OPP above %d W'
                                  % (math.floor(st['pout_w']) + OPP_MARGIN_W))
        if 'otp' in conv:
            c = conv['otp'][2]
            if st['temp_c'] >= c - OTP_MARGIN_C:
                raise PP300SError('UNSAFE_OPERATION', 'OTP %d C would trip the supply: it is at %g C now'
                                  % (c, st['temp_c']), 'let the supply cool down, or choose a higher OTP')

    @staticmethod
    def _groups(conv):
        """{group: {role: (reg, raw, val)}}: group None = Digital screen registers, n = parameter list n."""
        out = {}
        for k, item in conv.items():
            reg = item[0]
            role = k.split('_', 1)[1] if reg.group is not None else k
            out.setdefault(reg.group, {})[role] = item
        return out

    def _order(self, conv, groups, st, known):
        """-> (keys to send now in order, keys to send after the output came down).

        OVP/OCP are compared with the measured terminal voltage/current whatever the output flag says (the firmware
        checks them all the time; after OFF the terminals discharge over ~0.3 s, a battery keeps them up). A
        parameter list that is known not to be the selected one is not compared (its values are not in force)."""
        rank = {k: i for i, k in enumerate(DIGITAL_ORDER)}
        sel = (known.get('list') or {}).get('value')
        early, late, deferred = [], [], []
        for k, (reg, raw, val) in conv.items():
            role = k.split('_', 1)[1] if reg.group is not None else k
            if role in ('ovp', 'ocp'):
                if reg.group is not None and sel is not None and sel != reg.group:
                    early.append(k)
                    continue
                now = st['vout_v'] if role == 'ovp' else st['iout_a']
                margin = ovp_margin(now) if role == 'ovp' else ocp_margin(now)
                if val <= now + margin and reg.group is not None and sel is None:
                    # whether this list is in force is unknown (screen and selection are not reported): waiting
                    # for the output to come down would be pointless if it is not, and a trip if it is
                    raise PP300SError('UNSAFE_OPERATION', 'list %d %s %g %s is not above what the output shows now '
                                      '(%g %s), and the selected list is unknown to this tool: if list %d is in '
                                      'force on the Chinese screen this trips' % (reg.group, reg.name.split('_')[1],
                                                                                 val, reg.unit, now, reg.unit,
                                                                                 reg.group),
                                      'select the list first ("list select N", so the tool knows which one is in '
                                      'force), or switch the output off')
                if val <= now + margin:
                    target = groups.get(reg.group, {}).get('voltage' if role == 'ovp' else 'current')
                    if (target is None or target[2] + margin >= val) and st['output_on']:
                        raise PP300SError('UNSAFE_OPERATION', '%s %g %s would trip the output: it delivers %g %s '
                                          'now' % (reg.name, val, reg.unit, now, reg.unit),
                                          'lower the setpoint in the same call (e.g. --%s and --%s), or switch '
                                          'the output off first' % ('voltage' if role == 'ovp' else 'current',
                                                                    role))
                    deferred.append(k)
                    continue
                early.append(k)
            elif role in ('opp', 'otp', 'priority'):
                early.append(k)
            else:
                late.append(k)

        def key(k):
            reg = conv[k][0]
            role = k.split('_', 1)[1] if reg.group is not None else k
            return (-1 if reg.group is None else reg.group, rank.get(role, 99))
        return sorted(early, key=key) + sorted(late, key=key), sorted(deferred, key=key)

    def _wait_below(self, reg, val, timeout=5.0):
        role = 'ovp' if reg.unit == 'V' else 'ocp'
        t_end = time.time() + timeout
        while True:
            st = self.query()
            now = st['vout_v'] if role == 'ovp' else st['iout_a']
            margin = ovp_margin(now) if role == 'ovp' else ocp_margin(now)
            if val > now + margin:
                return st
            if time.time() >= t_end:
                raise PP300SError('NOT_APPLIED', '%s %g %s not sent: the terminals still show %g %s after %g s'
                                  % (reg.name, val, reg.unit, now, reg.unit, timeout),
                                  'the setpoints were sent; is the supply on the screen these registers act on, or '
                                  'is a battery / charged capacitor on the output? Send the protection again once '
                                  'the output has come down')
            time.sleep(POLL_S)

    def _settle(self, target_v, timeout=SETTLE_TIMEOUT):
        """Wait for the output to reach target_v (or CC, or off) -> {'settled', 'status', 'settle_s'}."""
        t0 = time.time()
        rec = self._recent_on()
        if rec is not None:                        # byte 11 / Vout are meaningless during the soft start
            time.sleep(SOFT_START_S - rec)
        while True:
            st = self.status()
            if not st['output_on']:
                return {'settled': False, 'status': st, 'settle_s': round(time.time() - t0, 2),
                        'note': 'the output is off: a protection trip (the supply sounds an alarm until a key is '
                                'pressed on it), DELAY/REV, or the front panel'}
            if st['mode'] == 'CC':
                return {'settled': True, 'status': st, 'settle_s': round(time.time() - t0, 2),
                        'note': 'in CC: the load draws the current limit, the voltage is below VSET'}
            if abs(st['vout_v'] - target_v) <= v_tolerance(target_v):
                return {'settled': True, 'status': st, 'settle_s': round(time.time() - t0, 2)}
            if time.time() - t0 >= timeout:
                raise PP300SError('NOT_APPLIED', 'the output did not follow VSET %g V: it reads %g V (%s) after '
                                  '%g s' % (target_v, st['vout_v'], st['mode'], timeout),
                                  'the supply ignores VSET/ISET/OVP/OCP/DELAY/REV frames on the Chinese screen '
                                  '(use "list set" there), or VSET was changed on the front panel',
                                  {'status': st})
            time.sleep(POLL_S)

    # ------------------------------------------------------------------ output
    def _limit_guard(self, st, maxv):
        """Switch off and raise when the output is above the bench voltage limit."""
        if maxv is not None and st['vout_v'] > maxv * 1.02 + 0.05:
            self._write(P.BY_KEY['output'], 0, False)
            raise PP300SError('UNSAFE_OPERATION', 'the output rose to %g V, above the bench voltage limit %g V: '
                              'switched off again' % (st['vout_v'], maxv),
                              'the setpoint in force is higher than the limit (front panel, or a parameter list on '
                              'the Chinese screen): set a voltage within the limit first', {'status': self.status()})

    def output(self, on, wait=True, timeout=None):
        """Switch the output; with wait, follow it until the voltage is up (ON) or down (OFF)."""
        reg = P.BY_KEY['output']
        lim = state.get_limits()
        maxv = lim.get('max_voltage')
        with self.changing():
            known = self.known()
            if on:
                kv, ko = known.get('voltage'), known.get('ovp')
                if maxv is not None and kv and kv['value'] > maxv + 1e-9:
                    raise PP300SError('UNSAFE_OPERATION', 'the last VSET sent (%g V) is above the bench voltage '
                                      'limit %g V' % (kv['value'], maxv), 'set a voltage within the limit first')
                if kv and ko and ko['value'] < kv['value'] + ovp_headroom(kv['value']) - 1e-9:
                    raise PP300SError('UNSAFE_OPERATION', 'VSET %g V and OVP %g V (as sent last) would trip the '
                                      'output as it rises' % (kv['value'], ko['value']),
                                      'set --ovp %g (or lower --voltage) first' % min(30.5, min_ovp(kv['value'])))
            sent = self._write(reg, 1 if on else 0, bool(on))
        res = {'sent': [sent]}
        warnings = []
        if on and known.get('voltage') is None:
            warnings.append('VSET unknown to this tool (never sent by it): the output goes to whatever the supply '
                            'has set; check vout in the status')
        if not wait:
            res['status'] = self.status()
            if on and maxv is not None:
                warnings.append('not monitored (--no-wait): the voltage limit check after ON did not run')
            if warnings:
                res['warnings'] = warnings
            return res
        delay = (known.get('delay') or {}).get('value') or 0
        rev = (known.get('rev') or {}).get('value') or 0
        if on and max(delay, rev) > LONG_WAIT_S and (timeout is None or timeout < SOFT_START_S + delay + rev):
            st = self.status()
            res.update({'settled': False, 'status': st, 'note': 'DELAY %d s / REV %d s (as sent last): the output '
                        'switches on about %d s after the command; not waited for' % (delay, rev, delay + rev)})
            if warnings:
                res['warnings'] = warnings
            return res
        if timeout is None:
            timeout = SOFT_START_S + (delay + rev if on else 0) + 2.0
        t0 = time.time()
        kv = known.get('voltage')
        # the voltage in force: the Digital VSET, or (Chinese screen) the selected list's VSET -- the screen cannot
        # be read, so either known one counts
        cands = [kv['value']] if kv else []
        ksel = known.get('list')
        if ksel is not None and isinstance(ksel.get('value'), int):
            klv = known.get('list%d_voltage' % ksel['value'])
            if klv:
                cands.append(klv['value'])
        last = prev = None
        while True:
            st = self.status()
            last = st
            el = time.time() - t0
            if on:
                self._limit_guard(st, maxv)
                up = False
                if st['output_on'] and el >= SOFT_START_S - 0.5:
                    if st['mode'] == 'CV':
                        stable = (prev is not None and prev['mode'] == 'CV' and prev['output_on']
                                  and abs(prev['vout_v'] - st['vout_v']) < 0.01 + 0.002 * st['vout_v'])
                        if any(abs(st['vout_v'] - c) <= v_tolerance(c) for c in cands):
                            up = True
                        elif not cands:            # VSET unknown: a stable CV reading after soft start
                            up = el >= SOFT_START_S and stable
                        else:                      # stable, but at none of the known setpoints
                            up = el >= SOFT_START_S + 1.0 and stable
                            if up:
                                res['note'] = ('the output settled at %g V, not at the VSET sent (%s): a parameter '
                                               'list (Chinese screen) or the front panel sets the voltage'
                                               % (st['vout_v'], ' / '.join('%g V' % c for c in cands)))
                    elif st['mode'] == 'CC':
                        up = el >= SOFT_START_S and st['vout_v'] > 0.02
                if up:
                    res.update({'settled': True, 'on_after_s': round(el, 2), 'status': st})
                    if not cands:
                        res['note'] = 'VSET unknown to this tool; the output is at %g V' % st['vout_v']
                    break
            elif not st['output_on'] and st['vout_v'] < 0.05:
                res.update({'settled': True, 'off_after_s': round(el, 2), 'status': st})
                break
            prev = st
            if el >= timeout:
                res.update({'settled': False, 'status': last})
                if on and not last['output_on']:
                    res['note'] = ('the output flag is still off after %.1f s: DELAY/REV running, a protection trip '
                                   '(the supply then sounds an alarm), VSET above OVP, or a cut-off on the Chinese '
                                   'screen (cut-off time counts the on-time since power-up; silent)' % timeout)
                elif on:
                    res['note'] = 'the output is on but has not reached the expected voltage within %.1f s' % \
                        timeout
                else:
                    res['note'] = 'the output is not fully down yet (a capacitive load or battery on the output)'
                break
            waiting = on and not st['output_on'] and el < delay + rev
            time.sleep(SLOW_POLL_S if waiting else POLL_S)
        if warnings:
            res['warnings'] = warnings
        return res

    # ------------------------------------------------------------------ Chinese screen parameter lists
    def _check_select(self, n, known, st):
        """Refuse making list n active when its values (as sent by this tool) are unknown where it matters, over the
        bench limits, or would trip the output that is on now."""
        kv, ko = known.get('list%d_voltage' % n), known.get('list%d_ovp' % n)
        ki, kc = known.get('list%d_current' % n), known.get('list%d_ocp' % n)
        lim = state.get_limits()
        if lim.get('max_voltage') is not None:
            if kv is None:
                raise PP300SError('UNSAFE_OPERATION', 'list %d voltage is unknown to this tool and a bench voltage '
                                  'limit is set' % n, 'send it first: list set %d --voltage V --ovp V' % n)
            for k in (kv, ko):
                if k is not None:
                    self.check_limits(P.BY_KEY['list%d_voltage' % n], k['value'])
        if lim.get('max_current') is not None:
            for k in (ki, kc):
                if k is not None:
                    self.check_limits(P.BY_KEY['list%d_current' % n], k['value'])
        if kv and ko:
            self.check_ovp_headroom(n, kv['value'], ko['value'], {})
        if st['output_on']:
            if kv is None or ko is None:
                raise PP300SError('UNSAFE_OPERATION', 'the output is on and list %d voltage/OVP are unknown to this '
                                  'tool: on the Chinese screen they take effect at once' % n,
                                  'switch the output off first, or send them: list set %d --voltage V --ovp V' % n)
            if ko['value'] <= st['vout_v'] + ovp_margin(st['vout_v']):
                raise PP300SError('UNSAFE_OPERATION', 'list %d OVP %g V is below the present output %g V: '
                                  'selecting it would trip' % (n, ko['value'], st['vout_v']),
                                  'switch the output off first')

    def select_list(self, n):
        reg, raw, val = self.convert('list', n)
        with self.changing():
            st = self.query()
            self._check_select(int(n), self.known(), st)
            sent = [self._write(reg, raw, val)]
        res = {'sent': sent}
        if st['output_on']:
            maxv = state.get_limits().get('max_voltage')
            t_end = time.time() + 1.0
            while time.time() < t_end:
                cur = self.status()
                self._limit_guard(cur, maxv)
                time.sleep(0.25)
        res['status'] = self.status()
        return res

    def set_list(self, n, select=False, voltage=None, current=None, ovp=None, ocp=None, cut_current=None,
                 cut_time=None):
        """Write values of parameter list n (values first, the list selection last)."""
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 4:
            raise PP300SError('INVALID_ARG', 'parameter list must be 0..4')
        vals = {'voltage': voltage, 'current': current, 'ovp': ovp, 'ocp': ocp, 'cut_current': cut_current}
        settings = {'list%d_%s' % (n, k): v for k, v in vals.items() if v is not None}
        fields = None
        if cut_time is not None:
            try:
                fields = P.parse_cut_time(cut_time)
            except ValueError as e:
                raise PP300SError('INVALID_ARG', str(e)) from None
            fields = dict({'h': 0, 'min': 0, 's': 0}, **fields)
            if fields['h'] * 3600 + fields['min'] * 60 + fields['s'] == 0:
                raise PP300SError('INVALID_ARG', 'cut-off time 0:0:0 switches the output off as soon as it turns on',
                                  'use "off" (99:60:60) to disable the timer')
        if not settings and fields is None and not select:
            raise PP300SError('INVALID_ARG', 'nothing to set for list %d' % n,
                              'give --voltage, --current, --ovp, --ocp, --cut-current, --cut-time or --select')
        res = {'sent': []}
        warnings = []
        with self.changing():
            if settings:
                r = self.apply(settings, wait=False)
                res['sent'] += r['sent']
                warnings += r.get('warnings', [])
            if fields is not None:
                # hours to 99 first, then minutes, seconds, then the real hours: every intermediate total is at
                # least the final one (the on-time counter keeps running, a shorter total could cut the output off)
                reg = P.BY_KEY['list%d_cut_time' % n]
                plan = [('h', 99)] if fields['h'] != 99 else []
                plan += [('min', fields['min']), ('s', fields['s']), ('h', fields['h'])]
                for unit, v in plan:
                    res['sent'].append(self._write(reg, P.cut_raw(unit, v), '%d %s' % (v, unit)))
                state.record_sent(self.port, reg.key, '%d:%02d:%02d' % (fields['h'], fields['min'], fields['s']),
                                  None)
            if select:
                try:
                    r = self.select_list(n)
                except PP300SError as e:
                    if res['sent']:
                        e.extra = dict(e.extra, sent=res['sent'])
                    raise
                res['sent'] += r['sent']
        res['status'] = self.status()
        if warnings:
            res['warnings'] = warnings
        return res

    # ------------------------------------------------------------------ display colours
    def set_colors(self, colors):
        """colors: {element: colour} with elements from protocol.COLOR_DEFAULTS. The supply saves all ten colours
        to its EEPROM (~210 ms) after a colour frame, and a frame that arrives during that save can miss it; so the
        last frame is sent again after COLOR_SAVE_S."""
        if not colors:
            raise PP300SError('INVALID_ARG', 'no colours given', 'elements: %s' % ', '.join(P.COLOR_DEFAULTS))
        conv = []
        for el, c in colors.items():
            if el not in P.COLOR_DEFAULTS:
                raise PP300SError('INVALID_ARG', 'unknown display element %r' % el,
                                  'elements: %s' % ', '.join(P.COLOR_DEFAULTS))
            try:
                raw = P.parse_color(c)
            except ValueError as e:
                raise PP300SError('INVALID_ARG', str(e)) from None
            conv.append((P.BY_KEY['color_' + el], raw))
        sent = []
        with self.changing():
            for reg, raw in conv:
                item = self._write(reg, raw, '0x%04X' % raw)
                item['rgb'] = P.rgb565_to_hex(raw)
                sent.append(item)
            time.sleep(COLOR_SAVE_S)
            reg, raw = conv[-1]
            item = self._write(reg, raw, '0x%04X' % raw)
            item['note'] = 'sent again so the EEPROM save includes every colour'
            sent.append(item)
        return {'sent': sent}

    # ------------------------------------------------------------------ raw frames
    def raw(self, reg_id, data, unsafe=False):
        """One frame. Without unsafe, documented registers go through the guarded paths (apply, output,
        select_list, set_colors) so raw never bypasses ranges, limits or the trip checks."""
        if not 0 <= reg_id <= 0xFF or not 0 <= data <= 0xFFFF:
            raise PP300SError('INVALID_ARG', 'id must be 0..0xFF and data 0..0xFFFF')
        if reg_id == P.QUERY_ID:
            if data != 0 and not unsafe:
                raise PP300SError('UNSAFE_OPERATION', 'the status query takes data 0', 'use data 0')
            return {'reply': self.query()}
        reg = P.BY_ID.get(reg_id)
        if unsafe:
            with self.changing():
                if reg is not None:
                    item = self._write(reg, data)
                else:
                    ans = self._do(lambda s: s.write(reg_id, data))
                    item = {'key': None, 'id': '0x%02X' % reg_id, 'raw': data, 'ack': ans['ack'],
                            'latency_ms': ans['latency_ms']}
            return {'sent': [item], 'unsafe': True}
        if reg is None:
            raise PP300SError('UNSAFE_OPERATION', 'register 0x%02X is not documented' % reg_id,
                              'unknown registers store nothing on firmware 1.4.4 but still make the supply write its '
                              'EEPROM; --unsafe sends it, only with the user\'s explicit approval')
        if not reg.raw_min <= data <= reg.raw_max:
            raise PP300SError('INVALID_ARG', 'data %d out of %s range %d..%d' % (data, reg.name, reg.raw_min,
                                                                               reg.raw_max), _range_hint(reg))
        if reg.key == 'output':
            return dict(self.output(bool(data), wait=True), routed='output')
        if reg.key == 'list':
            return dict(self.select_list(data), routed='list select')
        if reg.kind == 'rgb565':
            return dict(self.set_colors({reg.key[len('color_'):]: data}), routed='color')
        if reg.kind == 'cut':
            try:
                P.value_to_raw(reg, data)
            except ValueError as e:
                raise PP300SError('INVALID_ARG', str(e), 'cut-off time word: low byte value, high byte field '
                                  '(0 s 0..60, 1 min 0..60, 2 h 0..99)') from None
            with self.changing():
                return {'sent': [self._write(reg, data)]}
        return dict(self.apply({reg.key: P.raw_to_value(reg, data)}, wait=False), routed='set')


def _role_key(group, role):
    return role if group is None else 'list%d_%s' % (group, role)


def _known_item(known, group, role):
    k = known.get(_role_key(group, role))
    return (None, k.get('raw'), k['value']) if k and isinstance(k.get('value'), (int, float)) else None


def _ago(t):
    dt = time.time() - t
    if dt < 120:
        return '%.0f s ago' % dt
    if dt < 7200:
        return '%.0f min ago' % (dt / 60)
    return '%.1f h ago' % (dt / 3600)


def _range_hint(reg):
    if reg.kind == 'opp':
        return 'OPP: 10..310 W in 10 W steps'
    if reg.kind == 'otp':
        return 'OTP: 60, 70, 80, 90 or 100 C'
    if reg.kind == 'enum':
        return '%s: %s' % (reg.key, ' or '.join(reg.choices))
    if reg.kind == 'cut':
        return 'cut-off time: hours 0..99, minutes 0..60, seconds 0..60'
    return '%s: %s..%s %s' % (reg.key, P._fmt(reg.value_min), P._fmt(reg.value_max), reg.unit)
