# gphoto2 cameras (Nikon Z7 II)

DSLR and mirrorless cameras driven over USB through libgphoto2, via python-gphoto2.
Driver: `educelab.devices.cameras.gphoto2.GPhoto2Camera`. The lab's reference body is a
Nikon Z7 II.

Facts below are marked **verified**, **assumed**, or **unknown** as described in the
[README](README.md#how-sure-we-are). Most of what's here so far is carried over from
`pgs-scan`'s spectral prototype (`SpectralCapture.py` on the `spectral-processing-module`
branch), which drove the Z7 II, and hasn't yet been re-checked against this driver.
Body-specific facts are in [Nikon Z7 II](#nikon-z7-ii); everything else applies to any
camera libgphoto2 drives over PTP.

## Identity

- `DeviceInfo.model` is the model name libgphoto2 identifies, read when the camera
  opens; before that it is `"unknown"`. `address` is the libgphoto2 port.
- `serial_number` and `firmware_version` are left unset. Both are in the config tree
  (`serialnumber`, `deviceversion` on Nikon), but reading it at `open()` costs a full
  config read, whose time on the Z7 II is **unknown** (Task 5.5 decides).

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
  the driver logs a warning naming it and doesn't download it, rather than returning it
  with the wrong capture.
- **Downloads are retried.** Right after a long exposure the camera may still be writing
  to the card or running long-exposure noise reduction, and briefly rejects the bulk
  transfer with a bare `[-1]` even though the file-added event arrived. The driver tries
  `DOWNLOAD_ATTEMPTS` (3) times, `DOWNLOAD_RETRY_DELAY_S` (1 s) apart. **assumed**, from
  the prototype.
- **Where the files go** depends on the camera's `capturetarget` widget. With
  `Internal RAM` (libgphoto2's default for Nikon) the camera holds each file only until
  it is downloaded, and it never reaches the card; with `Memory card` it is also written
  to the card. The driver doesn't change this setting and never deletes files. A file it
  discards (see above) is lost under `Internal RAM`. **assumed**, from libgphoto2's
  documentation; check what the Z7 II does in Task 5.4.

## Settings

Settings come from the camera's config tree, which libgphoto2 builds from PTP device
properties. Every `settings()` call reads the whole tree, so it is a snapshot (see the
guide). On the Z7 II a full read takes 2.6 s for 102 settings (2026-10-20).

- **Writes.** A write of one widget (a setting, a focus move) reads and writes just
  that widget through `gp_camera_get_single_config()`/`set_single_config()`, falling
  back to the whole tree if the camera driver doesn't support it (`-6`). Writes of
  several widgets at once (turning live view on) use one whole-tree round trip. Before
  this, every focus move on the Z7 II took about 4 s whatever its size: 2.6 s of that
  was the tree read.

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
  the canonical setting is still listed with `value` `None`; writing any of its choices
  takes the camera out of that state. The driver can't put the camera into Bulb or Auto;
  do that on the camera. The shutter speed texts above come from libgphoto2's PTP
  driver; what the Z7 II actually lists is **unknown** until Phase 5 records it.
- **Native settings**: every other widget, by libgphoto2's name (e.g. `imagequality`,
  `whitebalance`). Radio and menu widgets are strings with `choices`, ranges are floats
  with `minimum`, `maximum`, and `step`, toggles are booleans, text is a string, and
  dates are Unix times.
- **Actions are not settings.** Widgets in the `actions` section (`viewfinder`,
  `autofocusdrive`, `manualfocusdrive`, …) are commands; the driver uses them for live
  view and the focus drive and doesn't list them.
- **The `other` section is never read.** It holds the PTP properties libgphoto2 has no
  name for, by hex code (`d1f8`, `5011`, …; about 280 on the Z7 II). On the Z7 II,
  `/main/other/d1fd` is a text widget with no value (NULL), and python-gphoto2 2.6.4
  segfaults reading it: `get_value()` passes the NULL to `PyUnicode_FromString`
  unchecked, so Python can't catch it. Every other widget on the body reads cleanly
  (2026-10-20, Linux aarch64, libgphoto2 2.5.34; `~/hw-logs/dslr-widgets-linux-*.log`).
  If another body crashes the same way in a named section, the fix has to be a NULL
  check before reading, not another skipped section.
- **Aperture and live view**: the prototype paused live view to read or change the
  aperture, because the choices were restricted while it was on. On the Z7 II the
  choices are the same with live view on and off (f/4 to f/45 on a 60 mm f/4,
  2026-10-20), so the driver doesn't pause it.

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
- Preview images are JPEG on Nikon and Canon bodies. On the Z7 II, previews come back
  without `viewfinder` being set, so libgphoto2 starts live view itself.
- **Z7 II sizes and zoom** (2026-10-20, `~/hw-logs/dslr-liveview-linux-1.log`), both
  native settings:

  | `liveviewsize` | Frame | JPEG |
  |---|---|---|
  | `QVGA` | 320×212 | 4–8 KB |
  | `VGA` (default) | 640×424 | 13–20 KB |
  | `XGA` | 1024×680 | 27–40 KB |

  `liveviewzoomarea` (`0`, `512`, `1024`, `2048`) magnifies without changing the frame
  size: `0` is the whole sensor, `512` the tightest crop, `2048` the loosest. A zoomed
  XGA frame is the one to score for sharpness. Frames came back every 15–21 ms, which
  may be repeats rather than new frames; **unknown**. The first preview after `open()`
  took 6.7 s, including a settings read and live-view start.
- **Hold the `CameraFile` until its bytes are copied.** `get_data_and_size()` returns a
  view of the file's buffer that doesn't keep the file alive. Copying from a temporary
  (`bytes(camera.capture_preview().get_data_and_size())`) reads freed memory: on Linux
  aarch64 the first 32 bytes of every Z7 II preview were glibc heap pointers, and Pillow
  couldn't decode them (2026-10-20, `~/hw-logs/dslr-preview-linux-1.log`).

## Keep-alive

While a camera is open, an internal daemon thread reads one event (a 10 ms
`wait_for_event`) whenever the camera has been idle for `KEEP_ALIVE_INTERVAL_S` (2 s).
The prototype added this because multi-second gaps with no USB traffic, such as a
turntable move plus light settling between captures, were followed by the camera
leaving PC mode. **assumed**, from the prototype; whether the Z7 II needs it at all is
**unknown**.

- The ping skips any interval in which a capture is in flight or another call holds the
  device, so it never delays a capture or interleaves with one.
- Each failed ping is logged as a warning and pinging continues. After
  `KEEP_ALIVE_FAILURES` (3) failures in a row, listeners get one `Disconnected` event
  whose reason carries the last error; a good ping starts the count again. One failure
  alone isn't treated as a lost camera. The device stays open, so the consumer decides
  whether to close and reopen. How many failures a real unplug or PC-mode drop produces
  is **unknown**.
- A file added between captures (a late JPEG, or a shot taken on the camera itself) is
  logged and not downloaded, as during a capture.
- `close()` stops the thread before ending the session.

## Focus drive

`camera.focus` is a `FocusDrive` when the camera's config tree has a range-type
`manualfocusdrive` widget (Nikon and Sony bodies), and `None` otherwise. The first access
on each connection reads the config tree to find out, so it raises `NotConnected` while
the camera is closed. The drive shares the camera's connection and lock: it opens and
closes with the camera, and closing just the drive leaves the camera open. Writing a
number to the widget drives the lens motor by that many steps, positive toward far.
Canon bodies have a choice-type `manualfocusdrive` (`Near 1`–`3`, `Far 1`–`3`), which this
driver doesn't support yet.

Everything below comes from `usb-dslr-capture-demo` (`origin/ankan:capture-gui.py`),
tested on a Sony a6300. Where the Z7 II was checked (2026-10-20, Nikkor Z 24–120 mm,
`~/hw-logs/dslr-focus-linux-3.log`), the bullet says so.

- **Direction**: positive steps move toward far (infinity).
- **End stops**: a move into an end stop fails with `-113` (`GP_ERROR_CAMERA_ERROR`). The
  driver raises `FocusLimitReached` for `-113`, choosing near or far from the direction
  of the move. `-113` is libgphoto2's generic camera error, so on a body that reports
  end stops differently, or fails with `-113` for another reason, this would misreport.
  **Z7 II: confirmed** at both stops; a further move into the stop fails again, and a
  move away from it works.
- **After an end stop**, the camera queues events about the failed move. The driver
  reads them and waits `FOCUS_LIMIT_RECOVERY_S` (1 s) before the next command, which is
  what kept the a6300 responsive.
- **Busy**: a move sent while the motor is still turning fails with `-110`
  (`GP_ERROR_CAMERA_BUSY`). The driver retries up to `FOCUS_BUSY_ATTEMPTS` (15) times,
  `FOCUS_BUSY_WAIT_S` (0.5 s) apart, then raises `DeviceBusy`. **Z7 II:** never busy;
  libgphoto2 waits for each move to finish before returning, so back-to-back 2000-step
  moves all worked.
- **Settle time**: after each write the driver waits 100 ms + 0.5 ms per step, capped at
  3 s (`focus_settle_s()`).
- **Chunks**: `step(n)` writes at most `FOCUS_CHUNK_STEPS` (1000) steps at a time, and
  never more than the widget's range allows. `drive_to_limit()` writes chunks as large
  as the widget's range allows until it hits the end stop, and gives up after
  `FOCUS_LIMIT_MAX_STEPS` (1,000,000).
- **Step size**: on the Z7 II with the 24–120 mm, near to far is about 57,000 steps
  (57 moves of 1000). A 30-point sweep from far to near gave a smooth sharpness curve
  with one peak, so steps look consistent in a direction; whether they match in both
  directions is **unknown**. The a6300's range was never measured.
- **Manual focus refuses moves.** With `focusmode` at `Manual` (or the lens switched to
  M), every `manualfocusdrive` write on the Z7 II fails with `-1` after about 3.7 s, with
  live view on or off. With `focusmode` `AF-S` and `liveviewaffocus` `Single-servo AF`,
  the same moves work (2026-10-20). The driver doesn't change the focus mode; its `-1`
  error says to. The earlier run with a 60 mm lens failed the same way and was probably
  this, not a lens without a motor. `manualfocusdrive` is listed either way, so
  `camera.focus` isn't `None` with a lens that can't drive focus.
- **Live view**: the original code paused live view while seeking a limit, to avoid
  deadlocks. Focus moves here hold the camera's lock, so a preview can't interleave with
  them; whether the Z7 II still needs live view off is **unknown**.

## Nikon Z7 II

The lab's reference body. Everything here is carried over from the prototype or from
libgphoto2's Nikon driver and is **assumed** until Task 5.4 records what this body does.

- **Exposure**: set the mode dial to M so `shutterspeed`, `iso`, and `f-number` are all
  writable. In other modes the camera owns some of them and rejects writes.
- **Widgets the driver uses**: `shutterspeed` (exposure time), `iso`, `f-number`,
  `viewfinder` (live view), `manualfocusdrive` (focus drive, range type), and
  `continuousaf` if present. `imagequality` chooses NEF, JPEG, or NEF + JPEG.
- **Leaving PC mode**: the prototype saw the Z7 II drop out of PC mode when its event
  buffer filled (events never read), with an async capture loop, and after quiet gaps of
  several seconds. The driver drains events around every capture, uses the blocking
  capture, and keeps the session alive. After a drop, the next call fails with `-52`.
- **Error codes seen** by the prototype: `-53` (claimed by `ptpcamerad`), `-105` while
  re-enumerating, `-52` after leaving PC mode, `-1` for a download right after a long
  exposure, and `-10` for a port wedged by repeated `ptpcamerad` kills. The focus codes
  (`-110` busy, `-113` end stop) were seen on a Sony a6300, not on this body.

## Hazards

- **Discarded files under `Internal RAM`.** A file that arrives after its capture has
  returned is logged and not downloaded; with `capturetarget` set to `Internal RAM` it is
  then gone. Set `Memory card` if every file must survive.
- **`-113` is a generic error.** The focus drive treats it as an end stop. If a body
  returns `-113` for another failure, `FocusLimitReached` is wrong and the step count
  resets to a limit the lens isn't at.
- **Stale ports.** A port from `discover()` is only good until the camera re-enumerates.
- **Killing `ptpcamerad` repeatedly** can wedge the USB port for about 25 s per claim.

## Unknowns

Each needs the Z7 II on the bench (Tasks 5.4–5.6):

- The port format on macOS. (On Linux libgphoto2 reports the model as `Nikon Z7_2`, at
  `usb:003,002`.)
- How long a single-widget focus move takes now that it skips the tree read.
- The `shutterspeed`, `iso`, and `f-number` choice texts; whether `shutterspeed2`
  exists; and what `Bulb` and `Time` look like.
- How long a single-widget read of `serialnumber` takes. A full read is 2.6 s, too slow
  for `open()`, but one widget may be fast enough to fill in `DeviceInfo`.
- Whether RAW + JPEG's second file is queued by the time the capture returns, or can
  arrive after the 50 ms quiet gap the driver waits for.
- What `capturetarget` defaults to, and whether files reach the card.
- Whether the focus settle time (100 ms + 0.5 ms per step, capped at 3 s) is needed at
  all, since libgphoto2 already waits for each move.
- Whether the camera needs the keep-alive at all, and how many failed pings an unplug
  produces.

## Manual check

`el-devices dslr` opens the camera (`--port`, or the only one attached), lists its
settings and whether it has a focus drive, then runs whichever checks are asked for:
`--exposure SECONDS` sets the nearest shutter speed, `--capture DIR` captures once and
saves every file under the camera's names (refusing to overwrite), `--preview` grabs
one live-view frame and prints its size, and `--focus-steps N` moves focus, reporting a
limit if it hits one. Add `-v` (`el-devices -v dslr …`) to log libgphoto2's traffic. The `hardware`-marked tests in `tests/test_gphoto2_hardware.py`
cover capture (RAW and RAW + JPEG), exposure, preview, and focus. Results from real
hardware go here.

Not yet run on the Z7 II.
