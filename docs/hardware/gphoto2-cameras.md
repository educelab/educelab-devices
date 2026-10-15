# gphoto2 cameras (Nikon Z7 II)

DSLR and mirrorless cameras driven over USB through libgphoto2, via python-gphoto2.
Driver: `educelab.devices.cameras.gphoto2.GPhoto2Camera`. The lab's reference body is a
Nikon Z7 II.

Facts below are marked **verified**, **assumed**, or **unknown** as described in the
[README](README.md#how-sure-we-are). Most of what's here so far is carried over from
`pgs-scan`'s spectral prototype (`SpectralCapture.py` on the `spectral-processing-module`
branch), which drove the Z7 II, and hasn't yet been re-checked against this driver.

## Transport

- **SDK**: python-gphoto2 (`pip install 'educelab-devices[gphoto2]'`), whose wheels bundle
  libgphoto2 on macOS and Linux. There is no Windows build, so the extra installs nothing
  there and the driver raises `ImportError` on first use.
- **Logging**: libgphoto2's own log goes to the Python logger
  `educelab.devices.cameras.gphoto2.libgphoto2` (its errors as warnings, its verbose and
  debug output as info and debug). It matters: libgphoto2 often reports a failure to the
  caller as a bare `[-1] Unspecified error` and logs the real cause (a PTP response
  code, a USB error) only there. **assumed**, from the prototype's experience.
- **Ports**: cameras are addressed by libgphoto2 port, e.g. `usb:020,007` (bus, device).
  The device number changes whenever the camera re-enumerates (replug, power cycle,
  reset), so a port is only good for the current plug-in. **assumed**, from how USB
  device numbering works.
- **Discovery** uses libgphoto2's autodetect, which reads USB descriptors without
  claiming the camera, so it works while another process holds it. **assumed**.

## Opening a session

`open()` with no port autodetects and opens the only camera attached; it refuses to guess
when several are. Errors while opening:

| Code | libgphoto2 name | What it means | Driver |
|---|---|---|---|
| `-53` | `GP_ERROR_IO_USB_CLAIM` | Another process holds the USB interface (see [ptpcamerad](#ptpcamerad)) | `DeviceBusy` at once, naming the remedy |
| `-105` | `GP_ERROR_MODEL_NOT_FOUND` | "Unknown model": nothing on the bus, or a camera still re-enumerating after a reset or replug | retried, then `DeviceNotFound` |
| `-52` | `GP_ERROR_IO_USB_FIND` | The port given has no device behind it | retried, then `DeviceNotFound` |
| other | | | `DeviceError` with the code, not retried |

- `-105` for both an empty bus and a camera that is re-enumerating: **assumed**, from the
  prototype, which retried it and only gave up early when no `-53` had been seen.
- `-52` for an explicit port with nothing behind it: **unknown**. libgphoto2 accepts any
  `usb:BBB,DDD` path when looking a port up, so the failure surfaces at `init`; settle it
  by opening a stale port on the Z7 II.
- Retry budget: 3 attempts, 0.5 s apart (`OPEN_ATTEMPTS`, `OPEN_RETRY_DELAY_S`). These
  are the prototype's values for the same case. **assumed** to be enough; a camera
  waking from sleep may need longer.

## ptpcamerad

On macOS, the system daemon `ptpcamerad` claims the USB interface of every still camera
that's plugged in, whether or not any app is using it, and libgphoto2 then fails with
`-53`. The daemon runs as the user and ignores `SIGTERM`; `launchd` restarts it on demand.

Workaround: quit apps that use the camera (Image Capture, Photos), run
`killall -9 ptpcamerad`, then open the camera straight away. Once libgphoto2 holds the
interface, the daemon can't take it back until the session closes. **assumed**: the
prototype did this automatically, once per connect.

The driver never kills processes itself; the `DeviceBusy` message says what to run. The
prototype found that killing the daemon repeatedly while it held a session wedged the
port, after which every claim blocked for about 25 s with `-10` (`GP_ERROR_TIMEOUT`), so
kill it once and then wait rather than looping. **assumed**, from the prototype's notes.

On Linux the equivalent is usually a desktop's `gvfs-gphoto2-volume-monitor`, which
mounts cameras as they appear. **assumed**, from the gphoto2 FAQ.
