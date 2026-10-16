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
- [~] Task 2.3: Write and run `@pytest.mark.hardware` tests on a hub; record findings.
  - Tests written in `tests/test_acroname_hardware.py`; the maintainer runs them on a hub.

### Verification

- [ ] Maintainer confirms port power-cycling on a hub.

## Phase 3: Port speed (proposed, awaiting approval)

The maintainer asked for "an interface for setting a port's speed", read as limiting a
port's USB data speed (e.g. USB 2 only, to cap camera bandwidth on a rig). Not started.

### Proposed interface

Acroname-only, on `AcronameHub`. The `Hub` role doesn't change: promote to the role
once a second hub driver needs it.

```python
class PortSpeed(StrEnum):          # in educelab.devices.hubs.acroname
    USB2 = "usb2"                  # Hi-Speed only: SuperSpeed lines disabled
    USB3 = "usb3"                  # SuperSpeed and Hi-Speed lines enabled (default)

hub.set_port_speed(port, PortSpeed.USB2)   # the fastest speed the port allows
hub.port_speed(port) -> PortSpeed          # read from the hub, not remembered
hub.connected_speed(port) -> PortSpeed | None   # what the attached device
                                                # enumerated at; None if nothing
```

- **Setting**: legacy USB entity, as the driver already uses: `usb.setSuperSpeedDataDisable(port)`
  for USB2 and `usb.setSuperSpeedDataEnable(port)` plus `usb.setHiSpeedDataEnable(port)`
  for USB3. These change data lines only, not VBUS.
- **Reading the limit**: `usb.getPortState(port)` bits 1 (USB 2 data enabled) and 3
  (USB 3 data enabled). Hi-Speed lines disabled raises `DeviceError` naming the state,
  since neither speed describes it.
- **Reading the negotiated speed**: port state bits 11 (USB 2 speed) and 12 (USB 3 speed),
  with bit 23 (device attached).
- **Power interaction**: `power_on()` currently calls `setPortEnable`, which re-enables
  every data line and would silently undo a USB2 limit. Proposed: the driver remembers
  each port's limit for the session (initialized from the hub at `open()`), and
  `power_on()` on a USB2-limited port enables VBUS and Hi-Speed only. `power_off()` still
  cuts power and data.
- **Re-enumeration**: `set_port_speed()` doesn't power-cycle. If the hub check shows a
  device attached at USB 3 doesn't drop to USB 2 by itself, the docstring says to call
  `power_cycle(port)` afterwards (see decision 2).
- **Persistence**: never saved, per the Phase 1 decision. Whether limits survive a hub
  reset or power loss is recorded in the hardware doc.
- **CLI**: `el-devices hub speed PORT [usb2|usb3]` shows the port's limit and connected
  speed, or sets the limit. `status` adds both to each port's line.
- **Errors**: as now; an unknown port is `KeyError`, an unknown speed `ValueError`.

### Options considered

- **`usb.setPortMode()` with a bit field**: one call, but the bits are per-device and
  only partly documented for the USBHub3p (bit 0 power, 1 HS, 3 SS, 16–18 power
  mode). A read-modify-write could clobber the power mode. Rejected.
- **The newer Port entity** (`stem.hub.port[i].setDataSSEnabled(bool)`,
  `getDataSpeed()`): richer readback (1.5M/12M/480M/5G/10G bits), but which firmware
  supports it on the USBHub3p is unknown. Keep it as a fallback if the legacy state bits
  turn out not to report speed.
- **A `Hub` role method**: needs a second driver, and would land on `main` first.
  Not recommended.
- **Power calls touching VBUS only** (`setPowerEnable`/`setPowerDisable`), leaving data
  lines alone: no memory needed, but power-cycling would no longer cut data, which
  changes Phase 1 behavior for self-powered devices. Not recommended.

### Decisions for the maintainer

1. Two speeds (`USB2`, `USB3`), or also a data-off option (charge only)?
2. Should `set_port_speed()` re-enumerate the device itself (cycle the port's data
   lines, or power), or leave that to the caller?
3. Keep `power_on()` honoring the limit through session memory (recommended), or
   accept that `power_on()` restores USB 3?
4. Enum values named `USB2`/`USB3` (recommended) or `HIGH_SPEED`/`SUPERSPEED`?

### Tasks (once approved)

- [ ] Task 3.1: Extend the fake with the HS/SS data calls and the speed state bits; test
  and implement `PortSpeed`, `set_port_speed`, `port_speed`, `connected_speed`, and the
  `power_on()` interaction.
- [ ] Task 3.2: Add `el-devices hub speed` and the speed columns in `status`.
- [ ] Task 3.3: Document port speed in `docs/hardware/acroname-usbhub3p.md` (API,
  state bits, unknowns).
- [ ] Task 3.4: Hardware tests on `ACRONAME_TEST_PORT`: the limit reads back; it survives
  `power_off()`/`power_on()`, `power_cycle()`, and a reconnect; with
  `ACRONAME_USB3_DEVICE=1` (a USB 3 device on that port), `connected_speed()` is USB2
  after limiting and power-cycling and USB3 after restoring. Manual check: whether
  limiting takes effect without a power cycle, and whether limits survive a hub reset.

### Verification

- [ ] Maintainer confirms a USB 3 device enumerates at USB 2 when limited, and at USB 3
  after restoring.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
