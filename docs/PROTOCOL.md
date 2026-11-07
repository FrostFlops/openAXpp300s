English | [中文 (简体中文)](PROTOCOL.zh-CN.md)

# openaxpp300s protocol reference

This document describes how openaxpp300s talks to an AX PP300S bench power supply over its USB serial port. It is written for people who want to implement or audit a client, and for anyone who wants to know what the supply does with a frame.

## Conventions and evidence levels

Every protocol statement is tagged with how it is known.

| Tag | Meaning |
|---|---|
| FIRMWARE | Seen in the disassembly of the supply firmware V1.4.4. The address of the code is given (`0x0800xxxx`, flash) or of the variable (`0x2000xxxx`, RAM). Where the disassembly was also run in an emulator the text says so. |
| MEASURED | Observed on a real supply on 2026-11-07, in three sessions: AX PP300S firmware V1.4.4, Windows 11, CH340 USB-serial bridge, input 19.8 V (in the second and third sessions from a 65 W USB-PD charger on the Type-C power input). Reference instrument: a Fluke 289, as a voltmeter (10 MOhm input) and, in series with an electronic load, as an ammeter (A jack). |
| VENDOR | Taken from the vendor serial-protocol PDF, the vendor user manual V1.1, the vendor PC application V1.2, or the change log that comes with firmware V1.4.4. |
| INFERRED | Concluded from code, names or analogy but not proven. Treat it as a hypothesis. |

All multi-byte integers on the wire are little-endian unless stated otherwise. Hex dumps are space-separated bytes. Units are SI: volts, amperes, watts, seconds, degrees Celsius. Setting units (10 mV, 10 mA) are stated where they matter. Port names are written `COMx`.

### Hardware status

Verified on the real supply (firmware V1.4.4, Windows 11, CH340), 2026-11-07, in three sessions.

First session:

- port discovery, status, selftest;
- setting VSET, ISET, OVP, OCP, DELAY, REV, OPP and OTP (every frame acknowledged);
- output on and off;
- VSET accuracy from 0.5 V to 15 V against a Fluke 289 (section 12.2);
- lowering VSET and OVP together with the output on (no trip);
- DELAY, REV, watch, sequence, raw query and the tool's refusals;
- two OVP trips (section 7).

Second session (the tool's latest code, after the review fixes; input from a 65 W USB-PD charger):

- VSET accuracy from 0.5 V to 30 V, including boost to 20, 24 and 30 V (section 12.2);
- the MCP server against the real supply (ports, status, selftest, set, output on and off, watch, log, sequence and the tool's refusals);
- CV under load, and the current limit (CC) from 0.1 A to 1 A, before the current calibration, with an electronic load against the Fluke 289 as ammeter (sections 12.3 and 12.4);
- the CC flag (byte 11) in CV and in CC under load (section 5.3), and its alternation between CC and CV with a load in constant-resistance mode (section 12.6);
- lowering ISET and OCP together while 1 A flows (ISET first, no trip);
- CV-first against CC-first priority (register 0x10): its effect on the switch-on current (section 12.5);
- the display colours (section 6.4);
- the Chinese screen: parameter lists (effect, live change, selection; section 6.3), a Digital VSET and DELAY ignored there (section 6.1), cut-off current and cut-off time (section 8), and the fact that cut-offs are silent;
- the output switched on with the front-panel output key, with no host frame, as seen by the Fluke 289 (section 5.4).

Third session (after the user calibrated the supply's current on the front panel against the Fluke 289; same input):

- the current limit (CC) from 0.05 A to 5 A against the Fluke 289, inside the vendor specification at every point (section 12.4); CC verified up to 5 A and about 17.5 W, no protection tripped.

Not yet verified on hardware:

- OCP, OPP and OTP trips (only two OVP trips were seen, in the first session; no protection tripped in the second or third);
- the effects of the on-device settings menu (section 10.2);
- currents above 5 A and powers above about 20 W;
- the DC5525 input connector;
- CC accuracy at output voltages other than about 3 V (all CC points were taken there);
- the EEPROM writes themselves: there is code evidence that every frame, status queries included, sets the save flag (section 9), but no write was observed on the hardware.

Everything in this document about those areas comes from FIRMWARE or VENDOR evidence only.

## 1. Overview

```
PC (openaxpp300s)  <--- USB, CH340 serial, 115200 8N1 --->  AX PP300S  (second Type-C port, the serial one)
```

| Part | Role |
|---|---|
| AX PP300S | Buck-boost bench supply, 0 to 30 V, 0 to 10 A, 300 W (VENDOR). Its microcontroller runs the regulation, the protections, the display and the serial link. |
| CH340 bridge | USB to UART converter. MEASURED: it enumerates as USB-SERIAL CH340, VID:PID 1A86:7523. |
| openaxpp300s | The PC client (CLI, library, MCP server). |

Link parameters:

| Item | Value | Evidence |
|---|---|---|
| Connector | The second Type-C port, which the manual calls the serial Type-C port | VENDOR |
| Baud rate | 115200 | FIRMWARE: init `0x0800E29C` called with 0x1C200 at `0x0800E160`; VENDOR |
| Format | 8 data bits, no parity, 1 stop bit, no flow control | FIRMWARE: init struct `0x0800E2FC` to `0x0800E310` |
| DTR, RTS | Not used. Opening the port with either level does not reset the supply. | MEASURED |
| Device UART | USART1 (PA9 TX, PA10 RX). It is the only live UART. | FIRMWARE |
| Power | This is a data port, not a power input. The manual says that plugging it in alone does not start the supply and does not damage it. | VENDOR |

The supply is a pure responder. It sends no banner, no periodic telemetry and no notification of a protection trip, a cut-off or a front-panel change (FIRMWARE: the only two callers of the transmit primitive `0x0800E33C` are the ACK at `0x08000ED4` and the status reply loop at `0x08000FAA`). The host must poll.

The supply cannot report its own settings, the screen, the cursor or an alarm. The only things a host can read are the measured output, the input voltage, the temperature and two flags (section 5). A host that wants to know a setpoint must remember what it sent.

## 2. Frame format

Host to supply, always 6 bytes:

```
A5  ID  dataLo  dataHi  chk  5A
```

| Offset | Field | Meaning |
|---|---|---|
| 0 | `A5` | Frame head. |
| 1 | ID | Register id (section 6). |
| 2 | dataLo | Data word, low byte. |
| 3 | dataHi | Data word, high byte. |
| 4 | chk | `(ID + dataLo + dataHi) mod 256`. |
| 5 | `5A` | Frame tail. |

VENDOR and FIRMWARE: the checksum is computed at `0x080008E6` to `0x0800090A`. The data word is `dataLo + 256 * dataHi`; each register interprets it as shown in section 6.

Examples, VENDOR: `A5 00 E8 03 EB 5A` sets VSET to 10.00 V (0x03E8 = 1000 times 10 mV). `A5 08 01 00 09 5A` switches the output on. `A5 0F 05 01 15 5A` sets the minutes field of the list-0 cut-off time to 5. The query frame is `A5 FF 00 00 FF 5A`.

Supply to host, two kinds of answer:

| Answer | Bytes | When |
|---|---|---|
| ACK | `01` | After every valid frame except ID 0xFF (section 4). |
| Status reply | 15 bytes, `A5 ... 5A` | After ID 0xFF (section 5). |

## 3. The byte parser

FIRMWARE (`0x08000878` to `0x0800090A`, run from the USART1 receive interrupt `0x0800CEE4`; also run in an emulator against all 256 IDs with good and bad frames):

- The parser is a byte state machine. It has two states: hunting and collecting.
- Hunting: a byte equal to `A5` starts a frame. Any other byte is dropped.
- Collecting: the next 5 bytes are stored with no inspection, so a data byte equal to `A5` is fine. After the fifth byte the parser always returns to hunting.
- After the fifth byte the frame is accepted only if `ID + dataLo + dataHi` equals the checksum byte and the last byte is `5A`. Otherwise all 5 bytes are discarded silently.
- The parser never looks for the tail and never rescans consumed bytes.

Consequences:

| Situation | Result | Evidence |
|---|---|---|
| Garbage without `A5` before a frame | Harmless. | FIRMWARE |
| A stray `A5` before a real frame | The stray `A5` plus the next 5 bytes form a bad frame. The real frame is swallowed: its `A5` and the next 4 bytes are consumed, its `5A` is dropped while hunting, no ACK. | FIRMWARE; MEASURED (`00 11 5A A5` before a valid query swallowed it) |
| One byte lost to a UART error | Same as a stray `A5`: the next frame is swallowed. | FIRMWARE |
| Bad checksum or bad tail | Nothing stored, nothing answered. | FIRMWARE; MEASURED (bad checksum, no answer) |
| Frame split over several writes | Fine, as long as the whole frame arrives within the timeout below. | FIRMWARE |

Inter-byte timeout, FIRMWARE (`0x0800AD74`, `0x0800AD9A` to `0x0800ADC0`): timer TIM2 runs at 10 kHz. While collecting, it increments a counter; when the counter exceeds 100 it forces the parser back to hunting. The counter is cleared only when the `A5` is accepted, not on each byte. So the whole frame must arrive within about 10 ms of its `A5`. At 115200 baud a 6-byte frame takes 0.52 ms. A partial frame is abandoned about 10.1 ms after its `A5`, with no ACK. The 72 MHz timer clock is INFERRED from the clock set-up. MEASURED: a frame split 3 + 3 bytes with 20 ms between the halves got no answer.

UART errors, FIRMWARE (`0x0800CEE6` to `0x0800CF2A`): a byte with a parity, overrun or framing error is read and discarded before it reaches the parser. The noise flag is not checked.

There is no minimum gap between frames. Back-to-back valid frames are each processed and acknowledged (FIRMWARE).

Resynchronisation rule for hosts (INFERRED from the timeout above): after a missing answer, stay idle for at least 11 ms so that the parser is certainly hunting (a half-received frame has timed out), then resend.

## 4. Acknowledgements

FIRMWARE: the ACK is a single byte `01`, sent at `0x08000ED4` from the common tail of the dispatcher (`0x08000EC8` to `0x08000ED4`), inside the receive interrupt. The tail reads: if the ID is not `0xFF`, send `01`.

| Rule | Evidence |
|---|---|
| Every frame with a valid checksum and tail is acknowledged, except ID 0xFF. | FIRMWARE; MEASURED; VENDOR (the PDF says every setting is answered with `01`) |
| This includes all unused IDs (204 of them), values outside any range, and cut-off-time frames whose high byte is not 0, 1 or 2 (nothing stored, still acknowledged). | FIRMWARE (default case `0x080009D4`, fall-through at `0x08000C2E` and the same place in the other lists) |
| There is no NAK and no error code. A frame that was not acknowledged was not valid, or was swallowed. | FIRMWARE |
| ID 0xFF gets only the 15-byte reply, no ACK. | FIRMWARE (`0x08000ECE`) |

Order of events for a valid frame, FIRMWARE: checksum and tail are checked; the flag `0x20000216` is set; the case code stores the value into its live RAM variable; the ACK is sent. So the ACK means "frame accepted and stored in RAM". It does not mean that the value is applied, persisted, or sensible. Later effects:

| Effect | Delay after the frame |
|---|---|
| Regulation and OVP/OCP checks use the new value | about 1 ms (TIM2 1 kHz slot) |
| Output switching, DELAY, REV | up to 50 ms (TIM3 tick), plus DELAY |
| EEPROM save of the field under the cursor (section 9) | about 20 ms, in the main loop |
| Colour save and redraw | 210 ms or more |

MEASURED: the ACK arrives about 12 to 14 ms after the frame is written (USB latency of the CH340; the firmware sends it within about 0.2 ms of the tail). If a reply byte is on the wire when the ACK is due, the ACK waits for the transmitter to finish (FIRMWARE: the transmit primitive waits for the TC flag first).

## 5. The status reply

### 5.1 Request

Frame `A5 FF lo hi chk 5A`. The data bytes are ignored but must give a valid checksum; the tool sends data 0. FIRMWARE: the receive interrupt only sets a flag (`0x20000207`, at `0x08000EC0`); the reply is sent later from the 50 ms timer interrupt (TIM3, `0x0800C1A8`; period 499, prescaler 7199 set via `0x0800CA4C`).

### 5.2 Layout

15 bytes. Offsets are 0-based. FIRMWARE (builder `0x08000EE8`, packer `0x08000F7C`) and VENDOR (PDF, same layout).

| Offset | Size | Field | Unit | Notes | Evidence |
|---|---|---|---|---|---|
| 0 | 1 | head | | `A5` | FIRMWARE |
| 1 | 2 | Vout | 1 mV | Output voltage. Unsigned in practice. Reads 0 when the internal value is below 9 (mV scale, `0x0800C646` to `0x0800C65C`). | FIRMWARE |
| 3 | 2 | Iout | 1 mA | Output current. | FIRMWARE |
| 5 | 2 | Pout | 10 mW | `Vout_mV * Iout_mA / 1000` in mW, divided by 10 (`0x0800C666`, `0x0800C954`). | FIRMWARE |
| 7 | 2 | Vin | 10 mV | Input voltage of the connector in use. Computed once per second from an ADC channel with a calibration factor (`0x0800B0B2` to `0x0800B15C`). The code does not fix the unit; the vendor PDF gives 0.01 V, which agrees with a reading on the real supply (0x07BD = 19.81 V). | FIRMWARE, VENDOR, MEASURED |
| 9 | 2 | temperature | 0.1 C | Signed 16-bit in the host (the firmware sign-extends all six values before packing). Written by the display loops (`0x08008C4E`). The unit is the one in the vendor PDF and is consistent with the fan thresholds 50.0 C and 45.0 C and the OTP compare (INFERRED from the code). | FIRMWARE, VENDOR |
| 11 | 1 | CC flag | | 0 or 1 only (section 5.3). | FIRMWARE |
| 12 | 1 | output flag | | 0 or 1 only (section 5.4). | FIRMWARE |
| 13 | 1 | checksum | | Sum of bytes 1 to 12 mod 256. | FIRMWARE (`0x08000F48`); VENDOR |
| 14 | 1 | tail | | `5A` | FIRMWARE |

Example, VENDOR: `A5 88 13 00 00 00 00 B4 07 25 01 00 01 7D 5A` is Vout 5.000 V (0x1388), Iout 0 A, Pout 0 W, Vin 19.72 V, 29.3 C, CV, output on; the checksum is 0x7D.

The reply carries no field for OVP, OCP, OPP, OTP or SCP flags, no setpoints and no screen (FIRMWARE: none of the trip flags `0x20000120` to `0x20000124`, nor the cut-off flags `0x200001A0` and `0x200001A1`, is read by the reply code).

### 5.3 Byte 11, the CC flag

FIRMWARE: the only writers are `0x0800AF72` and `0x0800AF7A`, in the 1 kHz slot of TIM2. The code averages 2 samples of one ADC channel (`0x08008B0C`) and stores 1 when the average is below 3600 counts, else 0. The firmware itself uses it as the hardware CC indicator in regulation (`0x0800B38C`). It is computed whatever the output state is.

Meaning for hosts:

| Condition | Byte 11 | Evidence |
|---|---|---|
| Output on, in constant voltage | 0 | MEASURED (after soft start, also in CV under a 0.5 A load) |
| Output on, in constant current | 1 | FIRMWARE; MEASURED under load (electronic load in CV mode at 3.0 V, ISET 0.1 to 1.0 A in the second session and 0.05 to 5 A in the third: the tool reported CC at every step) |
| First about 1.1 s after the output switches on (soft start: the output stays at about 0.011 V, then rises) | 1 | MEASURED |
| Output off | Not meaningful. It reads 1 (seen once as 0). The sense node reads low with no output; the cause is INFERRED. | MEASURED |

The display shows `STATE: CV` or `STATE: CC` only when byte 12 is 1, and `STATE: CLOSE` otherwise (FIRMWARE `0x080019E0` to `0x08001A46`). Host rule: ignore byte 11 when byte 12 is 0, and do not treat byte 11 = 1 during the first 1.5 s after ON as a real current limit. The tool reports `mode` OFF when byte 12 is 0 and CC/CV from byte 11 otherwise; it also exposes the raw `cc_flag`.

### 5.4 Byte 12, the effective output state

FIRMWARE: byte 12 is `0x20000102`. It is not the commanded state `0x20000101` (the one register 0x08 writes). It is 0 while any of these holds:

- DELAY has not elapsed yet;
- the OFF half-period of REV;
- after a Chinese-screen cut-off by current or by time (MEASURED, section 8);
- after any protection trip;
- after a front-panel OFF;
- after entering the settings menu (`0x08003AFE` to `0x08003B06`).

MEASURED: with DELAY and REV at 0, byte 12 becomes 1 at once after ON and 0 at once after OFF. With DELAY 3 s the output was on 4.39 s after the ON command (3 s plus soft start). With REV 2 s it came on after about 2 s and then toggled every 2 s; in 9 s the output was on for 3.6 s, with the CC flag high during each soft start.

Byte 12 = 1 does not guarantee a live output. FIRMWARE (`0x0800B26A` to `0x0800B2F2`): the 1 kHz slot gates the output-enable pin (PC6) with two more conditions; the meaning is INFERRED:

- on the Chinese screen, with the cursor on the list-select box (cursor 6) and the knob in value-edit mode, PC6 is held off;
- PC6 is driven on only if a second ADC channel (2-sample average times 1.692) is below 1600.0.

In both cases the reply says ON while the output is physically off. Whether the second gate can act in normal use is open (section 15).

A trip is visible only as byte 12 falling to 0 without a host command. It cannot be told from a front-panel OFF, a Chinese-screen cut-off, a REV off phase, or entering the settings menu. FIRMWARE: every trip clears both `0x20000101` and `0x20000102` and drives PC6 low.

MEASURED: the output came on three times while the tool sent no ON frame (the Fluke 289 log showed it); the user had pressed the output key on the front panel. That the reply then reads byte 12 = 1, exactly as after a remote ON, follows from the code, not from a reading: PC6 is driven on only while byte 12 is 1 (FIRMWARE, above). So a host cannot tell who switched the output on, and the settings in force may differ from what the tool sent last. The tool's `status` prints a note when the output is on but the tool did not switch it on.

### 5.5 Refresh rates and staleness

| Value | Refreshed | Evidence |
|---|---|---|
| Vout, Iout, Pout | On every second 50 ms tick, i.e. every 100 ms (parity test `0x0800C1C4`). | FIRMWARE |
| Vin | Once per second. | FIRMWARE (`0x0800B0B2`) |
| Temperature | By the display loops; the rate is INFERRED. | FIRMWARE, INFERRED |
| Byte 11 | Every 1 ms. | FIRMWARE |
| Byte 12 | Every 50 ms tick (TIM3 output logic). | FIRMWARE |

While the on-device settings menu is open, Vout, Iout and Pout are not refreshed (`0x0800C1CE` skips the update), and the temperature is not either. The reply then carries frozen values. FIRMWARE.

Values are sampled at the TIM3 tick, not at the time of the request. Several queries inside one 50 ms tick produce one reply (FIRMWARE; MEASURED: two queries in one write gave one reply). A query whose flag is set while a reply is being transmitted is cleared after sending and never answered (FIRMWARE, from the code order).

Latency, MEASURED: the status reply arrives 15 to 55 ms after the query (the next 50 ms tick plus USB latency), median about 48 ms. FIRMWARE estimate: 0 to 50 ms plus about 1.3 ms to transmit 15 bytes; a key held on the front panel can slow the tick (INFERRED).

### 5.6 The interleaving hazard

FIRMWARE: the 15-byte reply is sent byte by byte, busy-waiting, from inside the TIM3 interrupt (preemption level 2, loop at `0x08000FA6` to `0x08000FB6`). The USART1 receive interrupt has level 0 and preempts it. A setting frame that completes during the roughly 1.3 ms of the reply runs its interrupt, waits for the transmitter and writes its `01` between two bytes of the reply. The host would then see a corrupted reply.

Rule: never have a setting frame outstanding while waiting for a query reply, and never send a frame before the reply is complete. Validate every reply (head, tail, checksum) and resynchronise by discarding input up to the next `A5`.

## 6. Register table

FIRMWARE (dispatcher `0x0800090C`, tables `0x08000922` and `0x080009A6`; all 256 IDs run in an emulator) and VENDOR (PDF, vendor PC app V1.2). Data is a 16-bit word. The code stores it without any range check: every ID accepts 0 to 65535. The ranges below are the front-panel limits (the knob routines `0x08008092` to `0x0800810E`, `0x08008302` to `0x0800836C` and `0x0800848C` to `0x08008920`) and the vendor PDF ranges. They are NOT enforced by the firmware on the serial path. A host tool must enforce them itself.

Screen column: "Digital" = the Digital and Waveform screens, "Chinese" = the Chinese screen (its menu entry is "Chinese" in V1.4.4, FIRMWARE string; the manual V1.1 still calls it Digital2, and the change log says it was renamed in firmware 1.4.2, VENDOR), "any" = all screens. The vendor PDF groups the registers the same way.

### 6.1 Digital and Waveform screens, and all-screen registers

| ID | Name | Unit | Range | Screen | Notes | Source |
|---|---|---|---|---|---|---|
| 0x00 | VSET | 10 mV | 0 to 3000 | Digital | Output voltage setpoint. | FIRMWARE `0x080009C6`; VENDOR |
| 0x01 | ISET | 10 mA | 1 to 1010 | Digital | Current limit. The PDF says 0 to 1010; the knob minimum is 1. | FIRMWARE `0x080009D6`; VENDOR |
| 0x02 | OVP | 10 mV | 50 to 3050 | Digital | Trips when the measured voltage reaches it (section 7). The PDF says 0 to 3050; the knob minimum is 50. | FIRMWARE `0x080009E6`; VENDOR |
| 0x03 | OCP | 10 mA | 10 to 1050 | Digital | The PDF says 0 to 1050; the knob minimum is 10. | FIRMWARE `0x080009F6`; VENDOR |
| 0x04 | DELAY | 1 s | 0 to 9999 | Digital | Output-on delay (section 5.4). Real delay is between DELAY-1 and DELAY seconds plus up to 50 ms, because the 1 s tick is free-running (INFERRED from the code). | FIRMWARE `0x08000A08`; VENDOR |
| 0x05 | REV | 1 s | 0 to 9999 | Digital | Output toggle period. The first ON comes REV s after the delay, then the output alternates every REV s. The counter is not reset on OFF, so the first toggle after an OFF/ON cycle can come early (INFERRED). | FIRMWARE `0x08000A18`; VENDOR |
| 0x06 | OPP | 10 W | 0 to 30 | any | Limit = `data * 10 + 10` W, so 10 to 310 W. Only the low byte is stored. | FIRMWARE `0x08000A28`; VENDOR |
| 0x07 | OTP | 10 C | 6 to 10 | any | Limit = `data * 10` C, so 60 to 100 C. Only the low byte is stored. | FIRMWARE `0x08000A42`; VENDOR |
| 0x08 | ON/OFF | | 0 or 1 | any | Commanded output state. Only the low byte is used: nonzero = ON, so `0x0100` means OFF. Accepted on every screen and while the settings menu is open. Never persisted: power-on state is OFF. | FIRMWARE `0x08000A58`; VENDOR |
| 0x09 | list select | | 0 to 4 | Chinese | Selects the parameter list (section 6.3). Used unchecked as an index. | FIRMWARE `0x08000BA8`; VENDOR |
| 0x10 | CC first | | 0 or 1 | any | 0 = CV first, nonzero = CC first. First-boot default is 1. Bypassed while a front-panel correction mode is active. Not in the vendor PDF. MEASURED: with CV first the switch-on current overshoots ISET by about 60 %, with CC first by about 8 % (section 12.5). | FIRMWARE `0x08000A68`; VENDOR (vendor PC app V1.2) |

Which screen a register acts on, FIRMWARE: the Digital registers are always stored, but regulation uses them only when the interface is not the Chinese one (the Chinese screen uses the list values instead, branch `0x0800B384`). In factory mode (`0x2000006D`) a separate factory table drives the output. OPP, OTP and 0x08 act on all screens. DELAY and REV are ignored on the Chinese screen, where a commanded ON sets byte 12 at once (`0x0800C892`). MEASURED on the Chinese screen: a Digital VSET of 1.5 V was acknowledged but ignored (the output stayed at the list voltage of 2.4 V; the tool reported NOT_APPLIED after 3 s), and DELAY 3 s was ignored (the output came on at once).

Scaling, FIRMWARE and VENDOR: raw = value / unit. 5.00 V is raw 500, 1.50 A is raw 150, 12.34 V is raw 1234.

### 6.2 Dangerous values

FIRMWARE:

- VSET and ISET above their ranges go to the PWM unclamped (the double result is truncated with a 16-bit extend, `0x0800B638`); the hardware effect is INFERRED and is open (section 15).
- OVP, OCP, list OVP, list OCP or OTP of 0 is a permanent trip; OTP 1 to 5 trips at 10 to 50 C (section 7).
- OVP, OCP, OPP or OTP set very high disable the protection in effect (OVP above about 3050, OCP above 1200, OPP up to 2560 W, OTP up to 2550 C).
- 0x09 with data 5 or more is an out-of-bounds list index, persisted unchecked at EEPROM 0x3B and reloaded at boot (`0x0800E000`).
- 0x08 ON re-enables the output after a trip (section 7).
- 0xFF and any other frame cost EEPROM wear (section 9).

There is no calibration, erase, reset, flash or bootloader ID (FIRMWARE: the image has no `0x05FA0004` reset-request constant and no flash unlock key).

### 6.3 Chinese screen parameter lists

Five lists, n = 0 to 4. The high nibble of the ID is the list number: ID = `(n << 4) | low`.

| ID (list n) | Name | Unit | Range | Notes |
|---|---|---|---|---|
| `0x0A + 0x10n` | list VSET | 10 mV | 0 to 3000 | Voltage setpoint of list n. |
| `0x0B + 0x10n` | list ISET | 10 mA | 1 to 1010 | |
| `0x0C + 0x10n` | list OVP | 10 mV | 50 to 3050 | Checked in the same 1 kHz slot as the Digital OVP, using the selected list. |
| `0x0D + 0x10n` | list OCP | 10 mA | 10 to 1050 | |
| `0x0E + 0x10n` | cut-off current | 10 mA | 0 to 1000 | Section 8. 0 disables it. |
| `0x0F + 0x10n` | cut-off time | h, min, s | 0 to 99 h, 0 to 60 min, 0 to 60 s | One field per frame; dataLo = value, dataHi = field (0 seconds, 1 minutes, 2 hours). Section 8. |

Source: FIRMWARE (cases `0x0A` at `0x08000BB8`, `0x1A` at `0x08000C3C`, `0x2A` at `0x08000D24`, `0x3A` at `0x08000DA8`, `0x4A` at `0x08000E2C`, and the following IDs of each list; ID 0x2D has its own branch at `0x08000916`) and VENDOR (the PDF lists the same 6 IDs per list).

The list registers are always stored but they take effect only when the interface is the Chinese one and n equals the selected list (register 0x09). A frame for a non-selected list only changes RAM. Which list is selected cannot be read back.

MEASURED (Chinese screen, a 3 V constant-voltage load so that about 0 A flowed below 3 V):

- List 0 was written (VSET 2.0 V, OVP 2.5 V) and then selected, values first and the list select 0x09 last. After ON the output settled at 1.9975 V (readback mean): the list value drives the output, not the Digital VSET.
- Changing list 0 to VSET 2.4 V (OVP 2.9 V) with the output on was followed at once (2.401 V).
- List 1 was written (VSET 1.2 V, OVP 1.7 V). The tool refused to select it before sending anything, because its OVP of 1.7 V is below the present 2.4 V output and would trip. The firmware has no such check (0x09 is stored unchecked). After the list-1 OVP was raised to 3.0 V the selection went through and the output went to 1.2 V; no trip.

### 6.4 Display colours (IDs 0x50 to 0x59)

Source: VENDOR (vendor PC app V1.2; not in the protocol PDF). FIRMWARE confirms that the IDs exist and what they store. Data is a 16-bit RGB565 word. They act on the Digital screen in firmware V1.4.4 (VENDOR: change log). The names of IDs 0x53 and 0x54 come from the PC app (the on-screen meaning is INFERRED).

| ID | Element | EEPROM slot | Default (the PC app's "restore defaults") |
|---|---|---|---|
| 0x50 | Vout digits | 0x7E/0x7F | 0xFFE0 |
| 0x51 | Iout digits | 0x80/0x81 | 0x07E0 |
| 0x52 | Pout digits | 0x82/0x83 | 0x7FFF |
| 0x53 | auxiliary boxes, background (VIN, STATE, TEMP, Time, C, E) | 0x84/0x85 (INFERRED order) | 0x07E0 |
| 0x54 | auxiliary boxes, font | 0x86/0x87 (INFERRED order) | 0x0000 |
| 0x55 | setting bar, background | 0x88/0x89 | 0x07E0 |
| 0x56 | setting bar, labels | 0x8A/0x8B | 0x0000 |
| 0x57 | setting bar, values | 0x8C/0x8D | 0xFFFF |
| 0x58 | setting bar, ON/OFF indicator | 0x8E/0x8F | 0x07E0 |
| 0x59 | setting bar, selected box | 0x90/0x91 | 0xFFFF |

Effects of a colour frame, FIRMWARE: the value is stored, the running screen loop exits and re-enters (full redraw), and the main loop writes the colour magic `0x5D` to EEPROM 0x7D followed by all 10 colours: 21 byte writes, about 210 ms (`0x0800AA14`). A colour frame arriving during that save is stored in RAM and its flags are then cleared, so it may not be persisted (code order). Colours are always persisted, and are reloaded at boot when EEPROM 0x7D is `0x5D`.

MEASURED: `color set vout=red` turned the Vout digits red (seen by the user), and `color reset` restored the vendor defaults; the tool sends the last colour twice (section 13). That the colours survive a power cycle was not tested.

### 6.5 Unused IDs

FIRMWARE: 0x11 to 0x19, 0x20 to 0x29, 0x30 to 0x39, 0x40 to 0x49 and 0x5A to 0xFE (204 IDs) store nothing. They are acknowledged like any valid frame, and they still trigger the cursor-field EEPROM save and a redraw (section 9).

### 6.6 Persistence of a remotely set value

FIRMWARE. Nothing sent over serial is written to non-volatile storage on purpose. The only persistence is the side effect of section 9: when a frame arrives, the supply saves the field under the on-screen cursor.

| Register | Persisted when |
|---|---|
| VSET, ISET, OVP, OCP, DELAY, REV | The cursor is on that field (V-SET, I-SET, OVP, OCP, DELAY, REV = cursor 0 to 5) when a frame arrives. The boot cursor is V-SET, so VSET is the one that survives by default. |
| OPP, OTP, CC first | Only when the front-panel settings menu is exited (EEPROM 0x02, 0x03, 0x33; `0x080045AC` to `0x080045CA`). Otherwise they revert at power-off. |
| ON/OFF | Never. |
| List V, I, OVP, OCP, cut-off | Only if n is the selected list and the Chinese cursor is on that field. Writes to a non-selected list stay in RAM. |
| List select (0x09) | In the Chinese screen when the cursor is on the list-select box (cursor 6); written to EEPROM 0x3B. |
| Colours | Always. |

## 7. Protections

FIRMWARE. Trip flags: OVP `0x20000120`, OCP `0x20000121`, SCP `0x20000122`, OTP `0x20000123`, OPP `0x20000124`.

| Protection | Trip condition | Where | Checked with output off |
|---|---|---|---|
| OVP | OVP setting is less than or equal to the measured voltage (both in 10 mV). No debounce. Not active while a front-panel voltage-correction mode is on. | TIM2 1 kHz slot, `0x0800B040` to `0x0800B068` (Chinese: `0x0800AFB4` to `0x0800AFEC`) | Yes |
| OCP | OCP setting is less than or equal to the measured current (clamped to 12.00 A) for 100 consecutive 1 ms samples (about 100 ms). Not active while a front-panel current-correction mode is on. | TIM2 1 kHz slot, `0x0800B06A` to `0x0800B0B0` | Yes |
| OPP | `floor(Pout in mW / 1000)` is greater than the OPP limit in W. | Main loop, `0x08001368` (Digital) and `0x08002200` (Chinese) | Yes (power is 0) |
| OTP | Temperature (0.1 C) is greater than or equal to `n * 100`. | Main loop, `0x0800195E` and `0x08002224` | Yes |
| SCP | Never raised: the flag has no setter in V1.4.4. | `0x20000122` | n/a |

On a trip, FIRMWARE: the output-enable pin is driven low, both `0x20000101` and `0x20000102` are cleared, and the trip flag is set. The display shows `Warning: OVP` (or OCP, OTP, OPP) and an alarm tone sounds.

- Because OVP and OCP are compared with the measured terminal values even with the output off, a battery or a charged capacitor on the output, or an OVP below the voltage that is present, trips a supply that is off. OVP 0 trips within 1 ms and keeps re-tripping; OCP 0 trips after about 100 ms.
- OPP and OTP are checked in the main loop of the screens only; they are not checked while the settings menu is open.
- The alarm and the flag are cleared only when a front-panel key is down (the key scan runs from TIM3; clear at `0x08009328` to `0x0800933C`, which also stops the tone). No serial register clears a trip or reads one back.
- A remote ON (register 0x08) after a trip re-enables the output; nothing on that path reads the trip flags. MEASURED: after an OVP trip a later remote ON worked. If the fault persists it trips again.
- A trip is visible to a host only as byte 12 falling to 0 (section 5.4).

MEASURED: two OVP trips. (1) OVP 2.00 V with VSET 3.00 V (deliberate): the output turned off as the voltage rose, byte 12 went to 0 and byte 11 read 1; the alarm sounded until a front-panel key was pressed. (2) VSET 3.00 V with OVP 3.00 V (accidental): it tripped at the step from 2 V, because the voltage overshoots the setpoint during a step. This is why the tool keeps OVP at least `max(0.2 V, 1.5 %)` above VSET (section 13). Not yet MEASURED: OCP, OPP, OTP trips. No protection tripped in the second or third session. The cut-offs of the Chinese screen are not protections: they are silent (section 8).

OVP and OCP values set from the host are persisted only through the cursor rule in section 6.6; OPP and OTP only if the front-panel settings menu is exited afterwards.

## 8. Cut-off current and cut-off time (Chinese screen)

FIRMWARE. Both run in the TIM3 interrupt, once per second, and only when the interface is the Chinese one. They use the selected list.

Cut-off current (`0x0800C7A0` to `0x0800C808`): while byte 12 is 1, if Iout is below `value * 10 mA`, a counter increments; otherwise it resets. It also resets while the output is off. When the counter is greater than 5 (about 6 consecutive seconds; the manual says 5 s), the output is switched off: PC6 low, byte 12 and `0x20000101` cleared, flag `0x200001A0` set. Value 0 disables the function. The flag is not reported over serial. MEASURED: with a cut-off current of 0.1 A and about 0 A flowing, the output switched itself off after about 5.5 to 6 s (the firmware counter has to exceed 5).

Cut-off time (`0x0800C80A` to `0x0800C858`): if `h * 3600 + m * 60 + s` is less than or equal to the accumulated on-time `0x20000128`, the output is switched off the same way (flag `0x200001A1`). The check runs whatever the output state is.

- The on-time counts seconds while byte 12 is 1, in any screen (`0x0800C70E`). It accumulates across OFF and ON. It is reset only when the output comes back on after a cut-off-time trip (`0x0800B2AC`), or by the front-panel statistics clear. It is not reset by a normal OFF/ON. MEASURED: the cut-off time compares this accumulated on-time since power-up, not the time since the last ON.
- Default is 99:60:60 (about 100 h): effectively off. Total 0:0:0 cuts the output within about 1 s of every ON; it does not disable the timer.
- Setting a total below the accumulated on-time makes the next ON be cut within about 1 s (INFERRED from the code). MEASURED with a total of 0:00:10 after the supply had accumulated more on-time than that since power-up: the first ON was cut at once.
- MEASURED: after a cut-off-time trip the counter restarts. The second ON stayed on for about 9 to 10 s and was then cut.
- Hours, minutes and seconds are independent fields, so a full time needs 3 frames (`A5 0F 05 01 15 5A` sets the minutes field to 5; VENDOR example).
- Persistence: if n is selected and the cursor is on the cut-off time, exactly one byte is written, chosen by the digit sub-cursor (hours, minutes or seconds; EEPROM 0x46, 0x47, 0x48 plus 13n; `0x08007406` to `0x0800748E`).

MEASURED: both cut-offs are silent. The supply did not beep (the user confirmed), unlike a protection trip (section 7). For a host the only sign is byte 12 falling to 0 (section 5.4); the tool's `status` read it after each cut-off. When the output flag is still off at the end of the wait of `on`, the tool's note names a Chinese-screen cut-off (silent; on-time since power-up) as a possible cause.

## 9. EEPROM writes and wear

FIRMWARE, plus MEASURED where stated.

The supply keeps its settings in an external I2C EEPROM (device address `0xA0 | ((addr >> 8) << 1)`; a 24Cxx-class part, INFERRED from that addressing). The internal flash is never programmed (the image contains no flash unlock key; hand check of the disassembly). The EEPROM byte writer (`0x08000384`) sends start, device byte, address, data and stop, then waits 10 ms. It reads and compares nothing before writing. Addresses are fixed. There is no wear levelling and no ring buffer.

Every frame saves the cursor field. The flag set by the parser at `0x0800090C` is the same flag the encoder sets when the knob turns (`0x20000216`; the other direction is `0x20000217`). The screen loops check it on every pass (`0x08001330`, called from `0x0800274C` and `0x08007744`; Chinese: `0x080066AC`). When it is set, the loop writes the field under the cursor (`0x2000011C`) and clears it. This happens for every valid frame, including status queries (ID 0xFF) and unused IDs.

Code evidence that a status query sets the flag (FIRMWARE, checked by hand): after the checksum and the tail pass, the instructions at `0x0800090C` to `0x08000910` store 1 to `0x20000216`. That happens before the ID is read at `0x08000912` and dispatched, so the 0xFF query sets the flag as well. The query case at `0x08000EC0` only sets the reply-pending flag `0x20000207`. The main-loop save (`0x08001DEE` calling `0x08001F86`) writes the field under the cursor whenever the flag is set. The EEPROM writes themselves were not observed on the hardware; that would need a probe on the EEPROM's I2C lines (section 15).

| Screen and cursor | Written per frame |
|---|---|
| Digital or Waveform, cursor 0 to 5 | 2 bytes: VSET (0x04/0x05), ISET (0x06/0x07), OVP (0x08/0x09), OCP (0x0A/0x0B), DELAY (0x0C/0x0D) or REV (0x0E/0x0F). |
| Digital or Waveform, cursor 6 (the DATA box) | Nothing. |
| Chinese, cursor 0 to 5 | 2 bytes of the selected list value (cursor 0 to 4), or 1 byte of the cut-off time (cursor 5). |
| Chinese, cursor 6 (list select) | 1 byte (0x3B). Entering the Chinese screen also forces one such save (`0x0800E264`). |
| Factory mode | The factory table, 0x15 to 0x20. |
| Settings menu open | No write; only a redraw. |

Cursor range: on the Digital and Waveform screens the knob clamps the cursor to 0 to 6; the boot cursor is 0 (V-SET) (`0x0800E18A`). Cursor 6 is the DATA (CRL) box: with it selected, a frame saves nothing (`0x08001F8A`). On the Chinese screen the boot cursor is 6.

Wear, derived: with the cursor on V-SET (the default), each frame rewrites two bytes at EEPROM 0x04 and 0x05, 20 ms of main-loop time. Queries are frames too. The vendor PC app polls every 100 ms (VENDOR: PC app V1.2): about 10 writes per second, about 1 million writes in about 28 hours. 24Cxx parts are typically rated for about 1 million cycles; the actual part and its rating are not known (the rate is INFERRED, the code path is FIRMWARE). The write does not block reception, but it stalls the screen.

Tip (FIRMWARE-derived): before a long automated session, select the DATA box on the front panel (cursor 6) so that frames write nothing. Do not turn the knob while DATA is selected: that clears the Ah, Wh and time statistics (VENDOR manual). The consequence is that nothing sent over serial is then saved on a power cycle, which is the intended effect.

Settings sent over serial are kept over a power cycle only if their field is the one under the cursor when a frame arrives (VSET by default); OPP, OTP and CC-first are kept only when the front-panel settings menu is exited; colours are always kept; the output state never.

### EEPROM map

FIRMWARE (defaults `0x0800D9F0` to `0x0800DE3E`, loader `0x0800DE40`). Multi-byte settings are big-endian.

| Address | Content |
|---|---|
| 0x00 | interface |
| 0x01 | display flip |
| 0x02 | OPP |
| 0x03 | OTP |
| 0x04 to 0x0F | VSET, ISET, OVP, OCP, DELAY, REV (2 bytes each) |
| 0x10, 0x11 | two values not reachable from serial (`0x20000103`, `0x20000067`; meaning unknown) |
| 0x12 | magic `0x43` |
| 0x13 | current filter |
| 0x14 | factory mode |
| 0x15 to 0x20 | factory table |
| 0x23, 0x24 | a value not reachable from serial (`0x2000003A`; meaning unknown) |
| 0x25 to 0x30 | voltage and current calibration |
| 0x32 | key beep |
| 0x33 | CC first |
| 0x3B | selected list |
| 0x3C + 13n | list n: V, I, OVP, OCP, cut-off current (2 bytes each), then hours, minutes, seconds (1 byte each); list 4 ends at 0x7C |
| 0x7D | colour magic `0x5D` |
| 0x7E to 0x91 | 10 colours (2 bytes each) |

A list index of 5 or more makes the list save write at 0x3C + 13n, which for n = 5 is 0x7D to 0x89: it overwrites the colour magic and colours (address arithmetic FIRMWARE; effect on the real part INFERRED).

First-boot defaults (FIRMWARE `0x080011EC` to `0x0800125E`): Digital VSET 12.00 V, ISET 10.10 A, OVP 30.50 V, OCP 10.50 A. Lists 0 to 4: VSET 5, 12, 20, 24 and 30 V, each with ISET 10.10 A, OVP 30.50 V, OCP 10.50 A, cut-off current 0, cut-off time 99:60:60. Settings: interface Waveform, OPP 310 W, OTP 80 C, CC first on.

## 10. Front-panel-only state and settings-menu effects

### 10.1 State with no serial register

FIRMWARE (the dispatcher has no store there):

- interface (screen choice), display flip, key beep, current filter;
- voltage and current calibration and the correction modes;
- factory mode;
- statistics: on-time, charge, energy (cleared by turning the knob with the DATA box selected);
- the trip flags and their reset (a key press).

The cursor, the screen and the settings cannot be read back either.

### 10.2 Settings menu

FIRMWARE. Entering the on-device settings menu has these effects:

- the output is switched off once (`0x08003AFE` to `0x08003B06`);
- Vout, Iout and Pout are no longer refreshed, so a status reply carries frozen values (`0x0800C1CE`);
- regulation PWM updates are skipped and pin PB13 (an output discharge, INFERRED) is forced low (`0x0800B2F4`);
- OPP and OTP are not checked (the main loop runs only the menu routine);
- OVP and OCP in the 1 kHz slot stay active;
- serial frames are still accepted and acknowledged. A frame only redraws, with no EEPROM write.
- a remote ON (0x08) still drives the output-enable pin on. With regulation frozen and OPP/OTP unchecked this is a hazard (code FIRMWARE, effect INFERRED). A host cannot detect that the menu is open (frozen values look like steady ones), so do not switch the output on from serial while someone may be in the settings menu.
- on exit the menu saves OPP, OTP, CC first and the other settings (`0x08004592` to `0x080045D4`).

## 11. Timing recommendations for hosts

INFERRED from the code paths above, unless marked.

1. Send each 6-byte frame in one write.
2. Wait for the ACK. It arrives within about 0.2 ms of the tail on the wire (MEASURED about 13 ms through USB). Use a timeout of 20 to 50 ms at least; the tool uses 300 ms.
3. If no ACK arrives, stay idle for at least 11 ms so that the parser is hunting, then flush input and resend.
4. After a query, wait for the full 15-byte reply (up to about 55 ms; allow 100 ms) before sending anything else.
5. Treat replies as 20 Hz at most; faster polling coalesces. Values change at 10 Hz (Vout, Iout, Pout) or less.
6. Validate every reply (head, tail, checksum) and resynchronise on the next `A5`.
7. After colour frames, wait at least 300 ms after the last one; resend the final colour if in doubt.
8. Poll slowly (1 to 2 Hz) in logs. Every frame costs an EEPROM write (section 9). Use the DATA-box tip for long fast sessions.
9. After the output is switched on, wait about 1.5 s for the soft start before trusting Vout and byte 11.

## 12. Measured timings and accuracy

MEASURED on 2026-11-07, in three sessions (AX PP300S firmware V1.4.4, Windows 11, CH340, input 19.8 V). Timings are from the first session unless marked.

### 12.1 Timings

| Operation | Time |
|---|---|
| Open the port | about 1 ms |
| Close the port | about 0.4 ms |
| ACK after the frame | about 12 to 14 ms (USB latency; the firmware sends it in about 0.2 ms) |
| Status reply after the query | 15 to 55 ms, median about 48 ms |
| Output ON (DELAY = REV = 0): byte 12 | 1 at once |
| Output ON: voltage | about 0.011 V for about 1.1 s (soft start, byte 11 = 1), then the set voltage with about 0.3 s settling, byte 11 = 0 |
| Overshoot after ON | small: 1.0068 V for 1.00 V on the meter, 1.076 V once on the readback |
| Output OFF: byte 12 | 0 at once; Vout decays 2.0 V to 0.13 V in about 0.3 s into a 10 MOhm meter |
| VSET step with the output on | about 0.2 s to settle (0.94 s for 1.0 V to 0.5 V) |
| DELAY 3 s | output on 4.39 s after the ON command |
| REV 2 s | on after about 2 s, then toggles every 2 s |
| DELAY 3 s (second session) | output on 4.23 s after the ON command |
| VSET step, typical (second session) | about 0.18 s to settle |
| Output ON with the supply in CC (load in constant-voltage mode at 3.0 V; second session) | settled after 1.68 s |

Behaviours confirmed: a bad-checksum frame gets no answer; a frame split 3 + 3 bytes with 20 ms between gets no answer; garbage `00 11 5A A5` before a valid query swallows it; two queries in one write give one reply.

### 12.2 Voltage accuracy, no load

Against a Fluke 289 (10 MOhm input, V DC, auto range), output on, no load, 3 s steady per point.

First session, 0.5 V to 15 V:

| VSET (V) | Meter (V) | Meter minus VSET (mV) | Supply readback minus meter (mV) |
|---|---|---|---|
| 0.5 | 0.5029 | +2.9 | -8.5 (the mean includes settling) |
| 1.0 | 1.0022 | +2.2 | -1.6 |
| 2.5 | 2.5017 | +1.7 | -1.8 |
| 3.3 | 3.3047 | +4.7 | -1.9 |
| 5.0 | 5.0039 | +3.9 | -1.9 |
| 9.0 | 9.0070 | +7.0 | -4.1 |
| 12.0 | 12.0060 | +6.0 | -2.8 |
| 15.0 | 15.0050 | +5.0 | -2.7 |

Second session, 0.5 V to 30 V (input from a 65 W USB-PD charger, Vin 19.8 V):

| VSET (V) | Meter (V) | Meter minus VSET (mV) | Supply readback minus meter (mV) |
|---|---|---|---|
| 0.5 | 0.5026 | +2.6 | -8.6 (the mean includes settling) |
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

Every setting point of both sessions is within the vendor specification of 0.1 % + 5 mV (the largest deviation, +7.0 mV, at 9 V in the first session and at 12 V in the second, is inside the 14 mV and 17 mV allowed there; +2.9 and +2.6 mV at 0.5 V are inside 5.5 mV). Every readback point is too, except the 0.5 V mean, which includes the settling (-8.5 and -8.6 mV where 5.5 mV is allowed). The two sessions agree: +1.7 to +7.0 mV over 0.5 V to 15 V in the first, +2.6 to +7.0 mV over 0.5 V to 30 V in the second. Also in the second session: boost to 20, 24 and 30 V from the 19.8 V input works (no load); a VSET step settles in about 0.18 s (0.93 s for 1.0 V to 0.5 V); a sequence of 1, 2 and 3 V read 0.9998, 2.0049 and 3.004 V on the meter; lowering VSET from 30 V to 5 V and OVP from 30.5 V to 6 V in one call with the output on sent VSET first and OVP after the output had come down, with no trip. Voltage accuracy was measured with no load only.

### 12.3 CV under load

Electronic load in constant-resistance mode at 10 Ohm, the Fluke 289 (A jack) in series. With VSET 5 V and ISET 1 A (0.6 A in a second run) the supply stays in CV at about 0.5 A, and the tool read CV at every sample. The supply readback was 0.4943 A against 0.4940 A on the meter (+0.3 mA), and 0.4919 A against 0.4938 A in the second run (-1.9 mA).

### 12.4 Current limit (CC) accuracy

Electronic load in constant-voltage mode at 3.0 V, VSET 5 V, so that the supply has to limit the current; the Fluke 289 (A jack) in series. The meter had recently been checked on current: 5 A applied read 4.997 A, 50 mA read 0.04991 A, 400 mA read 0.39995 A (errors of -0.06 %, -0.18 % and -0.01 %), much better than the supply's 0.1 % + 5 mA.

Before the current calibration (second session):

| ISET (A) | Meter (A) | Meter minus ISET (mA) | Supply readback minus meter (mA) | Meter spread (mA) |
|---|---|---|---|---|
| 0.10 | 0.0864 | -13.6 | -6.2 | 2.7 |
| 0.25 | 0.2366 | -13.4 | -7.3 | 0.8 |
| 0.50 | 0.4864 | -13.6 | -2.6 | 0.4 |
| 1.00 | 0.9862 | -13.8 | -2.2 | 0.5 |
| 0.30 | 0.2867 | -13.3 | -2.7 | 0.8 |

The current limit was stable and linear, and the tool reported CC at every step. Every setting delivered about 13.5 mA less than ISET: a constant offset, outside the vendor specification of 0.1 % + 5 mA (5.5 mA at 0.5 A). That was the current calibration of an uncalibrated unit (INFERRED at the time; the manual says that current calibration needs a 10 A load and is not recommended for users; the front-panel calibration below removed the offset), not a tool problem: the tool sent the right raw value. The readback was 2 to 7 mA below the meter and noisier at low current (0.065 to 0.084 A at ISET 0.10 A).

Lowering ISET from 1.0 A to 0.3 A and OCP from 1.5 A to 0.6 A in one call while 1 A flowed sent ISET first and OCP after the current had come down; no trip.

The user then calibrated the supply's current on the front panel ("Current Correction" in the settings menu) with the Fluke 289 as the reference. The tool cannot calibrate: the firmware has no serial command for it.

After the current calibration (third session; tests/hw/bench_289_cc_cvload.py with --points and --cc-only; the supply in CC at every point, read as CC at every sample; OCP 5.8 A, OPP 100 W). Each point had a 1.5 s settle after the ISET step and then a 4 s window: the supply sampled every 0.5 s, the meter median over the window.

| ISET (A) | Meter (A) | Meter minus ISET (mA) | Meter spread (mA) | Supply readback mean (A) | Readback span (A) | Readback minus meter (mA) |
|---|---|---|---|---|---|---|
| 0.05 | 0.0518 | +1.8 | 8.4 | 0.0456 | 0.025 to 0.066 | -6.2 |
| 0.10 | 0.1023 | +2.3 | 2.8 | 0.0934 | 0.088 to 0.116 | -8.9 |
| 0.25 | 0.2523 | +2.3 | 0.4 | 0.2458 | 0.215 to 0.275 | -6.5 |
| 0.40 | 0.4022 | +2.2 | 0.5 | 0.3986 | 0.398 to 0.400 | -3.6 |
| 0.50 | 0.5021 | +2.1 | 0.7 | 0.4988 | 0.498 to 0.500 | -3.3 |
| 1.00 | 1.0022 | +2.2 | 0.5 | 0.9980 | 0.998 to 0.998 | -4.2 |
| 2.00 | 2.0016 | +1.6 | 0.6 | 1.9980 | 1.998 to 1.998 | -3.6 |
| 3.00 | 3.0012 | +1.2 | 0.6 | 2.9980 | 2.998 to 2.998 | -3.2 |
| 4.00 | 4.0004 | +0.4 | 0.3 | 3.9980 | 3.998 to 3.998 | -2.4 |
| 5.00 | 4.9996 | -0.4 | 0.3 | 4.9970 | 4.997 to 4.997 | -2.6 |

- The current setting is now inside the vendor specification of 0.1 % + 5 mA at every point from 0.05 A to 5 A: at most +2.3 mA (0.1 A to 1 A), falling to -0.4 mA at 5 A.
- The supply's readback is steady and inside the specification from 0.4 A up (2.4 to 4.2 mA below the meter). At 0.25 A and below it is noisy: single readings wander over 28 to 60 mA while the meter holds within 0.4 to 8 mA, and the mean reads 6 to 9 mA low. For small currents use the meter, or average many samples.
- At 0.05 A the regulated current itself wanders a little (meter spread 8.4 mA; 18 mA in a first run whose window included the step transient). A first run without the 1.5 s settle showed the same setting errors (-0.5 to +5.1 mA) but readback means distorted by the step transient.
- The output voltage rose from 3.04 V at 0.4 A to 3.49 V at 5 A: the load holds 3.0 V at its own terminals and the rest is the drop over the meter's current shunt and the leads (about 0.1 Ohm in all).
- Supply temperature 32.7 C to 36.0 C during the run (17.5 W at 5 A), input the 65 W USB-PD charger (19.8 V).

The earlier CC points were taken at an output of about 3 V and up to 1 A; the new ones also at about 3 V (2.9 to 3.5 V by the supply's readback) and up to 5 A.

### 12.5 CV first and CC first at switch-on

Switch-on into the 3 V constant-voltage load with ISET 0.5 A, sampled at 20 Hz:

| Priority (register 0x10) | Peak current (A) | Peak voltage (V) |
|---|---|---|
| CV first | 0.811 | 4.263 |
| CC first | 0.541 | 3.733 |

CC first limits the switch-on overshoot. With CV first the current exceeds ISET by about 60 % for a moment; an OCP close to ISET could trip then (OCP needs 100 ms above the limit). The readback refreshes every 100 ms (section 5.5), so the true peaks may be higher than these readings. The priority was set back to CC first, the factory default (a priority set over serial is lost at power-off unless the settings menu is exited afterwards, section 6.6).

### 12.6 Testing CC with an electronic load

Do not test the current limit with the load in constant-resistance (CR) mode. When the supply has to limit the current, the CR loop of the load and the CC loop of the supply fight: the current jumped between about 0 and 1.2 A every 0.5 to 2 s (the meter saw it too), and the supply sat in CV most of the time; the mode the tool read alternated between CV and CC. Even so, the charge logged by the tool over 10 s (0.00152 Ah) matched the meter (0.00158 Ah) within 4 %. Test CC with the load in constant-voltage mode set below VSET, or with a real resistor.

### 12.7 Other observations

One serial error was seen: a WriteFile "access denied" (PermissionError 13) while the output came up to 3 V right after the first OVP trip and a front-panel intervention. Reopening the port worked, and no Windows device removal event was logged. The tool reopens the port and retries after a serial I/O error.

## 13. How this tool applies the facts

| Fact | What the tool does |
|---|---|
| The firmware checks no range (section 6). | Every value is converted and checked against the front-panel range before anything is sent. Out-of-range values are refused. Setpoints are rounded to 10 mV and 10 mA and the result says what was sent. OPP must be a multiple of 10 W (10 to 310) and OTP 60, 70, 80, 90 or 100 C. OVP and OCP never go below the front-panel minimums (0.50 V, 0.10 A). |
| Unknown IDs are acknowledged but still write the EEPROM; wrong values can be dangerous. | `raw` refuses unknown IDs and out-of-range data unless `--unsafe` is given, and routes documented registers through the same guarded paths as the other commands. |
| An OVP at or near VSET trips on the step overshoot (section 7). | The tool requires `OVP >= VSET + max(0.2 V, 1.5 %)`, checked against the values given and against the ones it sent last. OCP is warned about when it is not above ISET by `max(0.05 A, 2 %)`. |
| OVP and OCP are compared with the terminal values with the output off too. | A new OVP or OCP below what the terminals show now (plus a margin) is sent after the setpoints, once the output has come down. If the output is on and the new value would trip it (it is not accompanied by a lower setpoint), the call is refused. OPP must be above the present power and OTP above the present temperature (margins 5 W and 5 C). |
| Order of frames matters. | Within one call: OPP, OTP, CC priority, then OVP and OCP that are above what the terminals show, then VSET, ISET, DELAY, REV and cut-off current. Lowering VSET and OVP together, or ISET and OCP together, with the output on works without a trip (MEASURED). |
| The supply cannot report its settings. | The tool records every value it sends and shows it with the status (`last_sent`). Changes that should be visible on the output (VSET with the output on, ON, OFF) are checked against the readback. |
| One frame in flight; interleaving hazard; 10 ms parser timeout. | One frame at a time, 3 ms minimum gap after the last byte received, a timeout of 0.3 s and 2 retries; after a missing answer it waits 80 ms and flushes the input before resending. Setting frames and queries are idempotent, so resending is safe. A port I/O error reopens the port. |
| Output ON has a soft start and byte 11 is not meaningful off. | `on` waits for the voltage; the CC flag is read as a mode only when byte 12 is 1. The soft start is allowed 1.6 s. If DELAY or REV (as last sent by the tool) is longer than 20 s, `on` returns after the frame instead of waiting, unless `--wait-timeout` is given. `on` takes a CV reading at the Digital VSET, or at the selected list's VSET (each as sent last), as settled; if the reading matches neither it accepts a stable CV reading 1 s after the soft start and says so. |
| A Digital register is ignored on the Chinese screen, and the list that drives the output is not readable. | With the output on, a VSET that has no effect is reported as NOT_APPLIED after 3 s. With the output on, a list selection whose OVP (as sent last) is not above the voltage now at the output plus a margin is refused before sending. Both MEASURED on the Chinese screen. |
| The output can be switched on from the front panel. | `status` notes an output that is on when the tool did not switch it on. |
| Frames cost EEPROM writes. | While waiting, the tool polls at 10 Hz (1 Hz while waiting out DELAY or REV); a command opens and closes the port, so a background log and a foreground command share the supply. |
| A colour frame starts a save of about 210 ms, and a colour frame that arrives during the save may miss it. | After the colour frames the tool waits 0.35 s and sends the last one again, so that the save includes every colour. |
| Bench limits. | Voltage-like and current-like values above the user-set bench limits are refused, and `on` (unless `--no-wait`) or list selection switch the output off again at once if the measured voltage exceeds the voltage limit by more than 2 % + 50 mV. |

## 14. Safety summary

| Rule | Reason |
|---|---|
| Keep OVP at least max(0.2 V, 1.5 %) above VSET. | Step overshoot tripped OVP 3.00 V at VSET 3.00 V. A trip sounds an alarm until a front-panel key is pressed. |
| Never send OVP or OCP of 0, or OTP of 0 to 5. | OVP, OCP or OTP of 0 is a permanent trip, even with the output off; OTP 1 to 5 trips at 10 to 50 C. |
| Never send 0x09 with data of 5 or more, and never send unknown IDs. | The index is persisted unchecked and can corrupt the EEPROM. Unknown IDs still trigger an EEPROM write. |
| Clamp every value to the front-panel range. | The firmware accepts and stores anything. |
| Do not poll fast for long. | Every frame writes the EEPROM (about 10 writes per second at 10 Hz on V-SET). |
| Do not switch the output on while the settings menu is open. | Regulation is frozen and OPP/OTP are not checked. |
| Treat a drop of byte 12 to 0 as a possible trip or cut-off. | It is the only sign a host gets, and cut-offs of the Chinese screen are silent. |
| Keep CC first (the default) when an OCP close to ISET is set. | With CV first the switch-on current overshot ISET by about 60 % (MEASURED); OCP needs 100 ms, but a close OCP could trip. |
| Connect a capacitive or inductive load before switching the output on (vendor manual). | Connecting such a load to an output that is already on causes a large current surge. |

## 15. Open questions

| Question | Notes |
|---|---|
| What does the CC-flag ADC channel sense, and why does byte 11 read 1 with the output off? | Byte 11 is now MEASURED as 0 in CV under load and 1 in CC under load (section 5.3). The reason for the 1 with the output off is still INFERRED. |
| Can the second ADC gate on the output-enable pin hold the output off in normal use (for example a high input voltage), so that byte 12 reads 1 while the output is dead? | FIRMWARE shows the gate; its sense point is unknown. |
| What happens to the PWM for VSET or ISET above 3000 and 1010 (saturate or wrap)? | Do not test. The host clamps. |
| What is the EEPROM part size and rating? | Decides what an out-of-bounds list save does and the real wear budget. Not confirmed that each poll causes an I2C burst (scope SCL/SDA or look for display stalls). |
| What is the exact HSE frequency and MCU part? | The baud-rate divisor assumes an 8 MHz crystal (INFERRED). The firmware uses more than 48 KB of RAM, so the part is not a genuine 48 KB device. |
| Exact worst-case query latency, including the time the TIM3 handler spends before the reply (a held key adds a 1 ms delay). | Estimate: about 51 ms. |
| What does the discharge pin do when the output is switched on with the menu open? | Role INFERRED. |
| What are the values at EEPROM 0x10, 0x11 and 0x23/0x24? | Not reachable from serial; not traced. |
| Exact mapping of colour IDs 0x53 and 0x54 to EEPROM slots. | Does not matter for the protocol. |
| Why did every CC setting deliver about 13.5 mA less than ISET on this unit? | It was a MEASURED constant offset before the user calibrated the current on the front panel; after the calibration the setting is inside the vendor 0.1 % + 5 mA from 0.05 A to 5 A (section 12.4). The tool sent the right raw value throughout and cannot calibrate (no serial command); if the current is off, calibrate on the front panel. |
| Do status queries and other frames write the EEPROM on the real supply? | Code evidence only (section 9). Needs a probe on the EEPROM's I2C lines. |
| OCP, OPP and OTP trips, the settings-menu effects, currents above 5 A, powers above about 20 W, the DC5525 input, and CC accuracy at output voltages other than about 3 V. | Not verified on hardware yet (see "Hardware status"). |
