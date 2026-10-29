# Hardware reference

Everything the lab knows about each piece of hardware: protocols, timings, quirks,
firmware versions, and what went wrong. One file per model, named `<model>.md` in
lowercase (e.g. `foldio360.md`, `misha.md`).

These docs are written to be implemented against. A driver change isn't done until its
hardware doc reflects what the change taught us (workflow.md).

## Index

| Model | Role | Driver | Doc |
|---|---|---|---|
| MISHA | Light | `lights.misha.MishaLight` | [misha.md](misha.md) |
| Foldio360 turntable and halo | Stage, Light | `Foldio360` (`educelab.devices.stages.foldio360`) | [foldio360.md](foldio360.md) |
| Nikon Z7 II (gphoto2) | File camera, Focus drive | `cameras.gphoto2.GPhoto2Camera` | [gphoto2-cameras.md](gphoto2-cameras.md) |
| XLC2 | Light | `lights.xlc.XLC2` | [xlc.md](xlc.md) |
| X-Carve (GRBL) | Stage | `stages.xcarve.XCarve` | not yet written |
| Spinnaker cameras | Frame camera | `SpinnakerCamera` (`educelab.devices.cameras.spinnaker`) | [spinnaker.md](spinnaker.md) |
| Acroname USBHub3p | Hub | `hubs.acroname.AcronameHub` | [acroname-usbhub3p.md](acroname-usbhub3p.md) |
| USB3 Vision cameras through Aravis or GenTL | Frame camera | `cameras.aravis.AravisCamera`, `cameras.gentl.GenTLCamera` | [usb3-vision.md](usb3-vision.md) |
| IDS U3-36P0XCP-M-GL | Frame camera | `AravisCamera` (`GenTLCamera` untested) | [ids-u3-36p0xcp.md](ids-u3-36p0xcp.md) |

Add a row when a doc is created, and link it from the Doc column.

## How sure we are

Mark every non-obvious fact with how we know it:

- **verified**: observed directly on real hardware. Say what was observed, on which unit
  or firmware, and when, e.g. "verified on firmware `V1.0.2_1712M02669`, 2026-10".
- **assumed**: believed but not tested, e.g. taken from vendor docs, another project's
  code, or extrapolated from a related measurement. Say where it came from.
- **unknown**: an open question that matters to a driver. Say what would settle it.

Drivers should treat **assumed** and **unknown** behavior as hazards: validate
defensively, use loose timeouts, and say so in a comment that links back here.

## Generalising

Don't record one unit's or one session's measurements as properties of the model. A
timing measured on one turntable, or a quirk seen on one firmware version, is a fact
about that unit or version, so record it that way. Promote it to a fact about the model
only when it has been seen on more than one unit, or the vendor documents it.

When a later observation contradicts the doc, fix the doc and note what changed. Don't
leave both versions for the reader to reconcile.

## What a hardware doc covers

Use the headings that apply:

1. **Identity**: model, vendor, firmware versions seen, how to read the version, and
   how the device shows up on the host (USB IDs, serial port names, BLE advertisement).
2. **Transport**: connection parameters (baud rate, BLE services, SDK version) and how
   to find the device.
3. **Protocol**: commands and responses, the framing, and what an error looks like.
   For lab firmware, this follows ADR 0004 (one `OK` or `ERR <reason>` line per
   command).
4. **Timing**: how long operations take, settle delays, and the timeouts the driver uses
   and why.
5. **State and recovery**: what survives a power cycle or reconnect, failure modes seen,
   and how to recover from each.
6. **Hazards**: anything that can damage hardware, lose data, or silently give a wrong
   answer.
7. **Manual check**: the `el-devices` subcommand for this device and the results of the
   last run on real hardware (date, firmware, unit, anything surprising).

Constants in driver code that encode hardware behavior (a timeout, a settle delay, a
magic byte) link to the section here that justifies them.
