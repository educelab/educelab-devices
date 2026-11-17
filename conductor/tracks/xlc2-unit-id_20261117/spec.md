# Specification: XLC2 Unit ID and Probing

**Track ID:** xlc2-unit-id_20261117
**Type:** Feature
**Created:** 2026-11-17
**Status:** Draft

## Summary

Give each XLC2 a unit ID stored in EEPROM and reported by the handshake, so a consumer can tell boards apart. Let the driver find XLC2s safely: `discover()` lists candidate ports without opening them, and a new `XLC2.probe()` handshakes only the ports that could be an XLC2. From GitHub issue #37.

## Context

`pgs-scan` is moving to a per-rig device list matched by model and serial number (educelab/pgs-scan#116 on GitLab). Every device it uses can be told apart that way except the XLC2:

- `XLC2.discover()` returns `[]`, because the USB descriptors don't identify an XLC2, and opening a port resets the board behind it, which could be the X-Carve's homed GRBL controller.
- The one board checked has a Holtek HT42B534 bridge (`04D9:B534`) with USB serial number `0000`, a placeholder that can't be rewritten. Other units may have a CH340 (`1A86`).
- The 2.0.0 handshake (`EduceLab XLC2,1.0,2.0.0`) doesn't include a unit ID.

The X-Controller (FTDI `0403:6001`) and MISHA (CP2102N `10C4:EA60`) are on other bridges, so an allowlist of XLC2 bridges never touches them.

## User Story

As a developer of a consumer such as `pgs-scan`, I want to find and open a specific XLC2 by its serial number, so that a rig's device list can name its light controller like every other device, without resetting unrelated boards.

## Acceptance Criteria

**Firmware 2.1.0**
- [ ] The handshake reply always has four fields: `EduceLab XLC2,<hw>,2.1.0,<id>`. The fourth field is empty when no ID is set.
- [ ] `<U,id>` writes the ID to EEPROM and replies `OK`. An ID that isn't 1–16 characters from `[A-Za-z0-9_-]` gets `ERR invalid command` and changes nothing.
- [ ] A magic byte marks a stored ID, so blank EEPROM (`0xFF`) reads as unset.

**Driver**
- [ ] `_parse_identity` accepts three or four fields. `MIN_FIRMWARE` stays at 2.0.0, and a 2.0.0 board opens with no serial number.
- [ ] `info.serial_number` is the unit ID read on open (`None` before open or when unset).
- [ ] `XLC2.set_unit_id(id)` validates the ID, writes it, and updates `info`.
- [ ] `discover()` opens no ports. It lists ports on the XLC2 bridge allowlist (`04D9:B534`, `1A86:7523`), with no serial number.
- [ ] `XLC2.probe()` handshakes allowlisted ports and returns `DeviceInfo`s with the unit ID and firmware filled in. It skips any port that also matches another driver's `PortMatch`, opens each port exclusively, probes in parallel, and skips silent ports, logging them at debug level.
- [ ] `XLC2(serial_number=...)` means the unit ID. With no port, `open()` probes and keeps the matching board's connection, so that board isn't reset twice. It raises `DeviceNotFound` if none match and `DeviceError` if several do. The USB-serial lookup is removed, and `port=` and `serial_number=` stay mutually exclusive.

**Serial transport**
- [ ] `SerialTransport` opens every port with `exclusive=True`, so it applies to MISHA and X-Carve too.

**CLI**
- [ ] With no `--port` or `--serial-number`, `el-devices xlc` probes. It checks the board if exactly one is found, and otherwise lists what it found and exits with an error.
- [ ] `--serial-number` takes the unit ID.
- [ ] `--set-id ID` writes the ID, and refuses unless exactly one board is targeted.

**Docs**
- [ ] ADR 0004 gains one sentence: a lab board whose USB bridge can't identify it reports a unit ID as an optional fourth handshake field.
- [ ] `docs/hardware/xlc2.md`: Identity, the 2.1.0 protocol, setting an ID, version history, and the hazard that probing resets any other CH340 board on the host.
- [ ] `docs/guide.md` notes that `probe()` opens ports and `discover()` does not.
- [ ] The `serial-drivers_20261104` spec is amended: the lab board identity carries an optional unit ID, and that track runs after this one.

## Dependencies

- Existing code: `lights/xlc.py`, `_serial.py`, `app/xlc.py`, `firmware/xlc2/`, `tests/fakes/xlc.py`, `tests/test_xlc.py`, `tests/test_cli_xlc.py`, `tests/test_serial_transport.py`.
- Runs before `serial-drivers_20261104`, which will absorb this into the shared lab board connection.

## Out of Scope

- A unit ID for MISHA, which is identified by its CP2102N's USB serial number.
- Probing at open time for any device other than the XLC2.
- `el-devices discover` calling `probe()`.
- A command to clear an ID, and enforcement of the `XLC2-NN` naming scheme.
- WCH bridges other than `1A86:7523`, until a unit shows one.

## Technical Notes

- Uploading through the bootloader doesn't erase EEPROM, so the ID should survive a reflash. Verify on hardware.
- The 2 s reset settle is paid once, because probes run in parallel.
- Hardware checks are batched into the final phase: flash 2.1.0 and set an ID; check that the ID survives a reflash and a power cycle; check that `probe()` with the X-Carve attached doesn't reset it; check exclusive open on macOS and Windows (Linux if a rig is available); record any CH340 unit's VID:PID.

---

_Generated by Conductor. Review and edit as needed._
