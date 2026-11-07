# openaxpp300s

English | [简体中文](README.zh-CN.md)

An open Python package, command-line interface and MCP server for the **AX PP300S buck-boost bench power supply**
(0 to 30 V, 0 to 10.1 A, 300 W), driven over the supply's USB serial port. It is written so that an AI agent (Claude
Code, or any MCP client) can set voltage and current, switch the output, read back what the terminals deliver, log
for hours and run timed sequences by itself, without the vendor PC app. Every command prints one JSON object per
line, and every error has a stable code, a message, an exit code and, for most errors, a hint.

The protocol was taken from the vendor documents, from a disassembly of the supply's firmware (V1.4.4) and from
measurements on the real supply; see `docs/PROTOCOL.md`.

This project is **not affiliated with AxinDIY**, the maker of the supply. "AX PP300S" is their product name.

## Status and tested hardware

Version 0.1.0 (beta). Verified on the real supply on 2026-11-07, in two sessions: an AX PP300S with firmware V1.4.4,
a Windows 11 PC, a CH340 USB serial chip. In the second session the supply ran from a 65 W USB-PD charger (about
19.8 V) on its Type-C power input, with a Fluke 289 and an electronic load as references.

Verified on the hardware:

- port discovery, `status`, `selftest`;
- `set` of VSET, ISET, OVP, OCP, DELAY, REV, OPP and OTP (each one is acknowledged by the supply);
- `on` and `off`, also into a load (CV and CC);
- VSET accuracy from 0.5 V to 30 V against a Fluke 289 with no load, including boost to 20, 24 and 30 V from the 20 V
  input (see [Accuracy](#accuracy));
- the CC limit (ISET) against the Fluke 289 from 0.1 A to 1 A at about 3 V, and the current in CV under load (0.5 A)
  (see [Accuracy](#accuracy));
- lowering VSET and OVP together with the output on (no load), and lowering ISET and OCP together while 1 A flowed into
  a load (no trip in either case; the setpoint is sent first, the protection after the output has come down);
- DELAY and REV behaviour, `watch`, `sequence`, `raw` queries and the refusals (OVP headroom, an undocumented or
  out-of-range `raw` frame);
- the **MCP server against the real supply**: ports, status, selftest, set (including a refused set), output, watch,
  log_start / job_status / job_stop, sequence_start;
- the Chinese screen and its parameter lists: writing and selecting lists, `on` into the selected list's voltage, a
  list change with the output on, the refusal of a list whose OVP is too low, the cut-off current and the cut-off
  time, and that Digital-screen registers (VSET, DELAY) have no effect there;
- display colours (`color set`, `color reset`);
- CV/CC priority (`--priority`);
- link timing (see [Timing](#timing)).

No protection tripped in the second session. The loads used were small: up to about 1.2 A and 15 W.

**Not yet verified on the hardware:**

- OCP, OPP and OTP trips (only OVP tripped, twice, on the first day);
- the on-device settings menu and its effects;
- currents above about 1.2 A and powers above about 15 W;
- the DC5525 power input (the second session used the Type-C power input);
- CC accuracy at other output voltages (the CC table below was measured at about 3 V);
- the EEPROM writes themselves (see [Does a status query really write the EEPROM?](#does-a-status-query-really-write-the-eeprom)).

Linux and macOS: the code has no Windows-only paths in the serial layer, but only Windows was tried. The test suite
runs against the simulator (see [Simulator](#simulator)); no test opens a real port.

## Install

Requirements: Python 3.10 or newer, the supply, a USB cable. The only dependency is `pyserial`.

From PyPI:

```
pip install openaxpp300s
```

Or from a checkout (for development): `pip install -e .` (add `".[test]"` for the tests). Either way the install
provides the commands `openaxpp300s` and `openaxpp300s-mcp`; `python -m openaxpp300s ...` is the same as
`openaxpp300s ...`. Releases: a tag `vX.Y.Z` runs `.github/workflows/release.yml` (tests, build, GitHub release,
PyPI by trusted publishing).

## Connecting the supply

Use the supply's **serial Type-C port** (the one for data), not the power-input port. The supply appears as a
CH340 USB serial device (VID:PID 1A86:7523) and gets a COM port number on Windows (COMx). Install the WCH CH340
driver if Windows does not do it by itself.

- The supply must be switched on and showing its normal screen (not the white-screen bootloader).
- Close the vendor PC app, a serial terminal and FlyMcu first: only one program can hold the port.
- Opening the port does not reset the supply and does not change its output.

Port discovery:

```
openaxpp300s ports --probe
```

lists the serial ports and asks each USB one (Bluetooth ports are skipped) for a status reply; the port that answers
is remembered for later commands. Without `--port`, the port is taken from `$OPENAXPP300S_PORT`, else the one that answered last, else the
first USB serial port that answers like a PP300S. To choose explicitly: `--port COMx` (for example `--port COM14`).

## Quick start

```
openaxpp300s ports --probe
openaxpp300s selftest
openaxpp300s limits --max-voltage 12 --max-current 3 --note "test board"
openaxpp300s set -V 5 -I 1 --ovp 6 --ocp 2
openaxpp300s on
openaxpp300s status
openaxpp300s off
```

Set the bench limits first (see [Bench limits](#bench-limits)). Add `--sim` to any command to try it against the
virtual supply, without hardware.

A real `--sim` run of `set`:

```
$ openaxpp300s --sim set -V 5 -I 1 --ovp 6 --ocp 2
{"ok":true,"cmd":"set","port":"SIM","sent":[
 {"key":"ovp","name":"OVP","value":6.0,"unit":"V","raw":600,"id":"0x02","ack":"01","latency_ms":1.9},
 {"key":"ocp","name":"OCP","value":2.0,"unit":"A","raw":200,"id":"0x03","ack":"01","latency_ms":5.0},
 {"key":"voltage","name":"VSET","value":5.0,"unit":"V","raw":500,"id":"0x00","ack":"01","latency_ms":5.2},
 {"key":"current","name":"ISET","value":1.0,"unit":"A","raw":100,"id":"0x01","ack":"01","latency_ms":5.9}],
 "output_on_before":false,"status":{...}}
```

(Line breaks added here; the tool prints one line.) The protections that stay above the output go first, then the
setpoints. `on` and `status`:

```
$ openaxpp300s --sim on
{"ok":true,"cmd":"on","port":"SIM","sent":[{"key":"output","name":"ON/OFF","value":true,"unit":"","raw":1,
 "id":"0x08","ack":"01","latency_ms":2.2}],"settled":true,"on_after_s":1.12,"status":{"vout_v":5.0,"iout_a":0.0,
 "pout_w":0.0,"vin_v":19.8,"temp_c":28.5,"output_on":true,"mode":"CV","cc_flag":0,"output_flag":1,...}}
```

`--text` prints a short human-readable form instead of JSON:

```
$ openaxpp300s --sim --text status
SIM  output OFF  OFF
out  0.000 V  0.000 A  0.00 W
in   19.80 V   temp 28.5 C   (reply 47.3 ms)
```

## CLI reference

Global options, before or after the subcommand:

| Option | Meaning |
|---|---|
| `-p`, `--port PORT` | serial port (default: `$OPENAXPP300S_PORT`, the port that answered last, else the first USB port answering like a PP300S) |
| `--sim` | use the virtual supply (also `OPENAXPP300S_SIM=1`) |
| `--text` | human-readable output instead of JSON |
| `--verbose` | print every frame sent and received on stderr |
| `--lock-wait S` | seconds to wait for another command using the port (default 5) |
| `--timeout S` | seconds to wait for each answer (default 0.3) |
| `--retries N` | resends of a frame without an answer (default 2) |
| `--version` | print the version (before the subcommand only) |

Commands:

| Command | What it does |
|---|---|
| `ports [--probe] [--all]` | list serial ports; `--probe` asks each USB port for a PP300S status; `--all` also probes non-USB and Bluetooth ports (slow) |
| `status` | measured output (V, A, W), input voltage, temperature, CV/CC, output state, plus the values this tool sent last and the bench limits; a note when the output is on but this tool did not switch it on (see below) |
| `set` | send settings in a safe order (options below) |
| `on [--no-wait] [--wait-timeout S]` | switch the output on (honours DELAY and REV) and wait until the voltage is up (see [On, settling and the Chinese screen](#on-settling-and-the-chinese-screen)); with DELAY or REV (as sent last) over 20 s it returns without waiting, unless `--wait-timeout` is given |
| `off [--no-wait] [--wait-timeout S]` | switch the output off and wait until it is down |
| `list select N` / `list set N ...` | the five parameter lists 0..4 of the Chinese screen (see below) |
| `color set ELEMENT=COLOUR ...` / `color reset` / `color list` | display colours of the Digital screen (firmware 1.4.4 or newer; saved in the supply's EEPROM) |
| `watch [--every S] [-n COUNT] [--for DUR] [--quiet] [--reconnect-for DUR]` | status lines every `--every` s (default 1; 10 samples unless `-n` or `--for` is given) as JSONL, then a summary |
| `log [-o FILE] [--format csv\|jsonl] [--every S] [--duration DUR] [--status-every DUR] [--reconnect-for DUR]` | log the status to CSV or JSONL with Ah and Wh until `--duration` or `stop` |
| `sequence [FILE] [--steps "5,2,10; 9,1,20"] [--repeat N] [--end off\|keep] [--every S] [--log FILE] [--format csv\|jsonl] [--dry-run]` | timed voltage/current steps (CSV like the vendor template: `voltage,current,seconds`); `--repeat 0` runs until `stop` |
| `stop [--pid PID] [--only watch\|log\|sequence]` | stop running `watch`, `log` and `sequence` commands cleanly |
| `running` | list the running ones |
| `limits [--max-voltage V] [--max-current A] [--note TEXT] [--clear]` | show or change the bench limits |
| `registers` | the register table: ids, ranges, screens |
| `raw ID DATA [--unsafe]` | send one frame (decimal or `0x..`); ID `0xFF` is the status query. Without `--unsafe`, documented registers go through the checks of `set`, `on`, `list` and `color`, and undocumented ones are refused; `--unsafe` sends the frame unchecked (no ranges, no bench limits) |
| `selftest` | read-only checks of the link and the supply (status replies, latency, plausible values, a bad-checksum frame ignored); never changes a setting; exit code 1 when a check fails |

`set` options:

| Option | Range and step |
|---|---|
| `-V`, `--voltage` | VSET, 0 to 30.00 V, 10 mV steps |
| `-I`, `--current` | ISET, 0.01 to 10.10 A, 10 mA steps |
| `--ovp` | over-voltage protection, 0.5 to 30.50 V |
| `--ocp` | over-current protection, 0.1 to 10.50 A |
| `--delay` | DELAY, 0 to 9999 s: after `on`, the output switches on this much later (0 = off) |
| `--rev` | REV, 0 to 9999 s: after `on` (and DELAY), the output comes on after REV s, then toggles every REV s (0 = off) |
| `--opp` | over-power protection, 10 to 310 W in 10 W steps |
| `--otp` | over-temperature protection, 60 to 100 C in 10 C steps |
| `--priority cv\|cc` | CV first or CC first (firmware 1.4.2 or newer; factory default is CC first; see [CV first or CC first](#cv-first-or-cc-first)) |
| `--no-wait` | do not wait for the output to follow a new VSET |

With the output on, `set` waits until the output follows a new VSET (up to 3 s, else `NOT_APPLIED`). A new OVP or OCP
that is not above what the terminals show now (plus a small margin) is refused while the output is on, unless the same
call lowers VSET (or ISET) below it: then the tool sends the setpoint first, waits for the output to come down, and
only then sends the protection. With the output off it waits for the terminals to discharge first.

`list select N` makes list N of the **Chinese screen** the active one; `list set N` writes list N: `--voltage`,
`--current`, `--ovp`, `--ocp`, `--cut-current A` (the output goes off after the current stays below it for about 6 s
by the firmware, 5.5 to 6 s measured, 5 s by the manual; 0 = off), `--cut-time` (the output goes off when the
accumulated on-time reaches it: `1h30m`, `45m`, `50s`, `H:M:S`, or `off` = 99:60:60; 0:0:0 is refused) and `--select`
(also make it the active list, sent after the values). VSET, ISET, OVP, OCP, DELAY and REV act on the Digital and Waveform screens only, the
list registers on the Chinese screen only; OPP, OTP and the CV/CC priority act on every screen. The supply
acknowledges and stores every frame on any screen, so a register for another screen simply has no effect. This is
verified on the hardware: on the Chinese screen a Digital VSET (and DELAY) is ignored, and with the output on a
`set -V` ends with `NOT_APPLIED` after 3 s.

Notes on the Chinese screen (measured on the hardware unless marked):

- A list value written over serial lives in RAM only and is lost at power-off, unless it belongs to the selected list
  and the front-panel cursor sits on that field. The list **selection** is saved: with the cursor in its default
  position on the Chinese screen, every frame saves it. (Firmware disassembly; power-off was not tried.) Write the
  values first and select last (`--select` does this).
- Changing the values of the selected list with the output on moves the output at once (2.0 V to 2.4 V followed
  immediately). `list select N` is refused before anything is sent when the OVP of list N (as sent last) is not above
  the voltage at the terminals now (plus a small margin), because the output would trip; raise that list's OVP first.
- The cut-offs are **silent**: the output just switches off and the supply does **not** beep, unlike a protection
  trip. With `--cut-current 0.1` and no current flowing, the output switched off after about 5.5 to 6 s. On the serial
  link it looks like a plain "output off"; when this tool had switched the output on, `status` adds a note that lists
  a cut-off among the possible causes.
- The cut-off time compares the **accumulated on-time** since power-up (or since the last time cut-off, or since the
  statistics were cleared), not the time since the last `on`. With a cut-off time of 10 s, the first `on` after setting
  it was cut at once, because the counter already held the on-time accumulated since power-up; after a time cut-off the
  counter restarts, and the next `on` stayed on for 9 to 10 s.

### On, settling and the Chinese screen

`on` waits until the output voltage is up. It counts the output as settled when a CV reading is at the Digital VSET
or at the VSET of the selected list (each as sent last), so on the Chinese screen `on` accepts the selected list's
voltage (measured: 1.9975 V for a list of 2.0 V). If the reading is at neither, but is stable in CV 1 s after the soft
start, `on` returns with a note saying so. If the output flag is still off when the wait ends, the note names a
Chinese-screen cut-off among the possible reasons (cut-offs are silent, and the on-time counts since power-up). Into a
CC load `on` settled in CC after about 1.7 s.

`status` adds a note when the output is **on but this tool did not switch it on**: someone pressed the output key on
the front panel, or another program switched it. The settings in force may then differ from `last_sent`. (The note
was added after this happened on the hardware: the user pressed the output key three times, and the meter showed the
output on without any `on` frame from the tool.)

### CV first or CC first

`--priority cc` (the factory default) limits the switch-on current overshoot; `--priority cv` lets the current
overshoot ISET for a moment. Measured switching on into a 3 V constant-voltage load with VSET 5 V and ISET 0.5 A, from
the supply's read-back polled at 20 Hz: CV first peaked at 0.811 A and 4.263 V, CC first at 0.541 A and 3.733 V (about
60 % over ISET against about 8 %). With CV first, an OCP close to ISET could trip during that overshoot (OCP needs
100 ms). A priority sent over serial is lost at power-off unless the settings menu is exited afterwards (firmware).

### Testing the current limit with an electronic load

To test CC (ISET) with an electronic load, put the load in **constant-voltage mode** at a voltage below VSET, or use
a real resistor. Do **not** use constant-resistance mode: when the supply has to limit the current, the load's CR loop
and the supply's CC loop fight, and the current jumps between about 0 and 1.2 A every 0.5 to 2 s while the supply sits
in CV most of the time (the multimeter saw it too). Even so, the charge that `log` recorded over 10 s in that state
(10 Ohm, VSET 12 V, ISET 1 A) was 0.00152 Ah, within 4 % of the meter (mean current times time: 0.00158 Ah). In CV
mode the CC limit is stable and linear.

`sequence --dry-run` checks the steps against ranges, bench limits and OVP headroom without sending any setting:

```
$ openaxpp300s --sim sequence --steps "5,1,2;9,1,2" --dry-run
{"ok":false,"cmd":"sequence","error":{"code":"UNSAFE_OPERATION","message":"step 2: VSET 9 V needs OVP >= 9.2 V,
 but the OVP sent last is 6 V (4 s ago): the output would trip","hint":"set an OVP above the highest step first:
 openaxpp300s set --ovp 9.21"}}
```

### Watching and logging

`watch` prints one JSON line per sample and a summary at the end:

```
{"type":"sample","t":1794040307.969,"vout_v":0.0,"iout_a":0.0,"pout_w":0.0,"vin_v":19.8,"temp_c":28.5,"mode":"OFF","output_on":false,"latency_ms":13.9}
{"ok":true,"cmd":"watch","event":"done","stopped_by":"count","elapsed_s":0.18,"last":{...},"samples":2,"ah":0.0,
 "wh":0.0,"on_s":0.0,"modes":{"OFF":2},"stats":{"vout_v":{"min":0.0,"max":0.0,"mean":0.0},...}}
```

`log` writes a file (default `data/log_<time>.csv`, or `.jsonl`) with the CSV columns `pc_time, local_time, vout_v,
iout_a, pout_w, vin_v, temp_c, mode, output_on, ah, wh, step`. It reconnects for up to `--reconnect-for` (default
5 min; `watch`: 10 s) when the supply stops answering. `watch`, `log` and `sequence` can run in the background;
`running` lists them and `stop` ends them. By the firmware, every status query makes the supply rewrite one EEPROM field (unless the
DATA box is selected), so do not poll faster than you need (see [Safety](#safety)).

## Output format

Every command prints JSON, one object per line, on stdout:

- success: `{"ok":true,"cmd":"...", ...}`;
- failure: `{"ok":false,"cmd":"...","error":{"code":"...","message":"...","hint":"..."}}` (no `hint` when there is
  none), also on stdout, and a non-zero exit code;
- `watch` prints sample lines (`"type":"sample"`); `log` and `sequence` print event lines (`"event":"started"`,
  `"step"`, `"link_lost"`, ...); all three end with a summary line carrying `"event":"done"`.

Units are SI: V, A, W, s, C. Timestamps `t` are Unix time in seconds.

## Errors and exit codes

| Code | Exit | Meaning and hint |
|---|---|---|
| `INVALID_ARG` | 2 | bad option or value, or a value outside the supply's range; the hint gives the range |
| `NOT_FOUND` | 3 | no PP300S answered on any USB serial port, or the named port does not exist; plug the supply in by its serial Type-C port, switch it on, close the vendor app, or give `--port COMx` |
| `BUSY` | 4 | the port is held by another program (close the vendor PC app, a serial terminal or FlyMcu and retry), or another openaxpp300s command kept it longer than `--lock-wait` |
| `NOT_APPLIED` | 5 | the supply acknowledged but the read-back shows no effect; typically the supply is on the wrong screen, or a charged capacitor or battery holds the output |
| `NO_REPLY` | 6 | no answer: supply off, in its bootloader, or the wrong port; try `ports --probe` |
| `BAD_REPLY` | 6 | an answer with a bad header, tail or checksum; another program may be talking to the supply |
| `PORT_ERROR` | 6 | serial I/O error (cable unplugged, driver error); the command was retried after reopening the port |
| `UNSAFE_OPERATION` | 7 | refused for safety, for example: over the bench limits (or the output rose above the voltage limit and was switched off again), an OVP closer to VSET than the headroom rule allows, an OVP or OCP not above what the terminals show now, an OPP or OTP within 5 W or 5 C of the present power or temperature, or an undocumented register in `raw` |
| (other) | 1 | any other failure (`INTERNAL`), and a `selftest` with a failed check |

A `watch`, `log` or `sequence` stopped on request (`stop`, Ctrl+C) ends with exit code 0 and the summary it had. Any
other command interrupted with Ctrl+C exits with code 130.

## Bench limits

`openaxpp300s limits --max-voltage 12 --max-current 3 --note "what is connected"` stores a maximum voltage (0 to
30 V) and a maximum current (0 to 10.1 A) in `state/limits.json`. They apply to every command and every MCP call,
except `raw --unsafe`:

- voltage-like values (VSET, OVP, list VSET and OVP) and current-like values (ISET, OCP, list ISET and OCP) above
  them are refused with `UNSAFE_OPERATION`;
- `on` is refused when the VSET sent last is above the voltage limit; with a voltage limit set, a list selection is
  refused when that list's voltage is unknown to the tool;
- while `on` follows the output (not with `--no-wait`), and for 1 s after a list selection with the output on, the
  output is switched off again at once if the measured voltage exceeds the voltage limit by more than 2 % + 50 mV
  (the front panel may have set anything);
- sequences are checked step by step before the first step starts, and every sample of a running sequence is
  checked against the voltage limit.

```
$ openaxpp300s --sim limits --max-voltage 12 --max-current 3 --note "test board"
{"ok":true,"cmd":"limits","changed":true,"limits":{"max_voltage":12.0,"max_current":3.0,"note":"test board",
 "updated":"2026-11-07 03:31:28"},"file":".../state/limits.json"}
```

(`file` is the full path; shortened here.)

`limits --clear` removes them. With no limits set, only the supply's own ranges apply. Set the limits to match what
is connected before you let an agent drive the supply.

## Safety

The supply drives real power. Read this before connecting a load.

- **Headroom rule.** The tool refuses an OVP lower than `VSET + max(0.2 V, 1.5 %)`, whether given in the same call
  or as sent last, and `on` refuses to switch on when the VSET and OVP sent last break that rule. An OCP lower than
  `ISET + max(0.05 A, 2 %)` is sent with a warning (it trips as soon as a load pulls the output into CC); keep it
  above that. The reason for the OVP rule is measured: an OVP of 3.00 V with VSET 3.00 V tripped the output as the
  voltage stepped up from 2 V, and an OVP of 2.00 V with VSET 3.00 V tripped as the output rose. A setpoint step
  overshoots a little (1.0068 V for 1.00 V once).
- **Never trip a protection.** A tripped protection switches the output off and sounds the alarm. The alarm and the
  trip flag are cleared **only by a front-panel key press**; the supply cannot report an alarm, and a trip looks on the
  serial link like a plain "output off" (the on/off flag goes to 0). After a trip, a remote `on` still switches the
  output on again, but the alarm keeps sounding until someone presses a key. Check `status` after every change.
- **The supply cannot report its settings.** The link carries the measured output and one on/off flag, not the
  setpoints, the screen, the cursor or the alarm. The tool remembers what it sent (`last_sent` in `status`), and the
  front panel may have changed things since. The `set` command refuses an OVP or OCP that is not above what the
  terminals show now (plus a small margin), unless the same call lowers the setpoint (see [CLI reference](#cli-reference)).
- **Know what is connected.** The voltage that appears on `on` is whatever is in force on the supply, which on the
  Chinese screen is the selected parameter list. Check `vout_v` after switching on.
- **Cut-offs are silent.** On the Chinese screen the cut-off current and cut-off time switch the output off without
  the alarm. A silent "output off" can therefore be a cut-off, not a trip and not a program; the cut-off time counts the
  on-time accumulated since power-up (see [CLI reference](#cli-reference)).
- **Keep the settings menu closed while a command runs.** Entering the on-device settings menu switches the output off
  once. While it is open the measurements on the link are frozen, OPP and OTP are not checked, and a remote `on` can
  switch the output on with regulation frozen. Do not use the tool while the menu is open. (Found in the firmware
  disassembly; not tried on the hardware.)
- **EEPROM wear and the DATA box.** The supply saves the front-panel field under its cursor to its EEPROM on **every
  valid frame it receives, status queries included** (found in the firmware disassembly; the writes themselves were
  not yet observed on the hardware; see [Does a status query really write the EEPROM?](#does-a-status-query-really-write-the-eeprom)). With the cursor on V-SET (the power-up default) that is a write to the VSET
  bytes about 10 times per second when polled at the vendor app's rate of 100 ms: about one million writes in 28
  hours, near the typical rating of a 24Cxx part (about 1e6 cycles; the actual part and its rating are not known).
  The EEPROM has fixed addresses and no wear levelling. **Tip:** move the front-panel cursor to the **DATA** box (the
  last box of the setting bar, after REV) on the Digital or Waveform screen; with it selected, frames save nothing
  (firmware). Turning the knob while DATA is selected clears the Ah, Wh and time statistics (vendor manual), so leave
  the knob alone. `watch` and `log` default to one query per second; do not go below what you need.
- **Settings persistence after power-off** (from the firmware disassembly). Values sent over serial are only partly
  kept. VSET, ISET, OVP, OCP, DELAY and REV are written to the EEPROM only if their field is the one under the cursor
  when a frame arrives (VSET by default); otherwise they are gone at power-off. OPP, OTP and CC-first are kept only if
  the front-panel settings menu is exited afterwards. The output state is never kept; the output is off after
  power-up. Display colours are always kept.
- **Closing the port is safe, the output stays as it is.** An interrupted command does not switch the output off by
  itself, except a `sequence`, which switches it off at the end, on `stop` or on Ctrl+C unless started with
  `--end keep` (and always after a bench-limit or `NOT_APPLIED` error). Switch the output off yourself when you leave.
- **Hardware coverage is limited** (see [Status](#status-and-tested-hardware)). Do not assume that a trip, an
  over-power or an over-temperature case behaves as written here until it has been tested on your supply.

## MCP server

`openaxpp300s-mcp` is an MCP server (stdio, standard library only). Every tool runs the CLI in a child process, so
the same checks, bench limits and error codes apply. It has no `raw` tool, and the bench limits can only be changed
with the CLI. Add it to Claude Code:

```
claude mcp add -s user openaxpp300s -- openaxpp300s-mcp
```

If the Python `Scripts` folder is not on `PATH`, give the full path of the executable, for example
`<Python>\Scripts\openaxpp300s-mcp.exe`. In **PowerShell** quote the double dash, because PowerShell swallows a bare
`--`:

```
claude mcp add -s user openaxpp300s '--' openaxpp300s-mcp
```

For another MCP client, start `openaxpp300s-mcp` (or `python -m openaxpp300s.mcp_server`) as a stdio server. Long
jobs run as background jobs (detached children) whose output stays in `state/mcp_jobs/<job>/`, so a restarted server
still sees them.

The 12 tools:

| Tool | What it does |
|---|---|
| `status` | measured output, input voltage, temperature, mode, `output_on`, `last_sent` and the bench limits (read-only) |
| `set` | change settings in the safe order, with all the refusals of the CLI |
| `output` | switch the output on or off and wait until it follows |
| `param_list` | write or select one of the Chinese-screen parameter lists 0..4 |
| `display_colors` | set or reset the Digital screen colours |
| `watch` | sample the output for a few seconds and return statistics and the last sample |
| `log_start` | start a background log to CSV or JSONL; returns a job id |
| `sequence_start` | run timed voltage/current steps in the background |
| `job_status` | state of a log or sequence job (read-only) |
| `job_stop` | stop a log or sequence job cleanly |
| `selftest` | read-only checks of the link and the supply |
| `ports` | list serial ports and probe the USB ones for a PP300S |

The MCP server was tested against the simulator and, on 2026-11-07, against the real supply: `ports`, `status`, `selftest` (9 of 9 checks), `set` (OVP and OCP first, then the setpoints; lowering VSET and OVP together sends VSET first; a VSET over the OVP sent last is refused), `output` on and off, `watch`, `log_start` with `job_status` (it returned the last CSV row) and `job_stop`, and `sequence_start` (two steps; it ended by itself and switched the output off).

## Does a status query really write the EEPROM?

Yes, by the firmware code, but it has **not been observed on the hardware**. The evidence is in the firmware V1.4.4
disassembly (checked by hand): once a received frame has passed its checksum and tail checks, the code at
0x0800090C to 0x08000910 stores 1 to the RAM flag at 0x20000216, **before** the register ID is read at 0x08000912 and
dispatched. So the status query (ID 0xFF) sets the flag like any other frame; its own case (0x08000EC0) only sets the
reply-pending flag at 0x20000207. The main-loop save path (0x08001DEE, which branches to the cursor-field save at
0x08001F86) then writes the front-panel field under the cursor to the EEPROM whenever that flag is set (Digital and
Waveform screens; the Chinese screen has its own save path). Seeing the writes would take a probe on the EEPROM's I2C
lines. The cure does not depend on that: select the **DATA** box with the front-panel cursor (see
[Safety](#safety)), and frames save nothing.

## Simulator

`--sim` (or `OPENAXPP300S_SIM=1`) replaces the serial port with a virtual AX PP300S that follows the firmware
V1.4.4 image and the measurements: the byte parser (a corrupt frame, or one not complete 10 ms after its 0xA5, is
dropped; a stray 0xA5 swallows the next frame), the ACK on every valid frame except the status query, the 50 ms
status tick, soft start, DELAY and REV, the OVP, OCP, OPP and OTP trips with a latched alarm, the Digital and Chinese
screens with the cut-offs, CV/CC with a resistive load (10 MOhm by default), and the EEPROM write count. The trips
other than OVP follow the firmware only; they were not seen on the hardware. Its state is a JSON file in
`state/sim/`, shared by all processes. Control it with:

```
python -m openaxpp300s.sim reset | show | set key=value ... | advance SECONDS | press | audit [--clear]
```

`press` simulates a front-panel key press (it clears the alarm), `advance` moves the simulated clock forward.
`OPENAXPP300S_SIM_DELAY` scales the simulated answer latency; `0` removes it. `OPENAXPP300S_NO_SERIAL=1` makes any
real port refuse to open.

## Tests

```
pip install -e ".[test]"
python -m pytest
```

The tests use the simulator only. `tests/conftest.py` isolates every test: `OPENAXPP300S_SIM=1`,
`OPENAXPP300S_NO_SERIAL=1`, `OPENAXPP300S_SIM_DELAY=0` and a fresh `OPENAXPP300S_HOME`, so a test can never reach a
real supply. The marker `hw` (`pytest -m hw`) is reserved for tests that need a real supply; there are none yet, and
the default run excludes it. The hardware checks are separate scripts, not part of the pytest run. They need a real
supply and switch the output on; the three `bench_289` scripts also read a Fluke 289 through
[open289](https://github.com/FrostFlops/open289), and the two CC scripts need an electronic load:

- `tests/hw/bench_289.py`: voltage accuracy with no load, compared with the 289;
- `tests/hw/bench_289_cc.py`: CV into a constant-resistance load, with the 289 in series as an ammeter (also shows why
  CR mode is unsuitable for testing CC);
- `tests/hw/bench_289_cc_cvload.py`: the CC limit with the load in constant-voltage mode: CC accuracy, lowering ISET
  and OCP together under current, and the CV-first against CC-first switch-on;
- `tests/hw/bench_chinese.py`: the Chinese screen (lists, `on` into the list voltage, cut-offs, ignored Digital
  registers); no meter, but a load that draws almost nothing below 3 V, and the supply switched to the Chinese screen
  by hand.

## Files

| Where | What |
|---|---|
| `state/` | locks (one command at a time owns the port), the remembered port, `last_sent.json` (values sent last), `limits.json` (bench limits), stop requests, running jobs, `mcp_jobs/`, `sim/` |
| `data/` | logs written by `log` (and the MCP tool `log_start`) when no path is given |

These folders live in `$OPENAXPP300S_HOME` if it is set; else, in a source checkout (also with `pip install -e .`),
inside the checkout; else, for an installed package, in `%LOCALAPPDATA%\openaxpp300s` on Windows and
`~/.local/share/openaxpp300s` elsewhere, so nothing is written into `site-packages`. Set `OPENAXPP300S_HOME` to move
everything to another folder.

Environment variables: `OPENAXPP300S_PORT` (default port), `OPENAXPP300S_HOME`, `OPENAXPP300S_SIM`,
`OPENAXPP300S_NO_SERIAL`, `OPENAXPP300S_SIM_DELAY`.

## Accuracy

Reference: a Fluke 289 (10 MOhm input for voltage, the A jack in series for current). The tables below are from the
second session on 2026-11-07, with the supply fed from a 65 W USB-PD charger (about 19.8 V) on its Type-C power input.

### Voltage, no load

Output on, no load, V DC, 3 s steady per point (second session; the first session agreed, 0.5 V to 15 V, +1.7 to
+7.0 mV):

| VSET (V) | meter (V) | meter minus VSET (mV) | supply read-back minus meter (mV) |
|---|---|---|---|
| 0.5 | 0.5026 | +2.6 | -8.6 (mean includes settling) |
| 1.0 | 1.0054 | +5.4 | -2.4 |
| 2.5 | 2.50415 | +4.2 | -2.8 |
| 3.3 | 3.3039 | +3.9 | -1.8 |
| 5.0 | 5.0028 | +2.8 | -1.1 |
| 9.0 | 9.005 | +5.0 | -2.2 |
| 12.0 | 12.007 | +7.0 | -3.6 |
| 15.0 | 15.006 | +6.0 | -2.4 |
| 20.0 | 20.003 | +3.0 | -0.4 |
| 24.0 | 24.006 | +6.0 | -3.5 |
| 30.0 | 30.005 | +5.0 | -2.9 |

The setting is inside the specified 0.1 % + 5 mV at every point (+7.0 mV at 12 V against 17 mV allowed). So is the
read-back, except the 0.5 V mean (-8.6 mV against a 5.5 mV limit), which includes the settling. Boost to 20, 24 and
30 V from the 20 V input works with no load.

### Current limit (CC), load in constant-voltage mode

The load was held at 3.0 V with VSET 5 V, so the supply sat in CC at every step:

| ISET (A) | meter (A) | meter minus ISET (mA) | supply read-back minus meter (mA) | meter spread (mA) |
|---|---|---|---|---|
| 0.10 | 0.0864 | -13.6 | -6.2 | 2.7 |
| 0.25 | 0.2366 | -13.4 | -7.3 | 0.8 |
| 0.30 | 0.2867 | -13.3 | -2.7 | 0.8 |
| 0.50 | 0.4864 | -13.6 | -2.6 | 0.4 |
| 1.00 | 0.9862 | -13.8 | -2.2 | 0.5 |

CC is stable and linear, but every setting delivers about **13.5 mA less than ISET**: a constant offset, outside the
specified 0.1 % + 5 mA (5.5 mA at 0.5 A). This is the **supply**, not the tool: the tool sends the right raw value,
and the most likely cause is a current-calibration offset of this unit (the manual says current calibration needs a
10 A load and is not recommended for users). Another unit may differ. Allow for it if you rely on ISET; the read-back is
closer to the meter (2 to 7 mA below it), and noisier at low current (0.065 to 0.084 A at ISET 0.1).

### Current in CV, under load

5 V into a 10 Ohm load (ISET 1 A, so CV, 0.5 A): the supply read-back was 0.4943 A against 0.4940 A on the meter
(+0.3 mA), and 0.4919 against 0.4938 A in a second run. For the charge logged with this load in CR mode while the
supply limited the current, see [Testing the current limit with an electronic load](#testing-the-current-limit-with-an-electronic-load).
These tests ran up to about 1.2 A and 15 W only.

### Timing

- The supply acknowledges a frame in about 12 to 14 ms (USB latency). A status reply takes 15 to 55 ms, median about
  48 ms (it is sent on the supply's next 50 ms tick).
- After `on` with DELAY = REV = 0 the output flag is set at once; the voltage stays near 0.011 V for about 1.1 s (soft
  start) and then settles in about 0.3 s. After a VSET step with the output on, it settles in about 0.2 s (0.94 s for
  a step from 1.0 V down to 0.5 V).
- `off`: into a 10 MOhm meter the voltage falls from 2 V to 0.13 V in about 0.3 s.
- DELAY 3 s: the output was on 4.39 s after the `on` command (3 s plus soft start; 4.23 s in the second session). REV 2 s: on after about 2 s, then
  toggling every 2 s.
- A frame with a bad checksum gets no answer, nor does a frame split over more than 10 ms; garbage containing 0xA5
  before a frame swallows that frame; two queries in one write get one reply. The tool sends each frame in one
  write, one at a time, and resends a frame that got no answer.

## What is NOT here

- No reading of the supply's settings, screen, cursor or alarm: the firmware does not report them.
- No firmware update and no flashing; FlyMcu is not used by this tool.
- No front-panel key presses (the firmware has no serial command for them).
- No Bluetooth or any other link; USB serial only.

## License

GPL-3.0-only. See `LICENSE`.

Not affiliated with AxinDIY. The vendor's protocol document and manual were read for reference; none of their text or
images is part of this project.
