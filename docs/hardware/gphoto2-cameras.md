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
| `-53` | `GP_ERROR_IO_USB_CLAIM` | Another process holds the USB interface (see [Another process holds the camera](#another-process-holds-the-camera)) | `DeviceBusy` at once, naming the remedy |
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

## Another process holds the camera

libgphoto2 fails with `-53` (`GP_ERROR_IO_USB_CLAIM`) when another process has claimed
the camera's USB interface. The driver raises `DeviceBusy` straight away, with a remedy
for the host platform only: `ptpcamerad` on macOS, `gvfs-gphoto2-volume-monitor` on
Linux, and a generic "another process holds the camera's USB interface" elsewhere. It
never kills processes itself.

### macOS: ptpcamerad

The system daemon `ptpcamerad` claims the USB interface of every still camera that's
plugged in, whether or not any app is using it. The daemon runs as the user and ignores
`SIGTERM`; `launchd` restarts it on demand.

Workaround: quit apps that use the camera (Image Capture, Photos), run
`killall -9 ptpcamerad`, then open the camera straight away. Once libgphoto2 holds the
interface, the daemon can't take it back until the session closes. **assumed**: the
prototype did this automatically, once per connect.

Kill it once, then wait rather than looping. The prototype found that killing the
daemon repeatedly while it held a session wedged the port, after which every claim
blocked for about 25 s with `-10` (`GP_ERROR_TIMEOUT`). **assumed**, from the
prototype's notes.

### Linux: gvfs-gphoto2-volume-monitor

Desktops running GVFS (GNOME and others) mount cameras as they appear, through
`gvfs-gphoto2-volume-monitor`, which holds the interface. Unmount the camera in the file
manager, or stop the monitor for the session. **assumed**, from the gphoto2 FAQ; not yet
seen on a lab machine.

## Capturing

`capture_files()` uses libgphoto2's blocking `gp_camera_capture()`, which releases the
shutter, waits out the exposure, and returns the first file the release produced.

- **Use the blocking capture, not `trigger_capture()` + event polling.** The prototype's
  async version was what made the Z7 II drop out of PC mode between shots; the blocking
  call (what `gphoto2 --capture-image-and-download` uses) handled a 10 s exposure
  repeatedly on the same camera, cable, and host. **assumed**, from the prototype's
  side-by-side test.
- **Further files arrive as events.** With RAW + JPEG, the first file comes back from the
  capture call and the rest as `GP_EVENT_FILE_ADDED`. The driver reads events every
  `EVENT_POLL_S` (50 ms) until the camera goes quiet, collecting added files, and gives up
  after `DRAIN_LIMIT_S` (2 s) if events keep coming (live view can report property
  changes continuously). **unknown** for the Z7 II: whether the JPEG's event is already
  queued when the capture returns, or can arrive after a 50 ms quiet gap. Settle it with
  the RAW + JPEG hardware test.
- **Drain events after every capture.** The camera queues events (file added, capture
  complete) in a finite buffer. If the host never reads them the buffer fills, PTP
  transactions then fail with `[-1]`, and the camera leaves PC mode, which shows up as
  `-52` on the next call. The prototype saw this on the Z7 II over multi-hundred-shot
  sessions. **assumed**, from the prototype.
- **Late files are discarded, not mis-attributed.** Events are also drained before each
  release. A file added then belongs to an earlier capture that has already returned, so
  the driver logs a warning naming it and leaves it on the card rather than returning it
  with the wrong capture.
- **Downloads are retried.** Right after a long exposure the camera may still be writing
  to the card or running long-exposure noise reduction, and briefly rejects the bulk
  transfer with a bare `[-1]` even though the file-added event arrived. The driver tries
  `DOWNLOAD_ATTEMPTS` (3) times, `DOWNLOAD_RETRY_DELAY_S` (1 s) apart. **assumed**, from
  the prototype.
- Files stay on the camera's card after download; the driver never deletes them.

## Settings

Settings come from the camera's config tree, which libgphoto2 builds from PTP device
properties. Every `settings()` call reads the whole tree, so it is a snapshot (see the
guide). How long a full read takes on the Z7 II is **unknown**; measure it in Phase 5.

- **Canonical settings** are read from these widgets, taking the first one the camera
  has, and replace them in the list:

  | Setting | Widgets | Native text | Value |
  |---|---|---|---|
  | `exposure_time` | `shutterspeed`, `shutterspeed2` | `1/250`, `10/13`, `2.5`, `30`, sometimes with a trailing `s` | seconds (float) |
  | `iso` | `iso` | `100` | int |
  | `aperture` | `f-number`, `aperture` | `f/5.6` | f-number (float) |

  Choices are the native choices that parse as numbers, in the camera's order, so pick
  one with `Setting.nearest()`. Writes send the camera's own text for the choice.
- **Values that aren't numbers** (`Bulb` and `Time` shutter speeds, an `Auto` ISO on
  some bodies) are left out of the canonical choices. While the camera is set to one,
  the canonical setting is absent and the widget is listed under its native name; set a
  numeric value there first. The shutter speed texts above come from libgphoto2's PTP
  driver; what the Z7 II actually lists is **unknown** until Phase 5 records it.
- **Native settings**: every other widget, by libgphoto2's name (e.g. `imagequality`,
  `whitebalance`). Radio and menu widgets are strings with `choices`, ranges are floats
  with `minimum`, `maximum`, and `step`, toggles are booleans, text is a string, and
  dates are Unix times.
- **Actions are not settings.** Widgets in the `actions` section (`viewfinder`,
  `autofocusdrive`, `manualfocusdrive`, …) are commands; the driver uses them for live
  view and the focus drive and doesn't list them.
- **Aperture and live view**: the prototype paused live view to read or change the
  aperture, because the choices were restricted while it was on. **unknown** whether this
  applies to the Z7 II; the driver doesn't do it yet.

## Live view

`preview_frame()` grabs one live-view image with `gp_camera_capture_preview()` and
decodes it with Pillow into an RGB array. On the first preview after `open()` the driver
reads the config tree and, in one write:

- turns live view on through the first of `viewfinder` (Nikon), `eosviewfinder` (Canon),
  or `d1a6` (Nikon's raw PTP property) the camera has, and
- turns continuous AF off through the first of `continuousaf`, `continuous-af`, or
  `continuousautofocus`, choosing the choice spelled `off`, `0`, `disable(d)`, `false`,
  or `no`, or `0` for a toggle.

These widget names and spellings are the prototype's. **assumed** for the Z7 II. Turning
continuous AF off keeps the lens from hunting between frames, and it stays off after
`close()`.

- **Best effort.** Most bodies return previews without live view being switched on
  explicitly, so a failure to enable it is logged as a warning and not retried until the
  next `open()`. **assumed**, from the prototype.
- `close()` turns live view off again if the driver turned it on, so the sensor isn't
  left running. A failure there is logged, not raised.
- Preview images are JPEG on Nikon and Canon bodies. Their size on the Z7 II is
  **unknown**; record it in Phase 5.

## Keep-alive

While a camera is open, an internal daemon thread reads one event (a 10 ms
`wait_for_event`) whenever the camera has been idle for `KEEP_ALIVE_INTERVAL_S` (2 s).
The prototype added this because multi-second gaps with no USB traffic, such as a
turntable move plus light settling between captures, were followed by the camera
leaving PC mode. **assumed**, from the prototype; whether the Z7 II needs it at all is
**unknown**.

- The ping skips any interval in which a capture is in flight or another call holds the
  device, so it never delays a capture or interleaves with one.
- A failed ping is logged as a warning and pinging continues. The driver doesn't treat
  one failure as a lost connection; the next real call reports the error.
- A file added between captures (a late JPEG, or a shot taken on the camera itself) is
  logged and left on the card, as during a capture.
- `close()` stops the thread before ending the session.
