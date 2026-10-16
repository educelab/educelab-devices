# Implementation Plan: Acroname USB Hub

**Track ID:** acroname-hub_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12
**Status:** [~] In Progress

## Overview

A small driver: fake brainstem seam, driver, then docs, CLI, and hardware check.

## Phase 1: Driver

### Tasks

- [x] Task 1.1: Declare the `[acroname]` extra; lazy import; define the internal brainstem seam and a fake.
- [x] Task 1.2: Write tests for discovery, port power, `power_cycle`, enumeration delay, and error mapping; implement.
- [x] Task 1.3: Run the `Hub` contract suite.

### Verification

- [x] Hub tests and contracts pass.
- [x] CI passes on every OS and Python version (draft PR #3).
  - Approved 2026-10-15.
  - Decided (2026-10-15): the enumeration delay only changes the running value. The driver never calls `system.save()` or `system.reset()`, because devices persist nothing. If the hardware check finds the delay doesn't take effect without a save, record that in the hardware doc and flag it; don't add a save step.

### Notes and deviations

- The spec names `power_cycle(port, off_seconds)`; the `Hub` role's parameter is
  `off_time`, and the driver keeps the role's signature.
- The `Hub` role has no settings API, so the enumeration delay is a pair of
  driver methods: `enumeration_delay() -> Setting` (seconds, step 0.1) and
  `set_enumeration_delay(seconds)`, which snaps to the nearest 100 ms. It sets
  the running hub only. `pgs-scan`'s `usb_hub3p_config.py` also called
  `system.save()` and `system.reset()`; the driver doesn't (see the decision
  above).
- `AcronameHub()` with no serial number opens the only hub attached and refuses
  to guess between several (as `MishaLight` does). Serial number 0 is rejected
  because the SDK reads it as "any hub".
- The driver connects with `discoverAndConnect(Spec.USB, serial)`: concrete
  stem classes override `connect()` with a different signature.
- What Phase 1 learned about the SDK and platforms is in
  `docs/hardware/acroname-usbhub3p.md` (Task 2.1).

## Phase 2: Docs, CLI, hardware verification

### Tasks

- [x] Task 2.1: Write `docs/hardware/acroname-usbhub3p.md`.
- [x] Task 2.2: Add the `el-devices hub` subcommand.
- [x] Task 2.3: Write and run `@pytest.mark.hardware` tests on a hub; record findings.
  - Tests written in `tests/test_acroname_hardware.py`. The maintainer ran them on 2026-10-16 (macOS, Python 3.13): all 7 passed. Results are in the hardware doc's Manual check.

### Verification

- [ ] Maintainer confirms port power-cycling on a hub.

## Phase 3: Port speed

The maintainer asked for "an interface for setting a port's speed": limiting a port's
USB data speed (e.g. USB 2 only, to cap camera bandwidth on a rig). Design approved
2026-10-16.

### Design

Acroname-only, on `AcronameHub`. The `Hub` role doesn't change: promote to the role
once a second hub driver needs it.

```python
class PortSpeed(StrEnum):          # in educelab.devices.hubs.acroname
    USB3 = "usb3"                  # SuperSpeed and Hi-Speed lines enabled (default)
    USB2 = "usb2"                  # Hi-Speed only: SuperSpeed lines disabled
    DATA_OFF = "data-off"          # no data lines: power only, for charging

hub.set_port_speed(port, PortSpeed.USB2)   # the fastest speed the port allows
hub.port_speed(port) -> PortSpeed
hub.connected_speed(port) -> PortSpeed | None   # what the attached device
                                                # enumerated at; None if nothing
```

- **Setting**: the legacy USB entity, as the driver already uses
  (`setHiSpeedDataEnable`/`Disable`, `setSuperSpeedDataEnable`/`Disable`). These
  change data lines only, not VBUS.
- **Reading**: `usb.getPortState(port)` bits 1 and 3 (USB 2 and USB 3 data enabled)
  give the limit at `open()`; bits 11 and 12 give the negotiated speed.
- **Rejected**: `setPortMode()` (per-device bit field, could clobber the power mode);
  the newer Port entity (firmware support unknown); a `Hub` role method; power calls
  that touch VBUS only.

### Decisions (approved 2026-10-16)

1. Three speeds: `USB2`, `USB3`, and `DATA_OFF` (charge only).
2. `set_port_speed()` doesn't re-enumerate; that's left to the caller, and the
   docstring points to `power_cycle(port)`.
3. `power_on()` keeps the port's limit, remembered while the hub is open and read
   from the hub at `open()`. A `DATA_OFF` port powers on with no data lines.
4. Names: `PortSpeed.USB2`, `PortSpeed.USB3`, `PortSpeed.DATA_OFF`.

### Tasks

- [x] Task 3.1: Extend the fake with the HS/SS data calls and the speed state bits; test
  and implement `PortSpeed`, `set_port_speed`, `port_speed`, `connected_speed`, and the
  `power_on()` interaction.
- [x] Task 3.2: Add `el-devices hub speed` and the speed columns in `status`.
- [x] Task 3.3: Document port speed in `docs/hardware/acroname-usbhub3p.md` (API,
  state bits, unknowns).
- [x] Task 3.4: Hardware tests on `ACRONAME_TEST_PORT` only, restoring its power and
  speed afterwards: the limit reads back and survives `power_off()`/`power_on()`,
  `power_cycle()`, and a reconnect; with `ACRONAME_USB3_DEVICE=1` (a USB 3 device on
  that port), `connected_speed()` is USB2 after limiting and power-cycling, USB3 after
  restoring, and `None` with `DATA_OFF`.
  - Written; not yet run on hardware. Dry-run on the fake SDK (real `brainstem`
    blocked) with the test port starting off, powered at USB3, USB2, and DATA_OFF:
    all 15 pass, only the test port is switched, and its state is restored.
  - Run on 2026-10-16 on port 7: 12 passed and 3 skipped. A second run with
    `ACRONAME_USB3_DEVICE=1` had 13 passed and 2 failed, but nothing was plugged
    into port 7, so the two connected-speed tests had no device to see. By hand on
    the empty port, the USB 2 limit read back without a cycle; connected speed is
    untested. Reworked the device tests: they first require a device at USB 3 on
    the test port and say to plug one in if there isn't. They test limits without a
    cycle, and test USB 3 restore without a cycle and reconnecting after a power
    cycle separately (30 s). On failure they print the raw port state. Re-run with
    a device pending.

### Verification

- [ ] Maintainer confirms a USB 3 device enumerates at USB 2 when limited, at USB 3
  after restoring, and not at all with data off.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
