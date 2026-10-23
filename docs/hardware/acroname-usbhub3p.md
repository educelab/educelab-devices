# Acroname USBHub3p

An 8-port USB 3 hub from Acroname whose downstream ports can be switched on and off
from the host. The lab uses it to power-cycle hung cameras without unplugging them.
It's controlled through Acroname's BrainStem SDK (the `brainstem` package), which wraps
a native library.

Driver: `educelab.devices.hubs.acroname.AcronameHub`. Manual check: `el-devices hub`.

Facts below come from the BrainStem SDK 2.12.5 source and docstrings, and from
`pgs-scan`'s `utils/usb_hub3p_config.py` and `utils/usb_hub3p_reset.py` (branch `dev`),
unless marked **verified**. Verified facts come from hardware test runs and read-only
checks of one unit, serial `0x1A43352C`, firmware 2.9.20, on macOS on 2026-10-16 (see
[Manual check](#manual-check)).

## Identity

- **Model**: the SDK class is `brainstem.stem.USBHub3p`, and discovery reports model
  number `brainstem.defs.MODEL_USBHUB_3P` (19). The driver reports the model as
  `USBHub3p`.
- **Serial number**: a 32-bit integer, conventionally written in hex. The driver
  writes it as `0x` and 8 hex digits, e.g. `0x1234ABCD`, and accepts that form or the
  bare hex digits. `pgs-scan` logged it the same way.
- **Firmware version**: `system.getVersion()` returns a packed number, which
  `brainstem.version.unpack_version()` splits into major, minor, and patch. The driver
  reports it as `major.minor.patch`. Versions seen: `2.9.20` on `0x1A43352C`
  (**verified** 2026-10-16).
- **On the host**: the hub appears as several USB devices, all with vendor ID
  `24FF` (Acroname Inc.) and the hub's serial number as their USB serial string
  (**verified** on macOS, `0x1A43352C`, 2026-10-16, with `system_profiler
  SPUSBHostDataType`):
  - `USBHub3p-2[A]`, `USBHub3p-2[0-3]`, `USBHub3p-2[4-7]`: the USB 2 hubs, product ID
    `8013`.
  - `USBHub3p-3[A]`, `USBHub3p-3[0-3]`, `USBHub3p-3[4-7]`: the USB 3 hubs, product ID
    `8013`, at 5 Gb/s.
  - `USBHub3p-Stem`: the BrainStem control endpoint, product ID `0013`, at 12 Mb/s,
    behind the upstream USB 2 hub. The driver talked to it with only the upstream
    cable connected, so the separate control port isn't needed for this. The
    `[0-3]` and `[4-7]` hubs are internal. How their port positions map to the
    driver's port numbers is **unknown**; plugging one device into each port in turn
    and reading the host's USB tree would settle it.

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
- **macOS**: one universal library. **Verified** with Python 3.13.15 and brainstem
  2.12.5, controlling `0x1A43352C`, 2026-10-16.

macOS has been used to control a hub, and Linux (Fedora) to list one; the other Linux and the Windows notes come from the SDK.

Importing `educelab.devices.hubs` never imports `brainstem`. The SDK is loaded on the
first `discover()` or `open()`, so these failures only affect this backend.

### Linux permissions

Without a udev rule, only root can open the hub. Acroname's SDK download ships a
rule like the one below (**assumed**; the PyPI wheel doesn't include it). The rule
below works (**verified** on Fedora, 2026-10-23). Save it as
`/etc/udev/rules.d/99-brainstem.rules`, then run
`sudo udevadm control --reload-rules && sudo udevadm trigger` and replug the hub:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="24ff", MODE="0666"
```

A missing rule hides the hub silently: without it, `el-devices hub list` printed
"No USBHub3p hubs found." and gave no permission error. With the rule, the hub
listed. **Verified** on Fedora, 2026-10-23. So the SDK loads on at least one
distribution outside its bundled list. Whether `open()` returns the permission error
(12) instead of `DeviceNotFound` is unchecked.

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
  calls, which the driver uses for [port speed](#port-speed).
- **Reading**: `usb.getPortState(port)` returns a bit field. The driver reads bit 0
  (`aUSBHUB3P_USB_VBUS_ENABLED`) as "powered". Other bits include USB 2 data enabled
  (1), USB 3 data enabled (3), connected at USB 2 (11), connected at USB 3 (12),
  error flag (19), and device attached (23).
- **Seen on `0x1A43352C`** (**verified**, read-only, 2026-10-16): every port powered
  with both data lines enabled. Ports 1–5 had USB 3 cameras attached
  (`0x0080100B`: VBUS, USB 2 and USB 3 data, connected at USB 3, attached) drawing
  about 440 mA each (`usb.getPortCurrent`). Port 0 had a USB 2 serial adapter
  (`0x0080080B`, connected at USB 2). Ports 6 and 7 were empty (`0x0000000B`).
  `usb.getPortMode()` read `0x00000001` on every port, and
  `usb.getDownstreamDataSpeed()` agreed with bits 11 and 12 (0 nothing, 1 Hi-Speed,
  2 SuperSpeed).
- **The newer Port entity** (`stem.hub.port[i]`, 12 entries): every getter tried
  (`getDataSpeed`, `getMode`, `getState`, `getDataRole`, `getDataHSEnabled`,
  `getDataSSEnabled`, `getEnabled`) returned error 18 (timeout) on firmware 2.9.20
  (**verified** 2026-10-16). The driver uses only the legacy `usb` entity.

### Port speed

The driver can limit how fast each port's data link runs, e.g. to keep a camera at
USB 2 and cap its bandwidth. It's an `AcronameHub` method, not part of the `Hub`
role.

| `PortSpeed` | Data lines | Calls |
|---|---|---|
| `USB3` (default) | Hi-Speed and SuperSpeed | `setHiSpeedDataEnable`, `setSuperSpeedDataEnable` |
| `USB2` | Hi-Speed only | `setSuperSpeedDataDisable`, `setHiSpeedDataEnable` |
| `DATA_OFF` | none; power only | `setSuperSpeedDataDisable`, `setHiSpeedDataDisable` |

**Lowering the limit applies at once; raising it to `USB3` needs a power cycle.**
Call `power_cycle(port)` after `set_port_speed(port, PortSpeed.USB3)`, or use
`el-devices hub speed PORT usb3 --cycle`. Without `--cycle`, `el-devices hub speed`
warns when the port's device is still at USB 2. Details under
[Speed changes on a live device](#speed-changes-on-a-live-device).

- `set_port_speed(port, speed)` switches only the data lines, never VBUS. It disables
  lines before enabling others, so the port never briefly allows more than asked.
- **`power_on()` keeps the limit.** `setPortEnable` would turn every data line back
  on, so for a `USB2` or `DATA_OFF` port the driver calls `setPowerEnable` and then
  sets the data lines. `power_off()` still uses `setPortDisable`, which clears power
  and data. **Verified** on port 7 of `0x1A43352C`, 2026-10-16: `USB2` and `DATA_OFF`
  read back from the port's data lines after `power_off()`/`power_on()` and after
  `power_cycle()`.
- **The limit lives in the driver while the hub is open**, starting from each port's
  state bits at `open()`: both data lines means `USB3`, Hi-Speed only `USB2`, neither
  `DATA_OFF`. A port that's unpowered at `open()` reads as `USB3`, because
  `setPortDisable` clears the data lines along with power, so they say nothing about
  the limit. A port with SuperSpeed lines but no Hi-Speed lines also reads as `USB3`
  (logged at debug). Setting a speed on an unpowered port takes effect at
  `power_on()`.
- `connected_speed(port)` reads bits 12 and 11 of the port state: `USB3`, `USB2`, or
  `None` if no device has connected. A full-speed device reports as USB 2
  (**verified** on port 0 of `0x1A43352C`, 2026-10-16: a HOLTEK USB-to-serial adapter,
  `04d9:b534`, that macOS listed at 12 Mb/s read as connected at USB 2). Low-speed
  devices are **assumed** to do the same.
- Nothing is saved to the hub's flash.
- **The limit reads back without a power cycle.** **Verified** on port 7 of
  `0x1A43352C`, 2026-10-16: each of the three limits read back from the port's data
  lines on a fresh connection to the hub, and by hand on the empty port,
  `el-devices hub speed 7 usb2` reported a `usb2` limit until
  `el-devices hub speed 7 usb3 --cycle`.

#### Speed changes on a live device

**Verified** on port 7 of `0x1A43352C`, firmware 2.9.20, with a USB 3 device drawing
about 245 mA, 2026-10-16 (hardware tests):

- **Lowering to `USB2`** without a power cycle: the device dropped from USB 3 to
  USB 2 within 0.5 s (0.8 s in a second run).
- **`DATA_OFF`** without a power cycle: the device disconnected within 0.3 s.
- **Raising from `USB2` to `USB3`** without a power cycle: the device stayed at USB 2
  (port state `0x0080080B`: VBUS, both data lines enabled, at USB 2, attached). It
  was watched for 15.1 s in one run and 5.1 s in another. It was also still at USB 2
  another 15.1 s later, after both data lines had been re-enabled and `setPortEnable`
  called on the powered port. So neither re-enabling the lines nor `setPortEnable`
  brings it back; a power cycle does (below).
- Two restores in the same run reached USB 3 without a power cycle. Both re-enabled
  the data lines and then called `setPortEnable`.
  - From `DATA_OFF`: back at USB 3 in 0.8 s. With no data lines the device was fully
    disconnected, so it attached fresh.
  - From `USB2`, about half a second after the device dropped: back at USB 3 in
    0.3 s.

  Why the second one worked and the 15.1 s case didn't is **unknown**. One guess
  is that a USB 3 device retries SuperSpeed only for a short while after losing it,
  and then waits for a power cycle (**assumed**, from the USB 3 spec's SS.Disabled
  state). Timing how soon after the drop the limit is raised would test it. Don't
  rely on it: power-cycle after raising to `USB3`.
- **A power cycle brings it back.** After `power_cycle()`, the device reconnected at
  USB 3 in 3.4–3.6 s, including from a fallen-back USB 2 connection with USB 3
  already re-enabled. A port limited to `USB2` reconnected at USB 2 in 3.8 s, so the
  limit survives the cycle. Times are counted from when `power_cycle()` returns,
  after the default 1 s off time (`Hub.power_cycle_off_time`). **Verified** on the
  same device and port, 2026-10-16.

**Unknown**:
- whether data-line settings survive a hub reset or power loss. Setting a port to
  `USB2`, removing the hub's power, and reading `el-devices hub status` afterwards
  would settle it;
- whether the hub accepts data-line changes on an unpowered port (the driver
  avoids them).

### Enumeration delay

`usb.getEnumerationDelay()` and `usb.setEnumerationDelay(ms)` read and set how long
the hub waits between enumerating its ports. The SDK says the value is in 100 ms
increments. The driver exposes it as `enumeration_delay()` (a `Setting` in seconds,
step 0.1) and `set_enumeration_delay(seconds)`, which rounds to the nearest 100 ms.

The driver only changes the running value. `pgs-scan`'s config script followed the
set with `system.save()` (write to the hub's flash) and `system.reset()`. The driver
doesn't do either, because devices persist nothing. **Verified** on `0x1A43352C`,
2026-10-16, that a new value reads back from the running hub and can be restored. The
unit read 1 s afterwards.

**Unknown**: whether the new value takes effect without a save and reset, and what
the maximum is. Setting a long delay, then timing how far apart devices on several
ports come back after the hub re-enumerates, would settle the first; setting
increasing values until the SDK rejects one would settle the second.

## Timing

- **Power-cycle off time**: the `Hub` default of 1 s, the same as `pgs-scan`'s reset
  script. It's a convention, not a measured requirement; a device that holds charge
  may need longer.
- **Re-enumeration**: one USB 3 device on port 7 of `0x1A43352C` reached the hub's
  "connected" state 3.4–3.8 s after `power_cycle()` returned, with the default
  1 s off time (**verified**, 2026-10-16). That's when the hub reports the link; the
  host may take longer to enumerate. Expect it to vary with the device.

## State and recovery

- **Across a reconnect**: a port switched off stays off after `close()` and a new
  `open()`; opening doesn't change port power. **Verified** on `0x1A43352C`,
  2026-10-16.
- **After a hub power loss or reset**: whether disabled ports come back enabled is
  **unknown**. Switching a spare port off, removing the hub's power, and reading
  `el-devices hub status` afterwards would settle it.
- **Unplugged while open**: commands fail with `NotConnected`. `close()` still
  succeeds; reopen once the hub is back.
- **A device that won't return to USB 3**: if a port reads `limit usb3, connected usb2`
  even after `power_cycle()`, suspect the cable. On 2026-10-19 a camera on port 3 of
  `0x1A43352C` stayed at USB 2 after `el-devices hub cycle 3`; a USB 2 cable or a
  damaged USB 3 pair is the likely cause (**assumed**; swapping the cable would
  confirm it).

## Hazards

- `power_off()` and `power_cycle()` cut data as well as power, so a device mid-transfer
  (e.g. a camera writing a file) loses that transfer.
- Know which port a device is on before cycling it. Ports are numbered from 0, as
  labelled on the hub's case (**verified** on `0x1A43352C`: a device in the port
  labelled 7 showed up on driver port 7).
- Power, speed, and the hardware tests act on real ports. On a rig, the other ports
  usually carry cameras in use: on 2026-10-16, ports 1–5 of `0x1A43352C` had five
  cameras on them. Run the hardware tests only with `ACRONAME_TEST_PORT` set to a
  port with nothing important on it.
- A `USB2` or `DATA_OFF` limit on a port that was unpowered at `open()` is forgotten
  on the next `open()`: it reads as `USB3`, and the next `power_on()` enables every
  data line.
- With several hubs attached, always pass a serial number. The driver refuses to
  guess, but a stale serial number in a config file points at whichever hub has it.

## Manual check

`el-devices hub list` lists attached hubs. `el-devices hub status` prints a hub's
identity, enumeration delay, and each port's power, speed limit, and connected
speed. `el-devices hub cycle PORT` power-cycles one port, and
`el-devices hub speed PORT [usb3|usb2|data-off] [--cycle]` shows or sets one port's
limit.

The `hardware`-marked tests are in `tests/test_acroname_hardware.py`. They only
switch the port named by `ACRONAME_TEST_PORT`, which must have nothing important on
it, and restore its power and speed afterwards; tests that switch anything skip
without it. With several hubs attached, set `ACRONAME_SERIAL_NUMBER`.

Set `ACRONAME_USB3_DEVICE=1` when a spare USB 3 device is plugged into the test port.
The device tests first wait up to 15 s for it to connect at USB 3, power-cycling once
if it's at USB 2, and fail saying to plug one in (or check the cable) if it doesn't.
They check that `USB2` and `DATA_OFF` apply without a power cycle, that raising to
`USB3` without one leaves the device at USB 2 for 5 s, and that the device reconnects
at its limit within 30 s of a power cycle. On failure they print the raw port state.

### 2026-10-16

All runs by the maintainer on macOS with Python 3.13.15, pytest 9.1.1, and brainstem
2.12.5. The logs from the speed runs name the hub as `0x1A43352C`; the first log
doesn't name the hub, firmware, or test port, but it was the only hub attached when
checked later that day.

- **Power and enumeration delay**: 7 passed in 7.79 s. The hub identifies as a
  USBHub3p with a serial number and firmware version in the expected formats,
  `discover()` lists it, every port reports its power, `power_off()`, `power_on()`,
  and `power_cycle()` switch the test port, a port switched off stays off across a
  reconnect, and the enumeration delay reads back and is restored.
- **Read-only checks** (`el-devices hub list`, `status`, `speed 7`, a script calling
  only SDK getters, and `system_profiler SPUSBHostDataType`): one hub, serial
  `0x1A43352C`, firmware 2.9.20, enumeration delay 1 s, all 8 ports powered at
  `USB3`, ports 1–5 connected at USB 3, port 0 at USB 2, ports 6 and 7 empty. The
  results are in [Identity](#identity) and [Ports](#ports).
- **Port speed, port 7 empty**: 12 passed and 3 skipped (the device tests) in
  40.35 s. Each limit reads back on a fresh connection, and `USB2` and `DATA_OFF`
  survive `power_off()`/`power_on()` and `power_cycle()`. Read-only checks
  afterwards found port 7 powered with both data lines enabled, nothing attached,
  drawing 0 µA, and every other port as before.
- **Port speed, USB 3 device on port 7** (`ACRONAME_USB3_DEVICE=1`, `-s`): in 89.57 s,
  `USB2` applied in 0.5 s and `DATA_OFF` disconnected in 0.3 s, but raising to
  `USB3` without a power cycle left the device at USB 2 for 15.1 s. The two
  reconnect cases couldn't start, because the device was still at USB 2.
- **Reconnect and raise-to-USB 3 tests**, same device and port: 3 passed in 31.46 s.
  The device, still at USB 2 from the previous run, reached USB 3 3.6 s after a
  power cycle. Raising from `USB2` to `USB3` without a cycle again left it at USB 2,
  for the 5.1 s watched. After `power_cycle()` it reconnected at USB 3 in 3.4–3.6 s,
  and at USB 2 in 3.8 s with a `USB2` limit.

Not checked on hardware: controlling a hub on Linux or Windows, whether the enumeration delay takes effect
without a save, and behavior after a hub power loss.
