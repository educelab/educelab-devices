# Acroname USBHub3p

An 8-port USB 3 hub from Acroname whose downstream ports can be switched on and off
from the host. The lab uses it to power-cycle hung cameras without unplugging them.
It's controlled through Acroname's BrainStem SDK (the `brainstem` package), which wraps
a native library.

Driver: `educelab.devices.hubs.acroname.AcronameHub`. Manual check: `el-devices hub`.

Facts below come from the BrainStem SDK 2.12.5 source and docstrings, and from
`pgs-scan`'s `utils/usb_hub3p_config.py` and `utils/usb_hub3p_reset.py` (branch `dev`),
unless marked **verified**. Nothing here has been checked against a hub by this
package yet.

## Identity

- **Model**: the SDK class is `brainstem.stem.USBHub3p`, and discovery reports model
  number `brainstem.defs.MODEL_USBHUB_3P` (19). The driver reports the model as
  `USBHub3p`.
- **Serial number**: a 32-bit integer, conventionally written in hex. The driver
  writes it as `0x` and 8 hex digits, e.g. `0x1234ABCD`, and accepts that form or the
  bare hex digits. `pgs-scan` logged it the same way.
- **Firmware version**: `system.getVersion()` returns a packed number, which
  `brainstem.version.unpack_version()` splits into major, minor, and patch. The driver
  reports it as `major.minor.patch`. Versions seen: none yet.
- **On the host**: **unknown**. Acroname's USB vendor ID is `24FF` (**assumed**, from
  Acroname's udev rules); the hub's product ID, and whether the BrainStem link needs
  the hub's separate control port connected or also works over the upstream port,
  are unknown. `lsusb` (Linux) or `system_profiler SPUSBDataType` (macOS) with the hub
  attached would settle both.

## Transport

- **SDK**: `brainstem` from PyPI, installed with
  `pip install 'educelab-devices[acroname]'`. Written against 2.12.5.
- **Discovery**: `brainstem.discover.findAllModules(Spec.USB)` lists every BrainStem
  module on USB. The driver keeps those whose model is `MODEL_USBHUB_3P`. Discovery
  doesn't connect.
- **Connecting**: `stem.discoverAndConnect(Spec.USB, serial_number)`. The concrete stem
  classes override `connect()` with a different signature, so the driver doesn't use
  it. The SDK reads serial number 0 as "the first module found", so the driver
  rejects 0.
- **Choosing a hub**: `AcronameHub()` with no serial number opens the only hub
  attached and raises `DeviceNotFound`, listing the serial numbers, when there are
  several. Once it has found a hub, reopening uses that hub's serial number.

### Platform support

`import brainstem` loads a native library bundled in the wheel, and fails at import
time if it can't:

- **Linux**: the SDK tries a fixed list of bundled builds: Ubuntu 24.04, 22.04, 20.04,
  and 18.04 (x86_64 and aarch64), Debian 12 (x86_64), and Debian 11.7 (armv7l). If
  none loads it raises `ImportError: This Linux platform/architecture is not
  supported`. Other distributions work only if one of those builds happens to load.
- **Windows**: the library needs the Microsoft Visual C++ 2015 (or later)
  redistributable for the matching architecture. Without it, the import raises
  `OSError`, and the driver re-raises it as an `ImportError` that points here.
  Download it from Microsoft
  (<https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist>); a
  restart is usually needed afterwards.
- **macOS**: one universal library. **Verified** to import on macOS with Python 3.13,
  2026-10-15 (no hub attached).

Importing `educelab.devices.hubs` never imports `brainstem`. The SDK is loaded on the
first `discover()` or `open()`, so these failures only affect this backend.

### Linux permissions

Without a udev rule, only root can open the hub. Acroname's SDK download ships a
rule like the one below (**assumed**; the PyPI wheel doesn't include it). Save it as
`/etc/udev/rules.d/99-brainstem.rules`, then run
`sudo udevadm control --reload-rules && sudo udevadm trigger` and replug the hub:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="24ff", MODE="0666"
```

How a missing rule shows up is **unknown**. The driver says to check udev rules
when the SDK returns its permission error (12), but the SDK may instead report the
hub as not found.

## Protocol

Every SDK command returns an integer error code, and every getter returns a `Result`
with `.error` and `.value`. Code 0 means success. The driver maps the others:

| Code | SDK name | Raised as |
|---|---|---|
| 3 | `NOT_FOUND` | `DeviceNotFound` |
| 5 | `BUSY` | `DeviceBusy` |
| 18 | `TIMEOUT` | `DeviceTimeout` |
| 25 | `CONNECTION_ERROR` | `NotConnected` |
| 2, 7, 13, 21, 29 | parameter, mode, range, unimplemented, invalid option | `CommandRejected` |
| 12 | `PERMISSION_ERROR` | `DeviceError`, mentioning udev rules |
| anything else | | `DeviceError` |

Messages give the hub's serial number, what failed, the SDK's description of the
code, and the code itself.

**Verified** with SDK 2.12.5 on macOS with no hub attached, 2026-10-15:
`discoverAndConnect()` with an absent serial number returns 3; commands on a stem that
isn't connected return 25; `disconnect()` on a stem that isn't connected returns 0;
`findAllModules(Spec.USB)` returns `[]`.

### Ports

- **Numbering**: 8 downstream ports, 0 to 7 (`USBHub3p.NUMBER_OF_DOWNSTREAM_USB`).
  `pgs-scan`'s reset script cycled `range(0, 8)`.
- **Switching**: `usb.setPortDisable(port)` turns off both power and data, and
  `usb.setPortEnable(port)` turns both on. The SDK also has power-only and data-only
  calls; the driver doesn't use them.
- **Reading**: `usb.getPortState(port)` returns a bit field. The driver reads bit 0
  (`aUSBHUB3P_USB_VBUS_ENABLED`) as "powered". Other bits include USB 2 data enabled
  (1), USB 3 data enabled (3), error flag (19), and device attached (23).

### Enumeration delay

`usb.getEnumerationDelay()` and `usb.setEnumerationDelay(ms)` read and set how long
the hub waits between enumerating its ports. The SDK says the value is in 100 ms
increments. The driver exposes it as `enumeration_delay()` (a `Setting` in seconds,
step 0.1) and `set_enumeration_delay(seconds)`, which rounds to the nearest 100 ms.

The driver only changes the running value. `pgs-scan`'s config script followed the
set with `system.save()` (write to the hub's flash) and `system.reset()`. The driver
doesn't do either, because devices persist nothing. **Unknown**: whether the new
value takes effect without a save and reset, and what the maximum is.

## Timing

- **Power-cycle off time**: the `Hub` default of 1 s, the same as `pgs-scan`'s reset
  script. It's a convention, not a measured requirement; a device that holds charge
  may need longer.
- **Re-enumeration**: how long an attached device takes to come back after
  `power_on()` depends on the device and the host, not on the hub. Not measured.

## State and recovery

- **After a hub power loss or reset**: whether disabled ports come back enabled is
  **unknown**.
- **Unplugged while open**: commands fail with `NotConnected`. `close()` still
  succeeds; reopen once the hub is back.

## Hazards

- `power_off()` and `power_cycle()` cut data as well as power, so a device mid-transfer
  (e.g. a camera writing a file) loses that transfer.
- Know which port a device is on before cycling it. Ports are numbered from 0, and the
  numbering on the hub's case may differ (**unknown**).
- With several hubs attached, always pass a serial number. The driver refuses to
  guess, but a stale serial number in a config file points at whichever hub has it.

## Manual check

`el-devices hub list` lists attached hubs. `el-devices hub status` prints a hub's
identity, enumeration delay, and the power state of every port.
`el-devices hub cycle PORT` power-cycles one port. The `hardware`-marked tests are in
`tests/test_acroname_hardware.py`: set `ACRONAME_TEST_PORT` to a port that's safe to
switch (tests that switch power skip without it) and, with several hubs attached,
`ACRONAME_SERIAL_NUMBER`.

Not yet run on hardware.
