# gphoto2 cameras (Nikon Z7 II)

DSLR and mirrorless cameras driven over USB through libgphoto2, via python-gphoto2.
Driver: `educelab.devices.cameras.gphoto2.GPhoto2Camera`. The lab's reference body is a
Nikon Z7 II.

Facts below are marked **verified**, **assumed**, or **unknown** as described in the
[README](README.md#how-sure-we-are). Observations on the Z7 II come from two bodies
(serials `…03057001` and `…03059822`, firmware `V1.62`) on Linux aarch64 with
python-gphoto2 2.6.4 and libgphoto2 2.5.34, 2026-10-20, unless they say otherwise. Facts
marked **assumed** mostly come from `pgs-scan`'s spectral prototype (`SpectralCapture.py`
on the `spectral-processing-module` branch), which drove a Z7 II. Body-specific facts
are in [Nikon Z7 II](#nikon-z7-ii); everything else applies to any camera libgphoto2
drives over PTP.

## Identity

- `DeviceInfo.model` is the model name libgphoto2 identifies, read when the camera
  opens; before that it is `"unknown"`. The Z7 II identifies as `Nikon Z7_2`.
  `address` is the libgphoto2 port.
- `serial_number` and `firmware_version` come from the `serialnumber` and
  `deviceversion` widgets, each read on its own at `open()`, since a full config read
  is too slow for `open()` (see [Settings](#settings)). A camera without them, or a
  failed read, leaves them `None`. The Z7 II pads its serial with zeros to 32 digits
  (`00000000000000000000000003057001`) and reports firmware `V1.62`. `open()` takes
  0.13 s including both reads. **verified** (`gphoto2-hw-open-linux-1.log`), before the
  vendor read below was added.
- `vendor` comes from the `manufacturer` widget, read the same way at `open()`, so
  `open()` now makes three reads. libgphoto2's PTP driver fills it from the camera's
  device info. `discover()` can't report it: libgphoto2's camera list has no vendor.
  The Z7 II reports `Nikon Corporation`. Each identity read took about 1 ms, and `open()`
  with all three took 0.24 s. **verified** (body `…03057001`, macOS 27, python-gphoto2
  2.6.4, 2026-10-29, after `killall -9 ptpcamerad`).

## Transport

- **SDK**: python-gphoto2 (`pip install 'educelab-devices[gphoto2]'`), whose wheels bundle
  libgphoto2 on macOS and Linux. There is no Windows build, so the extra installs nothing
  there and the driver raises `ImportError` on first use.
- **Logging**: libgphoto2's own log goes to the Python logger
  `educelab.devices.cameras.gphoto2.libgphoto2` (its errors as warnings, its verbose and
  debug output as info and debug). It matters: libgphoto2 often reports a failure to the
  caller as a bare `[-1] Unspecified error` and logs the real cause only there. On the
  Z7 II, a refused focus move surfaces as `[-1]` with only
  `'camera->functions->set_config (camera, window, context)' failed: -1` in the log, so
  the log doesn't always know more either. **verified**.
- **Ports**: cameras are addressed by libgphoto2 port, `usb:BBB,DDD` (bus, device), on
  both Linux and macOS. The device number changes whenever the camera re-enumerates
  (replug, power cycle, reset), so a port is only good for the current plug-in. One Z7 II
  showed up as `usb:001,007`, `usb:001,010`, and `usb:001,012` across a day of
  replugging. **verified**.
- **Discovery** uses libgphoto2's autodetect, which reads USB descriptors without
  claiming the camera, so it works while another process holds it. **verified** on
  macOS: autodetect found the Z7 II at `usb:001,001` while `ptpcamerad` held it.

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
  `usb:BBB,DDD` path when looking a port up, so the failure surfaces at `init`. Opening a
  stale port would settle it.
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
plugged in, whether or not any app is using it. With it running, every open of the
Z7 II fails with `-53` and the driver's hint. **verified** on macOS, 2026-10-20
(`z7-1-info.log`, `z7-3-tests.log`). The daemon runs as the user and ignores `SIGTERM`;
`launchd` restarts it on demand. **assumed**.

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
manager, or stop the monitor for the session. **assumed**, from the gphoto2 FAQ. The lab's
Linux bench machine never returned `-53`.

## Capturing

`capture_files()` uses libgphoto2's blocking `gp_camera_capture()`, which releases the
shutter, waits out the exposure, and returns the first file the release produced.

- **Use the blocking capture, not `trigger_capture()` + event polling.** The prototype's
  async version was what made the Z7 II drop out of PC mode between shots; the blocking
  call (what `gphoto2 --capture-image-and-download` uses) handled a 10 s exposure
  repeatedly on the same camera, cable, and host. **assumed**, from the prototype's
  side-by-side test. With the blocking capture, ten captures in a row and a capture
  after 10 s idle all worked on the Z7 II. **verified** (`gphoto2-hw-linux-3.log`).
- **Further files arrive as events.** With RAW + JPEG, the first file comes back from the
  capture call and the rest as `GP_EVENT_FILE_ADDED`. The driver reads events every
  `EVENT_POLL_S` (50 ms) until the camera goes quiet, collecting added files, and gives up
  after `DRAIN_LIMIT_S` (2 s) if events keep coming (live view can report property
  changes continuously). On the Z7 II at NEF + JPEG, every capture returned both files,
  JPEG first (`capt0001.jpg`, 2.6 MB; `capt0002.nef`, 51 MB). **verified**. A NEF alone
  is about 51 MB.
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
- **Where the files go** depends on the camera's `capturetarget` widget (`Internal RAM`
  or `Memory card`). The driver doesn't change it and never deletes files. On the Z7 II
  it is `Internal RAM` when the camera opens, and captures work with no card in the
  camera. **verified**. Under `Internal RAM` the camera holds each file only until it is
  downloaded, so a file the driver discards (see above) is lost; with `Memory card` it
  is also written to the card. **assumed**, from libgphoto2's documentation.

## Settings

Settings come from the camera's config tree, which libgphoto2 builds from PTP device
properties. Every `settings()` call reads the whole tree, so it is a snapshot (see the
guide). On the Z7 II a full read takes 2.6–2.8 s for 102 settings. Reads straight
after another take longer: 3.6–3.7 s, over 10 in a row (body `…03059822`, 2026-11-04).
**verified**.

- **Reading one setting.** `setting(name)` reads just the widget the setting comes from,
  through `gp_camera_get_single_config()`. The driver learns which widget that is from
  a full read, so the first `setting()` after `open()` (unless `settings()` came first)
  reads the whole tree. A setting whose widget has gone, or a name the driver hasn't
  seen, also takes a full read. Each call still reads the camera, so the result is as
  fresh as `settings()` as far as the driver goes. On the Z7 II a single read takes
  about 1 ms (at most 15 ms over 10 reads each of `exposure_time`, `iso` and
  `imagequality`), against 2.8 s for the first full read and 3.6–3.7 s for repeated
  ones. **verified** (2026-11-04, `gphoto2-single-setting-time-settings-1.log`). 1 ms
  is quick for a USB round trip. libgphoto2 may be answering from its own copy of the
  property, which it updates from camera events. Whether a change made on the body
  (a dial turned) shows up in the next single read is **unknown**. A value the driver
  wrote does read back at once. **verified**.
- **Writes.** `set_setting()` first reads the setting to check the value against its
  current choices, then writes it. Both the read and the write touch only that one
  widget, through `gp_camera_get_single_config()`/`set_single_config()`. If the camera
  driver doesn't support single-widget access (`-6`), each falls back to a whole-tree
  read or round trip. Writes of several widgets at once (turning live view on) use
  one whole-tree round trip, which costs a full read first. The Z7 II supports
  single-widget access. **verified**. On the Z7 II, `set_setting()` takes 0.19–0.27 s
  for `exposure_time` and 0.02–0.20 s for `iso`, each moving one choice and back,
  20 writes each. **verified** (same log). Before single-setting reads, each
  `set_setting()` also did a full read to validate, so it cost at least 2.8 s.
- **Canonical settings** are read from these widgets, taking the first one the camera
  has, and replace them in the list:

  | Setting | Widgets | Native text | Value |
  |---|---|---|---|
  | `exposure_time` | `shutterspeed`, `shutterspeed2` | `1/250`, `10/13`, `2.5`, `30`, sometimes with a trailing `s` | seconds (float) |
  | `iso` | `iso` | `100` | int |
  | `aperture` | `f-number`, `aperture` | `f/5.6` | f-number (float) |

  Choices are the native choices that parse as numbers, in the camera's order, so pick
  one with `Setting.nearest()`. Writes send the camera's own text for the choice, and
  read back as written. **verified** on the Z7 II for `exposure_time`.
- **Z7 II choices.** `shutterspeed` gives decimal seconds (`0.3333s`): 55 choices, 53 of
  them numbers, from 1/8000 s to 30 s. The two that aren't are Bulb and Time; their exact
  text wasn't recorded. `shutterspeed2` also exists, with fractions (`1/3`) and 58 choices,
  but the driver reads `shutterspeed` first. `iso` has 33 choices from 32 to 51200.
  `f-number` choices depend on the lens: f/4 to f/22 on the 24–120 mm f/4. **verified**.
- **Values that aren't numbers** (`Bulb` and `Time` shutter speeds, an `Auto` ISO on
  some bodies) are left out of the canonical choices. While the camera is set to one,
  the canonical setting is still listed with `value` `None`; writing any of its choices
  takes the camera out of that state. **verified** on the Z7 II at Bulb. The driver can't
  put the camera into Bulb or Auto; do that on the camera. On the Z7 II, auto ISO is the
  separate `isoauto` toggle, so `iso` stays a number while it is on. **verified**.
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
  unchecked, so Python can't catch it. Every other widget on the body reads cleanly.
  **verified** (`dslr-widgets-linux-*.log`). If another body crashes the same way in a
  named section, the fix has to be a NULL check before reading, not another skipped
  section.
- **Aperture and live view**: the prototype paused live view to read or change the
  aperture, because it found the choices restricted while live view was on. On the Z7 II
  the choices are the same with live view on and off (f/4 to f/45 on a 60 mm f/4,
  f/4 to f/22 on the 24–120 mm f/4), so the driver doesn't pause it. **verified**.

## Live view

`preview_frame()` grabs one live-view image with `gp_camera_capture_preview()` and
decodes it with Pillow into an RGB array, cropped to the preview region on the host.
`start_preview()` reads the config tree and turns live view on through the first of
`viewfinder` (Nikon), `eosviewfinder` (Canon), or `d1a6` (Nikon's raw PTP property) the
camera has. `stop_preview()` and `close()` turn it off again. `not_before` goes by
arrival: the driver asks for the image only once `not_before` has passed, and stamps
it with the time it arrived.

These widget names are the prototype's. The Z7 II has `viewfinder`. **verified**. The
driver no longer turns continuous AF off; it's the caller's setting.

On the Z7 II (`Nikon Z7_2`), preview also drives the zoom, from the geometry measured
below:

- **Region:** a fraction of the **image area** (FX, DX, 5:4, 1:1 or 16:9), which is
  what captures and unzoomed live view cover. The driver reads the area's size in
  sensor pixels from the largest `imagesize` choice each time preview starts.
- **Zoom:** the tightest `liveviewzoomarea` whose crop contains the preview region,
  or `0` for a region the `2048` crop can't hold. Never a level that holds only part
  of the region, though it would be sharper: a preview shows what was asked for. So a
  region wider than 0.25 of the FX width is cut from the unzoomed image, and one
  0.33 wide is 338 pixels across. For a short while (2026-11-12) the driver zoomed
  such regions and showed their middle; the maintainer ruled that out.
- **Centre:** `changeafarea`, at the region's centre in pixels of the image area. It's
  written after live view is running, and again with every zoom change. With a new
  zoom level it's written twice (see the zoom geometry below).
- **Scale:** the geometry's `capture_size` is the selected `imagesize` (8256 × 5504
  for FX at L), read each time preview starts, so `scale` compares a preview with the
  capture the camera would take now. Checked on the camera in FX at L: 0.124 for the
  whole view and for a region 0.335 wide, 0.5 at zoom `2048`, 1.0 at `1024` and 2.0
  at `512`. **verified** (2026-11-12). Changing `imagesize` during a preview shows
  from the next `start_preview()`. A body the driver can't read an image size from
  reports no scale.
- **Size:** `liveviewsize` `XGA` unless `max_size` asks for less. Then the host keeps
  every n-th pixel to fit, so the driver picks the smallest size that delivers at
  least 0.8 of the most any size would: a `max_size` of 1000 gets `VGA` whole (640)
  and not `XGA` halved (512). The host crops the image to the region.
- **Settling:** after any of those writes, `start_preview()` waits 0.4 s
  (`LIVE_VIEW_SETTLE_S`) before taking the image it reports, so moving a zoomed region
  costs about that long (0.46 s measured; 0.8 s when the zoom level changes too).
- **Starting and stopping:** `start_preview()` probes the widgets it needs by name
  and `stop_preview()` writes one widget at a time, so neither reads the whole config
  tree. A start takes 0.95–1.1 s, the first in a session included, and a stop 0.6 s.
  Before, a start that read the tree took 4.0 s, and a stop that wrote two widgets
  in one call 3.6 s, since that reads the tree first. **verified**. On a camera
  driver without single-widget reads, the probe reads the tree once.
- **Restore:** `stop_preview()` and `close()` put `liveviewsize` and
  `liveviewzoomarea` back as found. Both read as read-only settings on this body.
- **Resolution:** full resolution is only available zoomed. `1024` at `XGA` is about one
  image pixel per sensor pixel; the whole image area is at most 1024 px wide.

Checked on the camera (2026-11-10, macOS). **verified**:

- In 16:9, 5:4 and 1:1, for regions of 0.3, 0.2, 0.15, 0.1 and 0.05 of the area, the
  region the driver reported was within 0.003 of where the frame matched in an
  unzoomed frame. In DX it was within 0.006, and 0.01 for the 0.05 regions, where
  the match itself varied by that much between runs.
- The hardware tests also passed in DX, 5:4 and 1:1, with more regions skipped.
- In FX, the hardware tests found each zoomed frame within 1 pixel of an unzoomed
  XGA frame (0.0015 of it) of its reported region, at zooms `2048`, `1024` and `512`,
  including a region against the top edge. Regions over plain wall or repeating mesh
  couldn't be placed and were skipped.
- The size and zoom were back at `VGA` and `0` after `stop_preview()`, and a capture
  worked after a zoomed preview.
- A preview asked for with `not_before` after a focus step came back stamped after it.

DX, 5:4 and 1:1 are checked against the measurements below in the driver's tests, not
on the camera.

Other bodies, and a Z7 II without `imagesize`, keep their live-view settings writable
and are cropped on the host.

**Image areas** (macOS, firmware V1.62, 2026-11-10, `z7_live_view.py area` once per
area, matched against an FX frame with `z7_afmap_match.py --reference`). The camera has
no widget for the image area itself: it's set on the body (Photo shooting menu, Choose
image area), and shows over USB only as the sizes `imagesize` and `rawimagesize`
offer. **verified**:

| Image area | `imagesize` choices | On the sensor | Unzoomed `XGA` frame |
|---|---|---|---|
| FX | `8256x5504`, `6192x4128`, `4128x2752` | all of it | 1024 × 680 |
| DX | `5408x3600`, `4048x2696`, `2704x1800` | centred, 0.655 of the width and height | 1024 × 680 |
| 5:4 | `6880x5504`, `5152x4120`, `3440x2752` | centred, 0.833 of the width | 960 × 768 |
| 1:1 | `5504x5504`, `4128x4128`, `2752x2752` | centred, 0.667 of the width | 768 × 768 |
| 16:9 | `8256x4640`, `6192x3480`, `4128x2320` | centred, 0.843 of the height | 1024 × 576 |

- Unzoomed live view shows the image area, fitted in a 4:3 box (1024 × 768 at `XGA`).
- A zoomed frame is always 1024 × 680 at `XGA`, and the crop is always
  `liveviewzoomarea` sensor pixels wide at 3:2, whatever the area's shape: `2048`
  covered 0.246 of the full sensor's width and height in every area.
- `changeafarea` counts pixels **from the image area's top-left corner**, not the
  sensor's. `2064x1376` centred the crop at (0.25, 0.25) of the full sensor in FX,
  (0.42, 0.42) in DX, (0.33, 0.25) in 5:4 and (0.42, 0.25) in 1:1.
- The crop is clamped to the **image area**: `6192x4128` at `2048` stopped at the
  area's far edge in DX, 5:4, 1:1 and 16:9.
- Eleven centres measured across the four cropped areas fit this within 0.005 of the
  sensor. Points over the blank middle of the target couldn't be matched.
- The size choices also set the JPEG's pixel size (large, medium, small). The driver
  uses only the largest, which is the area itself.

- **Best effort.** Most bodies return previews without live view being switched on
  explicitly, so a failure to enable it is logged as a warning and not retried until the
  next `open()`. **assumed**, from the prototype.
- `close()` turns live view off again if the driver turned it on, so the sensor isn't
  left running. A failure there is logged, not raised.
- Preview images are JPEG on Nikon and Canon bodies. On the Z7 II, previews come back
  without `viewfinder` being set, so libgphoto2 starts live view itself. **verified**.
- **Z7 II displays during live view** (macOS, firmware V1.62, libgphoto2 2.5 via
  python-gphoto2, 2026-11-10, `z7_live_view.py viewfinder`). **verified**:
  - At open, `viewfinder` reads `0` and the rear screen and EVF show the scene.
  - The first `capture_preview()` starts live view on the host: `viewfinder` then reads
    `1`, and both displays show "Connected to PC" and go blank. The first preview took
    0.38 s, later ones 15–70 ms.
  - Live view stays on with no previews (10 s idle); the displays stay blank.
  - Writing `viewfinder=1` or `viewfinder=0` changes neither display. `0` reads back
    `0`, but the next preview sets it to `1` again.
  - Closing the session (`gp_camera_exit`) brings both displays back on their own.
  - So the operator loses the camera's screens from the first preview until `close()`,
    and `viewfinder=0` doesn't give them back. Whether `viewfinder=0` stops the sensor
    (heat, power) is **unknown**.
- **Z7 II sizes and zoom**, both native settings (`dslr-liveview-linux-1.log`):

  | `liveviewsize` | Frame | JPEG |
  |---|---|---|
  | `QVGA` | 320×212 | 4–8 KB |
  | `VGA` (default) | 640×424 | 13–20 KB |
  | `XGA` | 1024×680 | 27–40 KB |

  `liveviewzoomarea` (`0`, `512`, `1024`, `2048`) magnifies without changing the frame
  size: `0` is the whole sensor, `512` the tightest crop, `2048` the loosest. A zoomed
  XGA frame is the one to score for sharpness. **verified**. The first preview after
  `open()` takes 4–7 s, including a settings read and live-view start.
- **Z7 II zoom geometry** (macOS, 2026-11-10, `z7_live_view.py afmap` and
  `z7_afmap_match.py`, which finds each zoomed XGA frame in a full-sensor XGA frame by
  template matching). **verified** unless marked:
  - The crop is `liveviewzoomarea` **sensor pixels wide**, at the sensor's 3:2 aspect:
    `2048` showed 0.248 of the sensor's width and height (2048 / 8256), `1024` 0.124,
    `512` 0.062. So at XGA (1024 px wide), `1024` is about one frame pixel per sensor
    pixel, `2048` halves, and `512` upsamples by 2. That one-to-one at `1024` is
    **inferred** from the sizes, not measured.
  - **`changeafarea` sets the crop's centre**, as `"<x>x<y>"` in pixels of the image
    area (8256 × 5504 in FX, where this was measured; see Image areas above): `2064x2752` centred the crop at (0.25, 0.50) of the frame,
    `6192x5504` at (0.75, near the bottom), for every zoom level. Near an edge the
    crop is clamped to stay on the sensor (`0x0` gave a crop at the top-left corner).
  - `changeafarea` is write-only in effect: it reads back `0x0` after every write.
    Before any write, the crop sat off-centre (at the AF point, **assumed**), so a
    driver should write the centre every time it zooms.
  - `changeafarea` writes fail with `[-1] Unspecified error` until live view is
    running, so take a preview first.
  - **A centre written while the zoom is still changing is misplaced** in the smaller
    image areas. In DX, entering zoom from the unzoomed view, the write blocked for
    0.1–0.4 s and the crop landed about 1.5 times as far from the middle as asked
    (0.75 gave 0.89), as if the centre were in FX pixels. It never corrected itself.
    A second write straight after returned in 0.01 s and landed, in 12 of 12 trials
    over three zoom levels; so did one write 0.5 s after the zoom write, but 0.2 s
    was too soon on a session's first zoom. Moving between zoom levels didn't show
    it. FX hides it, since the two scales are the same there.
  - A size or zoom write shows in the frames 0.2–0.4 s later; previews taken sooner
    still show the old setting, so discard frames for about 0.4 s after a change.
  - QVGA frames are 320×212 at zoom `0` and 320×216 when zoomed. VGA and XGA keep
    their size.
- **Repeated frames.** Previews come back every 10–30 ms, faster than the camera
  makes new ones: about one in four or five has the same bytes as the one before.
  **verified** (`z7_live_view.py settle`, 2026-11-10).
- **After a focus step.** With `step(±2000)` at XGA, zoom `1024` on the centre,
  sharpness moved between about 65 and 51, and the first preview requested after
  `step()` returned already showed the new focus, in 4 of 4 rounds. `step()` waits
  `focus_settle_s()` before returning (1.1 s for 2000 steps), so a preview requested
  after it never showed the old lens position. Whether a preview during that wait
  would is **unknown**. **verified** (`z7_live_view.py settle --zoom 1024`).
- **Hold the `CameraFile` until its bytes are copied.** `get_data_and_size()` returns a
  view of the file's buffer that doesn't keep the file alive. Copying from a temporary
  (`bytes(camera.capture_preview().get_data_and_size())`) reads freed memory: on Linux
  aarch64 the first 32 bytes of every Z7 II preview were glibc heap pointers, and Pillow
  couldn't decode them. **verified** (`dslr-preview-linux-1.log`).

## Keep-alive

While a camera is open, an internal daemon thread reads one event (a 10 ms
`wait_for_event`) whenever the camera has been idle for `KEEP_ALIVE_INTERVAL_S` (2 s).
The prototype added this because multi-second gaps with no USB traffic, such as a
turntable move plus light settling between captures, were followed by the camera
leaving PC mode. **assumed**, from the prototype. With it, the Z7 II captured normally
after 10 s idle. **verified**. Whether the Z7 II would drop out without it is
**unknown**; a capture after a long idle gap with the keep-alive disabled would settle
it.

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
closes with the camera. Opening a drive you hold while the camera is closed opens the
camera, and closing the drive then closes the camera too, but only in that case. Writing a
number to the widget drives the lens motor by that many steps. Canon bodies have a
choice-type `manualfocusdrive` (`Near 1`–`3`, `Far 1`–`3`), which this driver doesn't
support.

The driver's handling of busy and end-stop errors comes from `usb-dslr-capture-demo`
(`origin/ankan:capture-gui.py`), tested on a Sony a6300. The Z7 II checks used a Nikkor
Z 24–120 mm f/4 (`dslr-focus-linux-3.log`, `gphoto2-hw-linux-3.log`).

- **Direction**: positive steps move toward far (infinity), negative toward near.
  **verified** on the Z7 II: in a sweep between the end stops, the frame at the end
  reached by positive moves has the background sharp and the foreground blurred, and
  the frame at the other end the reverse (`focus-linux-3/sweep-00.jpg`,
  `sweep-30.jpg`).
- **End stops**: a move into an end stop fails with `-113` (`GP_ERROR_CAMERA_ERROR`). The
  driver raises `FocusLimitReached` for `-113`, choosing near or far from the direction
  of the move. `-113` is libgphoto2's generic camera error, so on a body that reports
  end stops differently, or fails with `-113` for another reason, this would misreport.
  **verified** on the Z7 II at both stops: a further move into the stop fails again, and
  a move away from it works.
- **After an end stop**, the camera queues events about the failed move. The driver
  reads them and waits `FOCUS_LIMIT_RECOVERY_S` (1 s) before the next command, which is
  what kept the a6300 responsive. **assumed** to be needed on the Z7 II.
- **Busy**: a move sent while the motor is still turning fails with `-110`
  (`GP_ERROR_CAMERA_BUSY`). The driver retries up to `FOCUS_BUSY_ATTEMPTS` (15) times,
  `FOCUS_BUSY_WAIT_S` (0.5 s) apart, then raises `DeviceBusy`. The Z7 II is never busy:
  libgphoto2 waits for each move to finish before returning, so back-to-back 2000-step
  moves all worked. **verified**.
- **Settle time**: after each write the driver waits 100 ms + 0.5 ms per step, capped at
  3 s (`focus_settle_s()`). Since libgphoto2 already waits for each move on the Z7 II,
  whether the wait is needed there is **unknown**; a sweep with no wait that still gives
  a smooth sharpness curve would settle it.
- **Chunks**: `step(n)` writes at most `FOCUS_CHUNK_STEPS` (1000) steps at a time, and
  never more than the widget's range allows (±32767 on the Z7 II). `drive_to_limit()`
  writes chunks as large as the widget's range allows until it hits the end stop, and
  gives up after `FOCUS_LIMIT_MAX_STEPS` (1,000,000).
- **Step size**: on the Z7 II with the 24–120 mm, near to far is about 57,000 steps
  (57 moves of 1000). 57 moves take 42 s, about 0.15 s per move plus the 0.6 s settle
  wait. Driving to the near stop takes 1.4–4.4 s. A 30-point sweep from far to near gave
  a smooth sharpness curve (variance of the Laplacian) with one peak, so steps look
  consistent within a direction. **verified**. Whether steps match in both directions
  (no backlash) is **unknown**. The a6300's range was never measured.
- **The camera must be in AF.** With `focusmode` at `Manual` (or the lens switched to
  M), every `manualfocusdrive` write on the Z7 II fails with `-1` after about 3.7 s, with
  live view on or off. With `focusmode` `AF-S` and `liveviewaffocus` `Single-servo AF`,
  the same moves work. A lens with no focus motor (a 60 mm, on the second body) also
  fails every move with `-1`, so `-1` alone can't tell the two apart. **verified**. The
  driver doesn't change the focus mode: settings made on the camera are honored, so its
  `-1` error names both causes instead. `manualfocusdrive` is listed either way, so
  `camera.focus` isn't `None` with a lens that can't drive focus.
- **Live view**: the a6300 code paused live view while seeking a limit, to avoid
  deadlocks. Focus moves here hold the camera's lock, so a preview can't interleave with
  them. The Z7 II drives focus with live view on or off, including the sweep above,
  which grabbed frames between moves. **verified**.

## Nikon Z7 II

The lab's reference body.

- **Exposure**: set the mode dial to M so `shutterspeed`, `iso`, and `f-number` are all
  writable. In other modes the camera owns some of them and rejects writes. **assumed**,
  from libgphoto2's Nikon driver; all checks ran in M.
- **Widgets the driver uses**: `shutterspeed` (exposure time), `iso`, `f-number`,
  `viewfinder` (live view), and `manualfocusdrive` (focus drive, range type).
  `imagequality` chooses among JPEG Basic, Normal, and Fine (each also with `*`), NEF
  (Raw), and NEF + each JPEG quality: 13 choices. Focus needs `focusmode` `AF-S` (see
  [Focus drive](#focus-drive)). **verified**.
- **Leaving PC mode**: the prototype saw the Z7 II drop out of PC mode when its event
  buffer filled (events never read), with an async capture loop, and after quiet gaps of
  several seconds. **assumed**, from the prototype. The driver drains events around
  every capture, uses the blocking capture, and keeps the session alive; it never
  dropped out of PC mode in the bench runs. After a drop, the next call fails with `-52`.
- **Error codes seen**: `-53` with `ptpcamerad` running on macOS; `-1` for a focus move
  in manual focus or with a lens that has no focus motor; `-113` at a focus end stop.
  **verified**. The prototype also saw `-105` while re-enumerating, `-52` after leaving
  PC mode, `-1` for a download right after a long exposure, and `-10` for a port wedged
  by repeated `ptpcamerad` kills. **assumed**.

## Hazards

- **Discarded files under `Internal RAM`.** A file that arrives after its capture has
  returned is logged and not downloaded; with `capturetarget` set to `Internal RAM` (the
  Z7 II's state on open) it is then gone. Set `Memory card` if every file must survive.
- **`-113` is a generic error.** The focus drive treats it as an end stop. If a body
  returns `-113` for another failure, `FocusLimitReached` is wrong and the step count
  resets to a limit the lens isn't at.
- **A bare `-1` from a focus move** means manual focus or a lens with no focus motor on
  the Z7 II, and each one takes about 3.7 s to come back.
- **Stale ports.** A port from `discover()` is only good until the camera re-enumerates.
- **Killing `ptpcamerad` repeatedly** can wedge the USB port for about 25 s per claim.

## Unknowns

- Whether `-52` is what an explicit stale port returns.
- The Z7 II's exact Bulb and Time texts.
- Whether files reach the card with `capturetarget` at `Memory card`.
- Whether the focus settle time is needed on the Z7 II, and whether focus steps match in
  both directions.
- Whether live-view frames 15–21 ms apart are new frames or repeats.
- Whether the Z7 II needs the keep-alive, and how many failed pings an unplug produces.

## Manual check

`el-devices dslr` opens the camera (`--port`, or the only one attached), lists its
settings and whether it has a focus drive, then runs whichever checks are asked for:
`--exposure SECONDS` sets the nearest shutter speed, `--capture DIR` captures once and
saves every file under the camera's names (creating `DIR` if needed, refusing to
overwrite), `--preview [FRAMES]` previews (with `--region X,Y,W,H` and `--max-size PIXELS`) and
prints the region and size the camera applied and the frame rate, and
`--focus-steps N` moves focus, reporting a limit if it hits one. Add `-v`
(`el-devices -v dslr …`) to log libgphoto2's traffic. The `hardware`-marked tests in
`tests/test_gphoto2_hardware.py` cover identity, settings, exposure, Bulb, capture (RAW
and RAW + JPEG, repeated, after an idle gap), preview (whole sensor, zoomed regions
against an unzoomed frame, restore, capture afterwards, `not_before` after a focus
step), aperture with live view, and focus. The zoom tests need a still scene with
distinct shapes all over the frame.

Run 2026-11-10 on macOS 27 (Python 3.12, firmware V1.62), image area FX: 19 passed, 7
skipped (Bulb, which needs the shutter at Bulb, and six zoom regions over plain wall).

Last run 2026-10-20 on Linux aarch64 (Python 3.14, python-gphoto2 2.6.4, libgphoto2
2.5.34), Z7 II `…03057001` with a Nikkor Z 24–120 mm f/4 in M and AF-S:
`el-devices dslr --exposure … --preview --capture … --focus-steps 2000` captured a JPEG,
returned a 640×424 preview, and moved focus. All 13 hardware tests passed (12 in the full
run, Bulb separately with the shutter at Bulb; `gphoto2-hw-linux-3.log`,
`gphoto2-hw-bulb-linux-1.log`).
