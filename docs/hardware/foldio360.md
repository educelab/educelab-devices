# Foldio360 turntable and halo

The Orangemonkie Foldio360 is a Bluetooth LE turntable with a built-in LED ring (the
halo). The driver is `educelab.devices.stages.foldio360`.

Everything here was reverse-engineered from two Android HCI snoop captures of the official
app driving a Foldio360, firmware `V1.0.2_1712M02669`, plus live probing of the same unit
from a direct client. Both captures are a **single continuous BLE connection** (t = 53.8 s
– 2230.0 s, one discovery pass, one CCCD write, disconnect reason `0x16` = terminated by
local host). See [Capture provenance](#capture-provenance).

Everything marked **verified** was observed directly; everything marked **assumed** or
**unknown** has not been tested and should be treated as a hazard by the driver (see
[How sure we are](README.md#how-sure-we-are)).

## Identity

| | |
|---|---|
| Vendor | Orangemonkie |
| Model | Foldio360 |
| Firmware seen | `V1.0.2_1712M02669` (one unit) |
| Reading the version | `get_status` replies with the firmware string ([Command reference](#command-reference)) |

**Advertisement** (verified live against firmware `V1.0.2_1712M02669`, not from the
captures — both captures begin after discovery):

| Field | Value |
|---|---|
| Local name | `Foldio360` |
| Service UUIDs | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` (NUS) |
| Service data | none |
| Manufacturer data | none |
| TX power | −4 dBm |

Scanning by service UUID and by name are therefore both sufficient on their own.

The BLE address is unchanged across a power cycle (verified), so a cached address survives
even though the device's state does not ([State and recovery](#state-and-recovery)).

## Transport

There is no proprietary service. The vendor exposed a plain-text serial console over a
stock **Nordic UART Service (NUS)**, so any NUS-capable client works unmodified.

| Attribute | Handle | UUID | Properties | Role |
|---|---|---|---|---|
| Service | 0x000C–0x0011 | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — | NUS |
| TX (device → client) | 0x000E | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | Notify (0x10) | Responses |
| CCCD | 0x000F | `00002902-…` | Read/Write | Must be enabled first |
| RX (client → device) | 0x0011 | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | Write, WriteNR (0x0C) | Commands |

A legacy Nordic DFU service occupies handles 0x0012–0x0019 and is never touched during
normal operation. Ignore it.

Only one BLE central may hold the link, so the official app must be closed (or the phone's
Bluetooth disabled) before a host can connect.

### Framing

- Commands are **bare ASCII**. No terminator, no length prefix, no checksum. The closing
  `)` is the only delimiter; `get_status` and `continue` have no parentheses at all.
- The app writes with **ATT Write Command (opcode 0x52, unacknowledged)** exclusively.
  The characteristic also advertises acknowledged Write, which is untested but should work.
- **No ATT MTU exchange occurs** — the link stays at the 23-byte default, giving a 20-byte
  payload ceiling.
- One command (`set_signal`) exceeds 20 bytes and is split across two consecutive writes.
  **The firmware reassembles a byte stream; one write ≠ one command.** A client must chunk
  long commands at 20 bytes, and an emulator must buffer until `)`.

### Connect sequence used by the app

1. Discover services/characteristics.
2. Write `0x0001` to the CCCD at handle 0x000F.
3. Wait ~500 ms.
4. Send `get_status`.

Whether the ~500 ms settle in step 3 is **required** is unknown — it is the app's
behavior, and a client that copies it cannot tell. `get_status` itself replies in
**35–65 ms** (3 samples, live), so the wait is not response latency.

## Protocol

### Command reference

All commands go to handle 0x0011. All responses arrive as notifications on 0x000E.

| Command | Response | Verified |
|---|---|---|
| `get_status` | firmware string, e.g. `V1.0.2_1712M02669` | yes |
| `rotate(<dir>,<deg>,<speed>,<mode>)` | `OK` on **motion complete** | yes |
| `cancel(<mode>)` | `OK` | yes |
| `set_bright(<n>)` | `OK` | yes |
| `run(<frames>,<speed>)` | current frame index (starts at `1`) | yes |
| `continue` | next frame index, or `0` when the sequence ends | yes |
| `ir_shutter(0)` | current frame index, echoed | yes |
| `set_signal(<blob>)` | `OK` | yes |
| `get_position` | absolute angle, 0–359 | yes (undocumented, [get_position / get_bright](#get_position--get_bright)) |
| `get_bright` | current LED level, 0–100 | yes (undocumented, [get_position / get_bright](#get_position--get_bright)) |

The last two appear in **no capture and no vendor documentation** — the official app never
sends them. They were found by probing the firmware's vocabulary from a direct client, and
`get_position` in particular changes how a client should be built
([Notes for a client](#notes-for-a-client)).

The full vocabulary is exactly ten commands: the eight the app uses plus these two. Roughly
forty other plausible names (`home`, `get_state`, `get_speed`, `info`, `version`, `reset`,
…) all answer `FAIL`.

The driver does not expose `run`/`continue`, `ir_shutter`, or `set_signal`; they are
documented here for completeness.

### rotate

`rotate(dir, deg, speed, mode)`

| Arg | Type | Observed values | Meaning |
|---|---|---|---|
| `dir` | literal | `CW`, `CCW` | Direction. **The app's *Left* button emits `CW`, *Right* emits `CCW`.** |
| `deg` | int | 1, 5, 10, 25, 30, 45, 60, 90, 380, and 0 (jog only) | Degrees to travel |
| `speed` | int | 1, 2, 3 | See [Timing](#timing) |
| `mode` | int | 0, 1 | `1` = travel a fixed angle and stop. `0` = run until cancelled (`deg` is then also 0) |

The Left/Right inversion is confirmed twice over: by the capture, and independently by the
operator's own session notes from the reverse-engineering runs, which record *right* as
`CCW`. It is the single easiest thing in this document to talk yourself out of — see
[Notes for a client](#notes-for-a-client).

- `OK` fires when the motion **finishes**, not on receipt. A 380° move goes 52 s with no
  intermediate traffic.
- Angles **> 360 are executed literally**, not reduced modulo a revolution — `rotate(CCW,380,1,1)`
  ran 52.158 s, which is exactly the 380° prediction of the timing model, not the 20° one.
- `deg = 0` with `mode = 1` is **unknown** — the app suppresses it client-side (see
  [App quirks](#app-quirks--do-not-replicate)).
- `mode` values other than 0 and 1 are **unknown**.

**Jog acks on spin-up, not on cancel — verified.** At t = 213.269 the app sends
`rotate(CCW,0,3,0)` and `OK` arrives 142 ms later, while the platter is still turning;
`cancel(0)` 1.6 s afterwards draws its own `OK`. The ~142 ms matches the
[timing model](#timing) intercept (140 ms spin-up), so `mode = 0` acknowledges once the
motor is running. A client can therefore treat starting a jog as a fast, blocking call.

Note also t = 211.251, where `rotate(CCW,0,3,0)` and `cancel(0)` are sent 98 ms apart and
**only one `OK` comes back** — the same dropped-ack behavior [set_bright](#set_bright)
documents for brightness, so it is not unique to brightness. Back-to-back commands under
~100 ms can cost an acknowledgement.

### cancel

`cancel(mode)` stops motion. The argument mirrors the `mode` of the `rotate` being
cancelled (`cancel(1)` after a fixed-angle move, `cancel(0)` after a jog). Safe to send when
nothing is moving — the device still replies `OK`. Idempotent in practice.

Whether `cancel(1)` stops a fixed-angle `rotate` that is still in progress is **unknown**:
every `cancel` in the captures follows either a jog or a move that had already reported
`OK`. Settle it by sending `cancel(1)` partway through a long `rotate` and reading
`get_position` afterwards.

### set_bright

`set_bright(n)` sets the halo LED ring. `n` is an integer **0–100**; both endpoints verified
(`set_bright(0)`, `set_bright(100)`, both acked). Takes effect immediately.

Rapid writes overrun the device: a slider drag emitting a write every ~15 ms produced
9 `OK`s for 15 writes, and 4 `OK`s for 12 writes. The values still land — only the acks
are lost. **Pace to ~50 ms** if you need per-value confirmation.

### run / continue / ir_shutter

`run(frames, speed)`, `continue`, and `ir_shutter(0)` make up the 360° capture sequence,
and the only stateful part of the protocol. `run` does **not** free-run: the turntable
steps, reports its position, and blocks until told to continue. The client therefore owns
all shutter timing.

```
→ run(24,3)
← 1              # ~150 ms; frame 1 is the CURRENT position, no motion yet
   ... client exposes a frame ...
→ continue
← 2              # ~1.27 s; platter advanced 360/24 = 15°
   ... repeat through frame 24 ...
→ continue
← 0              # sequence complete
```

- `frames` divides a full revolution: step angle = `360 / frames`, and `speed` follows
  the [timing](#timing) table.
- **`frames` is a whitelist, not an arbitrary integer — verified.** Only **24, 36 and 48**
  complete. Every other value tested (2, 4, 6, 8, 12, 16, 18, 25, 30, 60, 72, 96) accepts
  the command and returns frame `1`, then **never answers `continue`** — the sequence
  wedges silently, with no error, and stays wedged until `cancel(1)`, which recovers it
  normally. This is not a divisibility rule: 12, 60 and 72 are multiples of 12 and all
  wedge. **The official app surfaces exactly 24, 36 and 48 and nothing else** (confirmed by
  the operator), so the firmware implements only the paths its own app can reach.
  A client should pass `frames` through rather than reject it, but must treat a stalled
  `continue` as the expected failure for an untried count.
- Frame indices are returned as **bare decimal ASCII**, not `OK`.
- `0` is the terminator, so indices are 1-based and `0` is never a valid frame.
- Step timing tracks the same rate model as `rotate` ([Timing](#timing)).

With an IR-triggered camera the app inserts a shutter command per frame, which the device
acknowledges by **echoing the same frame index**:

```
← 1
→ ir_shutter(0)
← 1              # same index — this is the shutter ack, not a new frame
→ continue
← 2
```

Frame cadence measured: **1.29 s/frame** without `ir_shutter`, **2.32 s/frame** with it,
at 24 frames / speed 3.

**`ir_shutter` requires a prior `set_signal` — verified.** On a device that has not been
given a waveform, `ir_shutter(0)` answers `FAIL`, inside a `run` sequence as much as
outside one. Replaying the capture's Canon blob makes the very next `ir_shutter(0)` succeed.
The app hides this by sending `set_signal` when a camera model is picked, so the dependency
never shows up as a failure there. A client that wants IR triggering must select a waveform
first; one that triggers its camera by other means can ignore `set_signal` entirely.

`ir_shutter`'s argument is constrained: `ir_shutter(2)` answers `FAIL`, so `0` is not merely
the only value ever sent but one of a small valid set. Its meaning is still **unknown**.

Note also that `ir_shutter(0)` works **outside** a `run`, once a signal is set, returning `1`.

The full 24-frame sequence completed identically **with no camera connected**. Nothing in
the handshake depends on the camera, so a client can drive `run`/`continue` and capture
images by any means it likes.

### set_signal (partially decoded)

Sent when a camera model is chosen in the app. The only non-ASCII command, and the only
one that exceeds the MTU.

```
write 1 (20 B)  73 65 74 5f 73 69 67 6e 61 6c 28 31 32 21 03 10 27 03 e7 01
write 2 ( 7 B)  32 32 a2 1c e7 01 29

reassembled     "set_signal(" 31 32 21 03 10 27 03 e7 01 32 32 a2 1c e7 01 ")"
                              └─"12"─┘  ← probably the camera-model index
```

Reading the body as little-endian `uint16` after the leading `0x21`:

```
4099   807   487   12850   7330   487
```

`487 … 7330 … 487` matches the **Canon RC-1** remote signature (≈490 µs burst, 7.33 ms gap,
≈490 µs burst), which is good evidence the body is a microsecond timing list. The framing
of the rest — the leading `0x21`, and 4099 / 807 / 12850 — is **not established from one
sample**.

A Python client does not need this to work: IR emission is `ir_shutter`, and `set_signal`
only selects which waveform that fires. Capture the blobs for the brands you care about by
stepping the app's camera picker and diffing.

### get_position / get_bright

Undocumented queries. Neither appears in any capture. Both were found by sending plausible
command names to the firmware and keeping whatever did not answer `FAIL`.

```
→ get_position
← 302            # absolute angle, 0-359, counted CLOCKWISE
→ get_bright
← 37             # matches the last set_bright
```

`get_position` is the significant one. Verified behavior:

| Property | Result |
|---|---|
| Range | 0–359, wraps cleanly through 0 |
| Direction | **Increases clockwise**, decreases counter-clockwise |
| Tracks fixed moves | Yes — `rotate(CW,45,3,1)` moves it +45 |
| Tracks jog | Yes — a 2 s jog at speed 1 moved it +15, against 14.4 predicted |
| Survives reconnect | Yes — it is firmware state, not session state |
| Resolution | ±1° (quantization, not drift: errors scatter and do not accumulate) |

There is **no `set_position`** — it answers `FAIL`, as do `home`, `zero`, `goto` and every
other name tried. The counter is read-only, so reaching an angle means turning until it
reads that angle. That is enough for repeatable homing, which the protocol was previously
believed not to support at all.

The counter re-zeros at power-on and integrates commanded motion rather than sensing the
platter; see [State and recovery](#state-and-recovery).

### Response grammar

The notify characteristic carries exactly four things:

| Pattern | Meaning |
|---|---|
| `OK` | The last command completed. For `rotate`, means **motion finished**. |
| `FAIL` | The device rejected the command. **No detail** — it does not say why. |
| `V<major>.<minor>.<patch>_<serial>` | Reply to `get_status` |
| decimal integer | Frame index during `run`, or the reply to `get_position` / `get_bright` |

**`FAIL` is a real error response, and the firmware does validate its arguments.** Earlier
revisions of this document said no error had ever been observed and that the parser should
be assumed permissive; both were wrong, and wrong for the same reason — the official app
never sends anything invalid, so no capture contains a `FAIL`. Verified rejections:

| Command | Reply | Reading |
|---|---|---|
| `set_bright(101)`, `set_bright(-1)` | `FAIL` | brightness range is enforced |
| `run(24,4)`, `rotate(CW,1,4,1)` | `FAIL` | speed is enforced |
| `ir_shutter(2)` | `FAIL` | the argument is a small valid set, not free |
| `ir_shutter(0)` with no `set_signal` | `FAIL` | a missing precondition, not a bad argument |
| `garbage`, `help`, `home`, `rotate(` | `FAIL` | unknown commands are rejected, not ignored |
| `rotate(CW,1,1,2)`, `…,9)` | `OK` | **`mode` is *not* validated** — see [rotate](#rotate) |

So a client still wants its own validation, not because the firmware is permissive but
because `FAIL` says nothing about *which* argument was wrong. Note the one gap: `mode`
accepts anything, so a typo there fails silently rather than loudly.

An unterminated command such as `rotate(` answers `FAIL` immediately rather than buffering
and swallowing the next command, so the parser does not delimit on `)` alone.

All observed notifications are ≤ 17 bytes and arrive whole, but a robust client should
still buffer rather than assume one notification = one response. Responses have no
terminator, so a digits-only notification is ambiguous: a complete `3` and the head of a
fragmented `302` are the same bytes. The driver waits 30 ms after a digits-only reply, and
up to 250 ms after any reply that doesn't match the grammar, before believing it.

### Command/response pairing

The protocol has no sequence numbers or command echoes, so responses are matched purely by
order. Consequences for a client:

- **Keep one command outstanding at a time.** There is no way to correlate an `OK` with a
  specific command otherwise.
- `rotate` and `run` block for seconds to a minute. Don't apply a short uniform timeout.
- `set_bright` acks are unreliable under load ([set_bright](#set_bright)), so a strict
  "wait for `OK` before the next command" loop will stall on brightness ramps. Either pace
  brightness writes or treat them as fire-and-forget.
- During a `run` sequence, `ir_shutter` and `continue` both reply with an index. They are
  distinguishable only by which one you sent — `ir_shutter` echoes the current index,
  `continue` returns the next.

### App quirks — do not replicate

These look like protocol rules until you compare captures. They are all client-side, and a
direct client is free to ignore every one.

**Manual angle entry always sends speed 1.** Two identical angle ladders were run, one with
the app's speed selector on 3×, one on 1×. Every command was byte-identical
(`rotate(CCW,N,1,1)`) and every duration matched within 40 ms:

| deg | selector 3× | selector 1× |
|---|---|---|
| 1 | 0.242 s | 0.275 s |
| 5 | 0.783 s | 0.903 s |
| 10 | 1.554 s | 1.548 s |
| 25 | 3.534 s | 3.569 s |
| 30 | 4.235 s | 4.193 s |
| 45 | 6.257 s | 6.248 s |
| 60 | 8.301 s | 8.374 s |
| 90 | 12.473 s | 12.485 s |

Only the 90° preset button, the jog control and `run` honor the speed setting. A direct
client can do what the app cannot: `rotate(CCW,25,3,1)`.

**The Repeat setting never reaches the wire.** `mode` stays `1` whether Repeat is Once or
Infinite. What actually changes is that on Infinite the app stops sending its automatic
`cancel` and never clears its "moving" UI state. In the capture the device finished
normally and reported `OK`; the app sat waiting until cancelled by hand 14.4 s, 28.7 s and
10.9 s later on three separate moves. **Infinite repeat is not implemented on the device.**
To repeat, re-issue `rotate`.

**The trailing `cancel` is bookkeeping.** After a typed-angle move with Repeat = Once, the
app emits `cancel(1)` 3–17 ms after the completion `OK` — fired from the notification
handler, not from a button release. The 90° preset never does this. It has no effect on the
motor and is not required.

**0° is suppressed client-side.** Three attempts to enter 0 in the manual field produced
29 s of complete radio silence. The firmware's behavior on `rotate(dir,0,s,1)` is therefore
untested — note `deg=0, mode=0` is the jog command, so a fixed-angle zero may or may not be
handled.

## Timing

22 timed moves from 1° to 380° at speed 1 fit a straight line with a **maximum residual of
78 ms**:

```
duration_s = 0.13696 * degrees + 0.1403      # speed 1: 7.30 °/s + 140 ms spin-up
```

It predicted the 380° move at 52.18 s against 52.158 s measured.

| speed | 90° duration | sustained rate | samples |
|---|---|---|---|
| 1 | 12.486 s | 7.21 °/s | 4 |
| 2 | 9.413 s | 9.56 °/s | 3 |
| 3 | 6.271 s | 14.35 °/s | 4 |

**Only speed 1 has a multi-angle ladder.** Speeds 2 and 3 were measured at 90° only, so
their intercepts are assumed equal to speed 1's:

```
slope_s_per_deg = {1: 0.13696, 2: 0.10303, 3: 0.06812}   # 2 and 3 are ASSUMED
intercept_s     = 0.1403
```

**Speed 3's assumed intercept now has live support.** A 45° move at speed 3 was measured
twice at 3.229 s and 3.228 s against a 3.205 s prediction (+24 ms), a second angle on that
speed's ladder. Speed 1 was re-confirmed at 90°: 12.466 s and 12.442 s against 12.467 s
predicted, inside 25 ms. Speed 2 still rests on a single 90° sample.

Useful because `OK` is deferred: a client is blind for the whole duration of a long move.
Use the model as a **watchdog**, not as a substitute for the notification — e.g. flag an
error if `2 × predicted + 1 s` elapses with no `OK`. The driver does exactly that
(`watchdog_seconds`).

Other timings the driver relies on:

| What | Value | Source |
|---|---|---|
| Reply to any command except a move | ≤ ~200 ms; driver waits 5 s | `get_status` 35–65 ms ([Connect sequence](#connect-sequence-used-by-the-app)); jog ack ~142 ms ([rotate](#rotate)) |
| Gap below which an ack can be lost | ~100 ms | [rotate](#rotate), [set_bright](#set_bright) |
| Spacing between the two cancels of a blind stop | 150 ms | Clears the ~100 ms window above |
| Brightness write pacing for reliable acks | ~50 ms | [set_bright](#set_bright) |

## State and recovery

**The position counter does not survive a power cycle — verified.** Parked at 122 with
brightness 42, unplugged, and re-read: both came back **0**. So zero is not a place on the
platter, it is *wherever the platter happened to be sitting when the device was switched
on*. The BLE address was unchanged across the cycle, so a cached address survives even
though the state does not. Nothing in the protocol reveals that a power cycle happened.

**It is an accumulator, not an encoder — verified.** With the device powered and reading
100, the platter was turned by hand and it still read 100. There is no feedback of any kind:
the counter integrates *commanded* motion, and cannot see the platter.

That makes `get_position` exactly as truthful as client-side dead reckoning, and it fails
the same way — silently. It cannot detect a hand-turned platter, a missed step, a stall, a
slipping subject, or an obstructed move; after any of those it will confidently report a
position the platter is not at, with no way to notice or recover. Its ±1° residual is
rounding inside that accumulation, not sensor resolution.

Two things still make it worth using over client-side tracking: it survives a reconnect
within a power session, and it integrates **jog** motion, whose duration a client cannot
time accurately from outside. But a rig that must *know* where the platter is has to verify
optically. Nothing in this protocol can.

Consequences for a client:

- Homing is **repeatable within a power session** and meaningless across one — and only as
  far as the motor is actually keeping up with what it is told.
- A rig that needs the same physical zero every session must establish it outside the
  protocol: align to a fiducial after power-on and keep the offset client-side, or find home
  optically from a reference frame. There is no `set_position` to fold the offset into the
  device ([get_position / get_bright](#get_position--get_bright)). Per ADR 0003 that offset
  is the consumer's, not the driver's: the driver reports the device counter as machine
  coordinates and does not home.
- Brightness resets to 0 as well, so the LED is off at power-on regardless of its last value.

## Hazards

- **Silent position errors.** The counter can't see the platter, so a hand-turn, stall, or
  obstruction leaves it wrong with no error, and a power cycle re-zeros it undetectably
  ([State and recovery](#state-and-recovery)).
- **The app's Left/Right labels are inverted** relative to the wire values
  ([rotate](#rotate)).
- **`mode` is not validated** by the firmware, so a bad value is silently accepted
  ([Response grammar](#response-grammar)).
- **Untried `run` frame counts wedge** until cancelled
  ([run / continue / ir_shutter](#run--continue--ir_shutter)).
- **Acks are dropped** under back-to-back writes, which desyncs a client that waits for
  every one ([Command/response pairing](#commandresponse-pairing)).

## Notes for a client

Design consequences of the above, in the order they'll bite. These were written for the
original standalone client; the driver follows them except where noted (its public API is
blocking, per ADR 0001, and it doesn't expose `run`).

- **Stack:** `bleak` + `asyncio`. Scan by name `Foldio360` or by service UUID
  `6e400001-b5a3-f393-e0a9-e50e24dcca9e`. Address characteristics by UUID, not by the
  handles in this document — handles are stable for this firmware but not guaranteed.
- **Enable notifications before the first write.** `start_notify` on the TX characteristic,
  then write commands.
- **Write with `response=False`** (`write_gatt_char(..., response=False)`), matching the app.
- **Chunk at 20 bytes** in the write helper. Only `set_signal` needs it today, but the
  ceiling is real.
- **Model the response channel as a queue, not a callback.** One outstanding command,
  `await` the next notification. Give `rotate`/`run` a timeout derived from
  [Timing](#timing) rather than a constant.
- **`rotate` should be awaitable to completion** (return when `OK` arrives) with a separate
  fire-and-forget variant, since `OK` genuinely means "done".
- **Expose `run` as an async iterator** yielding frame indices, so the caller can `await`
  their own capture between steps and the library sends `continue` on the next iteration.
  Terminate on index `0`.
- **Ask the device where it is.** `get_position` returns a 0–359 angle that survives
  reconnects — though *not* a power cycle, where it re-zeros to wherever the platter is
  sitting — and follows jog motion ([get_position / get_bright](#get_position--get_bright)).
  Do **not** dead-reckon from commanded angles — an earlier revision of this document
  recommended exactly that, before `get_position` was found, and it is strictly worse.
  Homing is `get_position` plus a rotate of the shorter signed difference; expect ±1° from
  the counter's resolution, and do not iterate to close that last degree or you will chase
  quantization noise.
- **Don't reimplement the app's quirks.** Pass `speed` through for every angle, drop the
  trailing `cancel`, and implement repeat as a loop.
- **Validate arguments in the client** — not because the firmware won't (it answers `FAIL`,
  [Response grammar](#response-grammar)) but because `FAIL` does not say which argument was
  at fault: `dir ∈ {CW, CCW}`, `speed ∈ 1..3`, `brightness ∈ 0..100`, `frames ≥ 1`,
  `deg ≥ 1` for fixed-angle moves.
- **Consider naming direction by the wire value, not the app's labels** (`CW`/`CCW`), and
  document that the app calls `CW` "Left". Silently inheriting the app's inversion will
  confuse anyone reading a trace alongside your code.

Minimal working capture, for reference: enable notifications → `set_bright(60)` →
`run(36,2)` → loop {await index, expose, send `continue`} until index `0`.

## Open questions

| Question | How to settle it |
|---|---|
| Does `cancel(1)` stop a fixed-angle `rotate` in progress? | Cancel partway through a long `rotate`, then read `get_position` |
| `set_signal` framing beyond the Canon match | Step the app's camera picker, diff the blobs |
| `rotate(dir,0,speed,1)` behavior | Send it — the app never will |
| `ir_shutter` arguments other than 0 | `2` is rejected; 1 and 3 untried |
| What `mode` ≥ 2 actually does | Accepted with `OK` ([Response grammar](#response-grammar)) but its effect is unobserved |
| `speed > 3`, `deg > 380` | `speed` 4 is rejected ([Response grammar](#response-grammar)); higher speeds and `deg > 380` untried |
| Which direction does `run` turn? | Not in the captures — watch the platter during a `run` |
| Does any `frames` outside 24/36/48 work? | Twelve values tried, all wedge ([run / continue / ir_shutter](#run--continue--ir_shutter)); the set matches the app's presets exactly, so probably not |
| Acknowledged writes (0x12) on RX | Untested; the characteristic advertises support |
| Is the ~500 ms settle after the CCCD write required? | Enable notifications and send `get_status` immediately |

## Manual check

`el-devices foldio` (planned). Not yet run against hardware with the `educelab-devices`
driver.

## Capture provenance

| | |
|---|---|
| Source | Android bug report → `FS/data/misc/bluetooth/logs/btsnoop_hci.log` |
| Captures | `bugreport.txt.1.txt` (first session), `bugreport.txt.2` (superset, both sessions) |
| Connection | one link, t = 53.8 s – 2230.0 s, ATT MTU 23 |
| Firmware | `V1.0.2_1712M02669` |
| Method | Timings measured between each ATT write and its notification; rotation rates are least-squares fits over the capture, not vendor figures. |

The reverse-engineering tools (`tools/parse_btsnoop.py`, `probe.py`) and the original
standalone client stay in the `foldio360` repository.
