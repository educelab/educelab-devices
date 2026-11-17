# Implementation Plan: XLC2 Unit ID and Probing

**Track ID:** xlc2-unit-id_20261117
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-17
**Status:** [~] In Progress

## Overview

Firmware first, with the firmware fake updated alongside it so the driver can be built test-first against it. Then the driver's identity changes, then discovery and probing (with exclusive open in the shared transport), then the CLI and docs. Hardware checks are batched into the final phase.

**Phase checkpoints:** the maintainer waived sign-off between phases for this track (2026-11-17). Run phases 1–5 in sequence, with the full suite passing at each phase boundary. Approval is still needed for the ADR 0004 change (Task 5.1).

## Phase 1: Firmware 2.1.0

### Tasks

- [x] Task 1.1: Update the firmware fake (`tests/fakes/xlc.py`) so it can play 2.0.0 or 2.1.0: a four-field handshake with an empty or set ID, and `<U,id>` with its validation.
- [x] Task 1.2: Firmware sketch: EEPROM layout (a magic byte plus up to 16 ID characters), the four-field handshake, `<U,id>`, `SW_VERSION` 2.1.0, and the header comment.
- [x] Task 1.3: Compile the sketch on the host against stubbed Arduino libraries, as was done for 2.0.0, and check its replies match the fake's.
  - Done with clang++ and stubbed `EEPROM`/`DotStar`/`Serial` (not kept in the repo). All 20 probe commands matched, including `<U>` with no field, which first answered `ERR unknown command` and was fixed to `ERR invalid command`. A simulated reset kept the ID.

### Verification

- [x] The sketch compiles, and the fake's replies match the firmware's for the handshake and `<U>`.

## Phase 2: Driver Identity

### Tasks

- [x] Task 2.1: Tests: parse three- and four-field replies; `info.serial_number` is the unit ID after open, `None` when unset and on 2.0.0.
- [x] Task 2.2: `_parse_identity` returns `(hardware, firmware, unit_id)`; `info` reports the unit ID.
- [x] Task 2.3: Tests, then `XLC2.set_unit_id(id)`: validates locally, sends `<U,id>`, updates `info`. Raises if the board's firmware is older than 2.1.0.

### Verification

- [x] `pytest tests/test_xlc.py`, `ruff check .`, `ruff format --check .`.

## Phase 3: Discovery and Probing

### Tasks

- [x] Task 3.1: Tests, then `SerialTransport` opens with `exclusive=True`. Check that the MISHA and X-Carve tests still pass.
- [x] Task 3.2: Tests, then `discover()` lists ports on the XLC2 bridge allowlist (`04D9:B534`, `1A86:7523`) without opening them.
- [x] Task 3.3: Tests, then `XLC2.probe()`: allowlisted ports minus any that match another driver's `PortMatch`, probed in parallel, skipping ports that are busy or silent, and returning `DeviceInfo`s with the unit ID and firmware filled in.
- [x] Task 3.4: Tests, then `XLC2(serial_number=...)` as the unit ID: `open()` probes, keeps the matching board's connection, and raises `DeviceNotFound` if none match or `DeviceError` if several do. Remove the USB-serial lookup.

### Verification

- [x] Full suite: `pytest`, `ruff check .`, `ruff format --check .`.

## Phase 4: CLI

### Tasks

- [x] Task 4.1: Tests, then `el-devices xlc` with no target probes. It checks the board when exactly one is found, and otherwise lists what it found and exits with an error.
- [x] Task 4.2: `--serial-number` takes the unit ID; update the help text.
- [x] Task 4.3: Tests, then `--set-id ID`, which refuses unless exactly one board is targeted.

### Verification

- [x] `pytest tests/test_cli_xlc.py` and the full suite.

## Phase 5: Docs

### Tasks

- [x] Task 5.1: ADR 0004: one sentence on the optional unit ID field. Needs maintainer approval.
- [x] Task 5.2: `docs/hardware/xlc2.md`: Identity, the 2.1.0 protocol, setting an ID, version history, the CH340 probe hazard, and the Unknowns (the HT42B534 can't be rewritten).
- [x] Task 5.3: `docs/guide.md`: `probe()` opens ports and `discover()` does not.
- Deviation: the hardware tests (`tests/test_xlc_hardware.py`) were extended in Phase 5 rather than 6, so Phase 6 is only the runs. With no `XLC2_PORT` they probe; with `XLC2_UNIT_ID` set they store and reopen by ID.
- [x] Task 5.4: Amend the `serial-drivers_20261104` spec: the lab board identity carries an optional unit ID, and that track runs after this one.

### Verification

- [x] The docs build, and they agree with the code.

## Phase 6: Hardware Checks (batched)

### Tasks

- [ ] Task 6.1: Flash 2.1.0. Run `el-devices xlc --set-id XLC2-01`, then confirm the ID survives a reflash and a power cycle.
- [ ] Task 6.2: With the X-Carve attached and homed, run `probe()` and `el-devices xlc` with no target. The X-Carve must not reset.
- [ ] Task 6.3: Exclusive open: a second process can't open an XLC2, MISHA or X-Carve that's in use, on macOS and Windows, and Linux if a rig is available.
- [ ] Task 6.4: Run `pytest -m hardware tests/test_xlc_hardware.py`, extended for the ID. Record the results, and any CH340 unit's VID:PID, in `docs/hardware/xlc2.md`.

### Verification

- [ ] Results are recorded in the hardware doc's Manual check section.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing on CI (Ruff, pytest matrix)
- [ ] Documentation updated
- [ ] Ready for review; the PR closes #37

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
