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
  - `timeout=None` is detected by overriding the base's private `_timeout()` to return `inf`, which the hooks turn into the per-leg watchdog. No change to the `Stage` ABC.
  - Turns over 360° are split into legs of at most 360° (rotations over 380° are untried), each verified on arrival.
  - Speeds are `1 / slope` deg/s for levels 1–3 (≈7.30, 9.71, 14.68).
  - The fake transport now emulates the firmware (position counter, held moves, cancel behavior) instead of only replaying scripts; scripted replies still override it.
  - The jog/stop/position code went in with this task, since the hooks are abstract; Tasks 2.2 and 2.3 add their tests.
- [ ] Task 2.2: Write tests for `jog`/`stop`, including `stop()` from another thread during a move and stray-reply draining; implement.
- [ ] Task 2.3: Write tests for `position()` during a move (returns last known, doesn't block); implement.
- [ ] Task 2.4: Remove the origin API; add the device lock.
- [ ] Task 2.5: Run the `Stage` contract suite.
- [ ] Task 2.6: Port `run(frames)` (with `KNOWN_GOOD_FRAMES`), `ir_shutter`, and `set_signal` back as Foldio360-only methods under the device lock, with `stop()` able to cancel a `run`; restore their original tests; mark them unverified in our use in the hardware doc.
  - Decided (2026-10-15, maintainer): port these commands after all, as Foldio360-only methods outside `Stage`. They won't be used yet, but they keep the reverse-engineered behavior available. Supersedes the Task 1.1 deviation that left them out; spec updated to match.

### Verification

- [ ] All Stage tests and contracts pass.

## Phase 3: Halo light

### Tasks

- [ ] Task 3.1: Write tests for `foldio.halo` (`"halo"` channel, level mapping to 0–100, paced writes, ack draining); implement.
- [ ] Task 3.2: Run the `Light` contract suite against the halo.

### Verification

- [ ] Halo tests and contracts pass.

## Phase 4: Docs and CLI

### Tasks

- [ ] Task 4.1: Fold the relevant `CLIENT_GUIDE.md` content into `docs/hardware/foldio360.md` and `docs/guide.md`.
- [ ] Task 4.2: Add the `el-devices foldio` subcommand.

### Verification

- [ ] Docs reviewed by maintainer.

## Phase 5: Hardware verification and archive

### Tasks

- [ ] Task 5.1: Write `@pytest.mark.hardware` tests (connect, status, move_to, move_by, stop mid-move, halo levels).
- [ ] Task 5.2: Run them and `el-devices foldio` on the turntable; verify cancel-during-move behavior and record it in the hardware doc.
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
