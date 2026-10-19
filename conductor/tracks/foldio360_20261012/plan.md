# Implementation Plan: Foldio360 Turntable

**Track ID:** foldio360_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12
**Status:** [~] In Progress

## Overview

Bring the client and its tests in unchanged and green first, then adapt the public surface to the `Stage` and `Light` contracts, then docs and CLI, then hardware verification and archiving the old repo.

## Phase 1: Move in

### Tasks

- [x] Task 1.1: Declare the `[foldio360]` extra; copy the client's transport and protocol layers into the package with lazy `bleak` import.
  - Deviation: the client's errors map onto the package hierarchy (`ResponseTimeout` -> `DeviceTimeout`, `DeviceNotFoundError` -> `DeviceNotFound`, `NotConnectedError` -> `NotConnected`, `CommandRejected` -> `errors.CommandRejected`); `UnexpectedResponse` stays as a driver-local `DeviceError` subclass since it means lost sync, which no shared error covers.
  - Deviation: `run`/`continue`, `ir_shutter`, `set_signal`, and `KNOWN_GOOD_FRAMES` were not copied. The spec puts them out of scope and no later task would remove them. With `set_signal` gone, `NusTransport.send()` takes `str` only.
  - Deviation: non-int arguments to `rotate_by`, `move_to`, and `origin` raise `TypeError` rather than `ValueError`, matching the package's convention (and Ruff's TRY004).
  - Deviation: the extra is `bleak>=3.0.2` without the old `<4` cap, which `requirements.txt` called a guess; a cap in a library extra only blocks consumers.
  - The module lives at `educelab.devices.stages.foldio360` but isn't exported from `stages/__init__.py` until it is a `Stage` (Phase 2).
- [x] Task 1.2: Port `test_foldio360.py` and its scripted fake transport; get it green unchanged in behavior.
  - The fake lives in `tests/fakes/foldio360.py` as `FakeNusTransport`; tests are pytest functions in `tests/test_foldio360.py`.
  - Not ported: tests for `run`, `ir_shutter`, and `set_signal` (not copied, see 1.1), and the `cli.py` argument tests (`parse_direction`, `bounded_int`, parse-without-connecting), which move with the CLI in Task 4.2. `test_fail_is_an_error_response` used `ir_shutter(2)`; it now scripts FAIL for a `rotate`.
  - Added: `DeviceTimeout` from a real `NusTransport.read()`, a missing `bleak` naming the extra, importing the driver not importing `bleak`, and `stop()` sending both cancels.
- [x] Task 1.3: Copy `PROTOCOL.md` to `docs/hardware/foldio360.md` and restructure it to the hardware-doc outline without losing content or markers.
  - Sections map to the hardware-doc outline: Identity (advertisement), Transport (NUS, framing, connect sequence), Protocol (command reference, one subsection per command, response grammar, pairing, app quirks), Timing, State and recovery (the power-cycle reset and accumulator findings, split out of the old §2.6), Hazards, Notes for a client (old §7), Open questions (old §8), Manual check, Capture provenance. `§N` cross-references became heading links; driver comments cite headings, not numbers.
  - Added: a Timing table of the other constants the driver uses, the fragment/numeric settle windows under Response grammar, an ADR 0003 pointer under State and recovery, and an **unknown** for `cancel(1)` during a fixed-angle `rotate` (spec: "verify on hardware"; PROTOCOL.md didn't list it in §8, but no capture shows it).
  - Fixed a contradiction: the old §8 said `speed > 3` was untried, while §4 records `run(24,4)` and `rotate(CW,1,4,1)` answering `FAIL`. The open question now covers only speeds above 4 and `deg > 380`.

### Verification

- [x] Ported tests pass on all CI platforms.
  - Approved 2026-10-15, after CI on draft PR #2 passed on every OS and Python version.
  - Decided (2026-10-15): using the package's error types (`DeviceTimeout`, `DeviceNotFound`, `NotConnected`, `CommandRejected`) in place of the client's own is approved.
  - Decided (2026-10-15): positions are whole degrees. Move targets are rounded to the nearest degree, and arrival is accepted within ±1°. Document this in the hardware doc, the driver docstrings, and the Stage behavior notes where callers would notice it.

## Phase 2: Stage adaptation

### Tasks

- [x] Task 2.1: Write tests for the `"theta"` axis, `move_to` shortest path, literal `move_by`, default timeouts, and arrival verification; implement on the `Stage` hooks.
  - `Foldio360` is now the `Stage` itself (exported from `educelab.devices.stages`); the old client methods (`rotate`, `rotate_by`, `get_status`, `get_position`, `get_device_position`, `cancel`, the origin API) are gone, so Task 2.4's origin removal landed here. `get_brightness`/`set_brightness` stay as public, locked methods until the halo replaces them in Phase 3.
  - `timeout=None` is detected by overriding the base's private `_timeout()` to return a sentinel (-1, which no caller can pass), which the hooks turn into the per-leg watchdog. No change to the `Stage` ABC. (Until 2026-10-16 the sentinel was `inf`; main's guide now promises that `math.inf` waits indefinitely, so after the rebase onto main the sentinel moved and `inf` means no limit.)
  - Turns over 360° are split into legs of at most 360° (rotations over 380° are untried), each verified on arrival.
  - Speeds are `1 / slope` deg/s for levels 1–3 (≈7.30, 9.71, 14.68).
  - The fake transport now emulates the firmware (position counter, held moves, cancel behavior) instead of only replaying scripts; scripted replies still override it.
  - The jog/stop/position code went in with this task, since the hooks are abstract; Tasks 2.2 and 2.3 add their tests.
- [x] Task 2.2: Write tests for `jog`/`stop`, including `stop()` from another thread during a move and stray-reply draining; implement.
  - `stop()` takes the lock only if it is free. If so it cancels synchronously (a known jog by mode, otherwise both modes, as the old client's `stop()` did); if not, it sends the cancel fire-and-forget and counts the ack as a stray, and the waiting move settles the link and raises `PositionError`. Tests cover both possible cancel behaviors (the move's OK sent or not), since the real one is unknown.
  - Added `DeviceBusy` for moving or jogging during a jog, matching `SimulatedStage`; the firmware's behavior there is untested.
- [x] Task 2.3: Write tests for `position()` during a move (returns last known, doesn't block); implement.
  - When the device lock is free, `position()` asks the turntable (so it follows jogs and other clients' moves); when another thread holds it, it returns the cached value.
- [x] Task 2.4: Remove the origin API; add the device lock.
  - The origin API went with the old client methods in Task 2.1 (`test_no_origin_api`); the power-cycle reset is in the hardware doc's State and recovery, with the ADR 0003 pointer.
  - The lock is the `Device` base's: every `Stage` method is `@synchronized` except `stop()`/`position()`, and the halo methods are `@synchronized` too. Tests show a second thread's command waiting for a move.
- [x] Task 2.5: Run the `Stage` contract suite.
  - `TestFoldio360Contract` runs `StageContract` on the firmware-emulating fake with `position_tolerance` set to the 1° arrival tolerance. All pass; `test_move_outside_limits_raises` skips (a wrapping axis has no limits).
- [x] Task 2.6: Port `run(frames)` (with `KNOWN_GOOD_FRAMES`), `ir_shutter`, and `set_signal` back as Foldio360-only methods under the device lock, with `stop()` able to cancel a `run`; restore their original tests; mark them unverified in our use in the hardware doc.
  - Deviation: `run(frames, on_frame)` takes a per-frame callback instead of returning a generator. A generator would have to hold the device lock across `yield`s, which leaks the lock if the generator is abandoned and breaks if it's resumed on another thread. Returning `False` from `on_frame` replaces `break`, and an exception from it cancels the sequence. The steps run at the stage's speed (`set_speed`) rather than taking a `speed` argument.
  - `stop()` cancels a `run` like a move: `_motion` is the fixed mode for the whole sequence, including while `on_frame` runs. Moves and jogs from inside `on_frame` raise `DeviceBusy`.
  - `KNOWN_GOOD_FRAMES` is back, used only in the timeout message; frame counts are still passed through. `NusTransport.send()` and the driver's `_command()` take bytes again for `set_signal`.
  - Ported tests: frame validation, `set_signal` type check, the sequence and its `continue` after the last frame, abort cancels, `ir_shutter` echo inside a run, FAIL for `ir_shutter(2)`, `set_signal` sent as raw bytes, and a run timeout cancelling once. Added: a 24-frame run against the emulating fake, `on_frame` raising, `ir_shutter` without a signal, 20-byte chunking of a long write, `stop()` from another thread during a run, and moves refused inside a run.
  - Decided (2026-10-15, maintainer): port these commands after all, as Foldio360-only methods outside `Stage`. They won't be used yet, but they keep the reverse-engineered behavior available. Supersedes the Task 1.1 deviation that left them out; spec updated to match.

### Verification

- [x] All Stage tests and contracts pass.
  - Approved 2026-10-15, after CI on PR #2 passed all 14 jobs.
  - Decided (2026-10-15): `run(frames, on_frame)` keeps its callback shape.
  - Decided (2026-10-15): `stop()` with nothing moving keeps sending both cancels, because it covers motion started by another process.
  - Decided (2026-10-15): whether `discover()` does a real BLE scan is decided in Phase 4, together with the CLI.

## Phase 3: Halo light

### Tasks

- [x] Task 3.1: Write tests for `foldio.halo` (`"halo"` channel, level mapping to 0–100, paced writes, ack draining); implement.
  - `Foldio360Halo` (in `stages/foldio360.py`) is created by the turntable and reached as `Foldio360.halo`. It shares the turntable's lock (so a halo command waits for a move, and works inside `run`'s `on_frame`) and its connection: `is_open`, `open()`, and `close()` delegate to the turntable, so opening or closing either opens or closes both. These are overrides in the driver; the `Device` base is unchanged.
  - Levels go out as `round(level * 100)`; `level()` reads `get_bright`, so it reports the hardware, not the last value set.
  - Ack handling: writes are paced at least 50 ms apart (`_BRIGHT_PACING_S`); if an ack still doesn't come within 0.5 s, the level is read back and accepted if it landed, else `DeviceTimeout`. The lost ack is counted as a stray and drained before the next command.
  - Removed the turntable's public `get_brightness`/`set_brightness` and the `wait=False` fire-and-forget path; pacing replaces it for ramps.
  - `docs/guide.md` §1.4 now says a driver may offer a role as a second object sharing the connection (the halo); `CONTEXT.md` is unchanged.
- [x] Task 3.2: Run the `Light` contract suite against the halo.
  - `TestFoldio360HaloContract` runs `LightContract` (and its `DeviceContract` lifecycle tests) on `Foldio360(transport=FakeNusTransport()).halo`. All pass.

### Verification

- [x] Halo tests and contracts pass.
  - Approved 2026-10-15, after CI on PR #2 passed all 14 jobs.
  - Decided (2026-10-15): change the halo lifecycle. `halo.open()` on a closed turntable opens it and remembers that it did; `halo.close()` then closes the turntable only in that case, and otherwise closes just the halo, whose calls raise `NotConnected` until it is reopened. Closing the turntable always makes the halo unusable. Tests, the Light contract wiring, the hardware doc, and guide §1 follow.
  - Decided (2026-10-15): `CONTEXT.md` stays unchanged; "a device offers one or more roles" still holds.

## Phase 4: Docs and CLI

### Tasks

- [x] Task 4.1: Fold the relevant `CLIENT_GUIDE.md` content into `docs/hardware/foldio360.md` and `docs/guide.md`.
  - Into the hardware doc: a Transport > Connecting subsection (scan vs. direct-connect times, caching the address with a short `connect_timeout` and scan fallback, the macOS empty-scan ambiguity), two Timing rows (scan window, `get_position` round trip), consumer rules under State and recovery (don't chase the last degree, a position shows a move was commanded, don't guess at power cycles, when a cached position is sound), a Hazards item for jogs outliving their process, and Driver behavior items for errors, the transport seam, and offering `KNOWN_GOOD_FRAMES` in a UI.
  - Into `docs/guide.md`: a §8 row for not guessing at state the hardware can't report.
  - Left out as already covered by the guide or superseded by the `Stage` API: lifecycle, the `AUTO` timeout convention, the origin API, `rotate`/`cancel`/`jogging`, `wait=False` brightness, the generator form of `run`, and the "no lock" concurrency rule (the driver now has one).
  - Deviation: `CLIENT_GUIDE.md` §2 notes that bleak's own exceptions escaped `connect()` unwrapped. The guide promises a `DeviceError` for hardware and connection problems, so the transport now wraps them: a connection timeout becomes `DeviceTimeout`, any other connection failure `DeviceNotFound`, and a failed write `NotConnected`. Writing the tests exposed a real bug: on Python 3.11+, `concurrent.futures.TimeoutError` is the builtin `TimeoutError`, so `_LoopThread.call()` mistook a coroutine's own timeout (e.g. bleak's connect timeout) for its wait expiring and crashed formatting `None`. Fixed.
- [x] Task 4.2: Add the `el-devices foldio` subcommand.
  - `el-devices foldio [--address A] [--scan-timeout S] ACTION`, where ACTION is `scan`, `status`, `rotate DIR DEGREES [-s 1-3]` (literal `move_by`), `goto ANGLE [-s 1-3]` (`move_to`), `halo LEVEL` (0–1, like `el-devices misha --level`), or `stop`. `scan` and `stop` go beyond the spec's list: `scan` is how to find an address now that nothing caches one, and `stop` clears a jog left by another program. Ctrl-C during a move stops the turntable. No address caching (spec).
  - Registered as `"foldio"` in `SUBCOMMANDS` and as the `foldio360` backend in `BACKENDS`, both alphabetical. The module imports the driver only inside its handlers, so the CLI still starts without it.
  - Proposed for the maintainer (Phase 2 left this to Phase 4): `Foldio360.discover()` now does a real BLE scan (8 s), so `el-devices discover` lists turntables. A failed scan raises `DeviceError`, and without `bleak` it raises the `ImportError` that `discover` reports as "unavailable". The contract test's call is kept off the radio by patching the driver's bleak loader. Easy to revert to `[]` if the 8 s in `el-devices discover` is unwelcome.
  - Restored the original CLI argument tests (`parse_direction` refusing Left/Right, `bounded_int`, parse-without-connecting, bad arguments as usage errors before any connect) in `tests/test_cli_foldio.py`, plus tests for each action against the fake. `fake_bleak()` in `tests/fakes/foldio360.py` stands in for bleak's scanner and client.

### Verification

- [x] Docs reviewed by maintainer.
  - Approved 2026-10-15, after the docs review and CI on PR #2 passing all 14 jobs; everything in the phase is accepted.
  - Decided (2026-10-15): `discover()` keeps the real 8 s BLE scan.
  - Decided (2026-10-15): `el-devices foldio halo` takes a level from 0 to 1.
  - Decided (2026-10-15): the bleak error wrapping and the `_LoopThread` fix are accepted.

## Phase 5: Hardware verification and archive

### Tasks

- [x] Task 5.1: Write `@pytest.mark.hardware` tests (connect, status, move_to, move_by, stop mid-move, halo levels).
  - `tests/test_foldio360_hardware.py` (`pytestmark = pytest.mark.hardware`): identity and firmware; whole-degree position; `move_to` arrival and short way (timed); `move_by` there and back, and literal past a full turn (timed, at speed 3); each speed's 90° time against the timing model (settles whether speeds 2 and 3 hold); `stop()` from another thread mid-move, checking the platter stays put afterwards (settles the open `cancel(1)` question); jog then stop; `run(24)` for three frames, printing step signs (settles which way `run` turns); `ir_shutter` without a signal answering FAIL; halo levels reading back, and a fast ramp landing its last level.
  - Connects to `FOLDIO360_ADDRESS` if set, else scans. Skips cleanly without `bleak`, when a scan finds no turntable, or when the given address doesn't connect (all three checked locally: 18 skipped). Measurements print with `-s`, for the hardware doc.
- [x] Task 5.2: Run them and `el-devices foldio` on the turntable; verify cancel-during-move behavior and record it in the hardware doc.
  - Blocked on hardware (2026-10-15): the turntable isn't available. The maintainer runs the steps in the Phase 5 report when it is.
  - First findings (maintainer, 2026-10-16): the wire value `CW` turns the platter clockwise. Recorded in the hardware doc's rotate table; the driver docstring and `el-devices foldio rotate --help` now say so. It doesn't settle which way `run` turns (`run` takes no direction), so that question stays open.
  - The maintainer expects `stop()` to leave the halo alone. Checked: no stop path (idle, mid-move, jog, or `el-devices foldio stop`) sends `set_bright`, and nothing on record says the firmware's `cancel` touches the halo. Pinned with offline tests (idle, mid-move, jog, CLI) and a hardware test, `test_stop_leaves_the_halo_alone`, which checks the firmware side. Stated in the hardware doc, the `Foldio360` docstring, and the `stop` help.
  - Hardware run (maintainer, 2026-10-16; log at `/Users/seth/hw-logs/foldio.log`): all 18 hardware tests passed in 101.7 s on macOS, Python 3.13.15, firmware `V1.0.2_1712M02669`. Recorded in the hardware doc:
    - On macOS the address is a per-host CoreBluetooth UUID (`E8A05472-C1C6-BF86-D37F-32C827F0F04A`), not a MAC; documented in Identity and in the `--address`, `FOLDIO360_ADDRESS`, and `Foldio360(address=)` help.
    - `cancel(1)` stops a `rotate` in progress (21° of 180° after 3.0 s, still two seconds later). Open question settled; whether the cancelled `rotate` sends its own OK stays open.
    - `run(24)` steps +15° each, so `run` turns clockwise. Open question settled.
    - 90° at speeds 1/2/3 measured 12.707/9.701/6.573 s against 12.467/9.413/6.271 s predicted, 0.24–0.30 s over. Recommendation: keep the model and the timeouts. The excess matches the two counter reads around each `move_by` call (start position and arrival check), which the hardware test's timer includes and the watchdog doesn't time. The watchdog leaves seconds of margin either way. `move_to` 20° took 3.06 s, `move_by` 380° at speed 3 took 26.70 s, and a 2 s jog at speed 1 covered 15°.
    - Halo levels 1.0/0.5/0.25/0.0 read back; the paced 21-step ramp landed its last level.
  - `test_stop_leaves_the_halo_alone`, added after this run, passed on hardware on 2026-10-19.
- [ ] Task 5.3: With the maintainer, add a README pointer to `educelab-devices` in `~/source/foldio360` and archive it.

### Verification

- [ ] Maintainer confirms turntable and halo behave as documented.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
