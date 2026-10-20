# Implementation Plan: MISHA Lights

**Track ID:** misha-lights_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12
**Status:** [~] In Progress

## Overview

Build the shared serial transport and its fake first, then the MISHA driver against the fake, then firmware, docs, and CLI, and finish on real hardware.

## Phase 1: Shared serial transport

### Tasks

- [x] Task 1.1: Declare the `[serial]` extra; lazy `pyserial` import with a missing-extra error.
- [x] Task 1.2: Write tests for port matching (all criteria must match; no criteria matches nothing); implement.
- [x] Task 1.3: Implement open (with DTR-reset settle), line read/write with timeouts, and close.
- [x] Task 1.4: Implement the scripted fake serial transport (expected writes → canned replies) for driver tests.

### Verification

- [ ] Transport tests pass on all CI platforms without `pyserial` hardware.

## Phase 2: Firmware update

### Tasks

- [x] Task 2.1: Copy the current firmware into `firmware/misha/` unchanged (provenance commit).
  - Deviation: the sketch is in `firmware/misha/MISHA/`, since Arduino tools only build a sketch whose folder matches its `.ino` name. The README stays in `firmware/misha/`, as in the source repo.
- [x] Task 2.2: Add `OK`/`ERR <reason>` replies to every command, `ERR` for unknown wavelengths, and bump the SW version.
- [x] Task 2.3: Document the protocol change in the firmware README.
- [x] Task 2.4: Rework switching (maintainer review, 2026-10-13): add `STATE` (`OK <nm>,<pct>`, amending ADR 0004) and `OFF`; changing the lit band's level only changes brightness; `<nm>,0` turns off only that band. Still 2.0.0, since no board has it yet.

### Verification

- [x] Firmware compiles; maintainer reviews the protocol change.
  - Compiles for the Nano (ATmega328P) with arduino:avr 1.8.8, no sketch warnings: 10708 B flash (34%), 793 B RAM (38%); 1.0.0 was 11574 B / 740 B. Protocol approved 2026-10-13.

## Phase 3: MISHA driver

### Tasks

- [x] Task 3.1: Write handshake tests (accepted name, wrong name, no reply, firmware too old); implement `open()`.
- [x] Task 3.2: Write tests for `set_level`, exclusive-channel state, `all_off`, waiting for `OK`, `ERR` → `CommandRejected`, and wavelength validation; implement.
- [x] Task 3.3: Implement `discover()`.
- [x] Task 3.4: Run the `Light` contract suite against `MishaLight` on the fake transport.
- [x] Task 3.5: Read the panel's state with `STATE` on open instead of turning it off; use `OFF` for `all_off()` and `close()`.
- [x] Task 3.6: Make `port` optional: with none, `open()` finds the panel itself, resetting each board it probes only once.
- [x] Task 3.7: Find panels without opening ports (maintainer review, 2026-10-13). Opening a port resets the Arduino behind it, so probing could reset an X-Carve and lose its homing, and it contradicted `Device.discover()` ("without opening them"). `discover()` and a port-less `open()` now match USB descriptors only: the panel's CP2102N bridge (`10C4:EA60`) with a serial number written to contain `MISHA`. `serial_number=` picks a unit; unnamed units need `port=` or `serial_number=`. ID probing is removed.

### Verification

- [x] All MISHA tests and contracts pass.
  - 428 tests pass with and without pyserial installed (after Tasks 3.5 and 3.6).

## Phase 4: Docs and CLI

### Tasks

- [x] Task 4.1: Write `docs/hardware/misha.md` (protocol including the firmware version history, timing, quirks, unknowns).
  - Include naming a panel: write a `MISHA-<8 hex>` USB serial with Xpress Configurator in Simplicity Studio 5. Studio 6 doesn't support CP210x devices or Xpress Configurator (Silicon Labs community answer, 2026-10-13). Keep the VID/PID; never set the lock. Naming is optional, since `serial_number=` takes the factory serial.
- [x] Task 4.2: Add the `el-devices misha` subcommand.

### Verification

- [x] Docs reviewed by maintainer.
  - Approved 2026-10-14, after the doc gained the measured exposure limits and timing, and `el-devices misha` moved to a 25% default.
  - Decided (2026-10-13): keep the ~60 Hz SoftPWM. Dimming flickers and the drivers ramp over ~1.8 ms, so the doc recommends running bands at 100% and setting exposure with the camera; Phase 5 measurements confirm or revise it. Driver cooling is out of scope.

## Phase 5: Hardware verification

### Tasks

- [x] Task 5.1: Flash the updated firmware onto the MISHA panels.
  - Done: `MISHA-2108d7a8` and `MISHA-e6a00ad8` both report firmware 2.0.0, hardware 1.1 (2026-10-20).
- [x] Task 5.2: Write `@pytest.mark.hardware` tests (handshake, each channel on/off, all off, `ERR` on an unknown wavelength).
- [x] Task 5.3: Run `el-devices misha` and the hardware tests on the panels.
  - Done (2026-10-20): `el-devices misha` clean on `MISHA-2108d7a8`; hardware tests 25/25 on each panel.
- [x] Task 5.4: Record results (firmware/hardware versions, timing) in `docs/hardware/misha.md`.
  - Done (2026-10-20): Manual check, Last run.
  - Done (2026-10-14): flicker and brightness against level, measured with a camera on firmware 1.0.0; the doc's exposure limits replace the assumed PWM behavior.
  - Done (2026-10-14): boot and reply timing measured on `MISHA-e6a00ad8`, firmware 2.0.0.

### Verification

- [ ] Maintainer confirms the panels behave as documented.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
