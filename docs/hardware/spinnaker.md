# Spinnaker cameras (FLIR / Teledyne)

Machine-vision cameras driven through Teledyne FLIR's Spinnaker SDK and its Python
bindings, PySpin. The lab uses them in the `pgs-scan` rigs (several cameras capturing
together). Driver: `educelab.devices.cameras.spinnaker`.

> Checked on one rig of Chameleon3 CM3-U3-50S5M cameras (2026-10-16 and 2026-10-19; see
> Last run). Facts marked **unknown** or assumed are still open; the rig check (Manual
> check) settles them.

## Identity

- **Models:** the first rig checked has five Chameleon3 CM3-U3-50S5M (mono, USB3),
  firmware 1.12.3.00, serials 20305113, 20298452, 20305122, 20305114, 20298479 (verified
  2026-10-16). Other `pgs-scan` rigs may have Blackfly S bodies (assumed).
- **On the host:** cameras are enumerated by the SDK, not by a serial port or path. Each
  camera reports `DeviceSerialNumber`, `DeviceModelName`, and `DeviceVendorName` on its
  transport-layer node map (`cam.TLDevice`), which can be read **without** initializing
  the camera (assumed, from `pgs-scan` `dev`). The serial number is how the driver
  addresses a camera.

## Installing PySpin

PySpin is not on PyPI, so `educelab-devices` has no extra for it. You need three things
from the same Spinnaker release: the SDK, its system dependencies, and its PySpin wheel.

### Versions

- **Spinnaker SDK 4.4.0 or newer.** `SpinnakerSystem.open()` (and so every camera and
  `discover()`) raises `ImportError` naming the installed and required versions if the
  SDK is older (`MIN_SPINNAKER_VERSION`, read from `System.GetLibraryVersion()`). 4.4.0
  is the only release the driver's PySpin names were checked against (4.4.0.246,
  verified 2026-10-16), and the first with Python 3.12 wheels for macOS and Windows
  (release notes, 4.4.0.246: "Added PySpin 3.12 support for macOS and Windows").
- **Python 3.12.** PySpin ships cp310 and cp312 wheels, and this package requires 3.11
  or newer, so 3.12 is the only overlap.
- **numpy 2.** The 4.4.0.246 cp312 wheel declares `Requires-Dist: numpy >=2.0` (verified
  in the macOS arm64 wheel's metadata, 2026-10-16; assumed the same for the Linux and
  Windows cp312 wheels). PySpin gained numpy 2 support in 4.2.0.46; 4.3.0.189 moved the
  cp310 wheel back to numpy 1.x, which doesn't affect 3.12. `educelab-devices` itself
  requires numpy 2 or newer.
- **The wheel's version must match the SDK's exactly** (Teledyne's PySpin install guide:
  on Linux, "the wheel version must match the installed Debian package version").

Why not older SDKs: supporting them would need fallbacks the driver doesn't have.

| SDK | What would break |
|---|---|
| before 4.4.0 on macOS or Windows | No Python 3.12 wheel |
| before 4.2.0.46 | No numpy 2 support |
| before 3.0.0.118 | Enums lacked the `SPINNAKER_` prefix ("Updated Spinnaker C++ enums with SPINNAKER prefix", 3.0.0.118), so the image status was `IMAGE_MISSING_TRAILER` (as `pgs-scan` `dev` still uses). This is assumed to be when the status names changed; the notes don't name the image status enum |
| unknown | `Image_GetImageStatusDescription()` (used by `pgs-scan` `dev`) is gone; 4.4.0 has `Image.GetImageStatusDescription()` and no SWIG-flattened `Class_method` functions at all. Which release changed this isn't in the release notes |

### macOS (Apple Silicon)

Spinnaker 4.x supports Apple Silicon only; Intel Macs need 3.2 (release notes,
4.1.0.172). 4.4.0 deprecates macOS 13.2 and older. Install the dependencies with
Homebrew first:

```bash
brew install pkgconf ffmpeg@6 libomp libusb
```

- **ffmpeg must be 6.x.** The SDK's `libSpinVideo` links
  `/opt/homebrew/opt/ffmpeg@6/lib/libavcodec.60.dylib` and its siblings by absolute path,
  and `libSpinnaker` links `/opt/homebrew/opt/libomp/lib/libomp.dylib` and
  `/opt/homebrew/lib/libusb-1.0.0.dylib` (verified with `otool -L`, SDK 4.4.0.246,
  2026-10-16). So `ffmpeg@6` needs no `brew link`, can sit alongside a newer `ffmpeg`,
  and Homebrew must be at its default `/opt/homebrew` prefix.
- **pkgconf** (Homebrew's `pkg-config`) is reported as needed by the maintainer. PySpin
  4.4.0.246 imported on a Mac without it, so it may only be needed by the SDK installer
  or for building the C++ examples (unverified).

Then install the SDK package and the wheel:

```bash
python3.12 -m venv .venv
.venv/bin/pip install "educelab-devices @ git+https://github.com/educelab/educelab-devices"
.venv/bin/pip install ./spinnaker_python-4.4.0.*-cp312-cp312-macosx_*_arm64.whl
.venv/bin/python -c "import PySpin; v = PySpin.System.GetInstance().GetLibraryVersion(); print(v.major, v.minor, v.type, v.build)"
```

### Linux (Ubuntu), unverified on lab hardware

From Teledyne's guides (sources below); 4.4.0 deprecates Ubuntu 18.04 and older:

1. Dependencies (Ubuntu 22.04, from the Bumblebee X guide for SDK 4.1+):
   ```bash
   sudo apt-get install libavcodec58 libavformat58 libswscale5 libswresample3 \
     libavutil56 libusb-1.0-0 libpcre2-16-0 libdouble-conversion3 libxcb-xinput0 \
     libxcb-xinerama0 qtbase5-dev qtchooser qt5-qmake qtbase5-dev-tools
   ```
   The ffmpeg library versions (`libavcodec58` and so on) are Ubuntu 22.04's; other
   releases ship different numbers.
2. Run `sudo sh install_spinnaker.sh` from the extracted SDK. It installs the Debian
   packages and asks whether to:
   - add a udev rule (`/etc/udev/rules.d/40-flir-spinnaker.rules`) that puts FLIR USB
     devices in the `flirimaging` group, and add your user to that group. Without it,
     only root can open the cameras;
   - raise the USB-FS memory limit to 1000 MB. Teledyne: the default "is set to only
     allow streaming up to a total of 2MB over USB3", far below one camera. Since 2.0.0.109 the installer
     can set this persistently. To set it by hand until the next reboot:
     `sudo sh -c 'echo 1000 > /sys/module/usbcore/parameters/usbfs_memory_mb'`.
   Log out and back in (or reboot) for the group change.
3. Install the cp312 Linux wheel from the same release with `pip`.

Kernel 6.8.0-40 and later broke large USB3 Vision transfers until a fix in 4.2.0.21
(release notes), another reason to stay on 4.4.0 or newer.

### Windows, unverified on lab hardware

Run the SDK installer as administrator (3.1.0.79 fixed a failure when not run as
administrator), choosing the USB3 camera drivers. 4.4.0 deprecates 32-bit Windows and
Windows 10 and older, so use 64-bit Windows 11. Then install the
`cp312-cp312-win_amd64` wheel with `pip`. Teledyne's PySpin install guide predates the
3.12 wheel (its example uses Python 3.10). Other camera vendors recommend turning off USB
selective suspend in Power Options when USB3 cameras stop streaming after a while; that
advice is not from Teledyne.

### Sources

- Spinnaker SDK release notes: https://www.teledynevisionsolutions.com/support/support-center/technical-guidance/iis/spinnaker-sdk-release-notes/
- Installing PySpin: https://www.teledynevisionsolutions.com/support/support-center/technical-guidance/iis/installing-pyspin-for-the-spinnaker-sdk/
- Getting Started with the Bumblebee X (Ubuntu 22.04 dependencies): https://www.teledynevisionsolutions.com/support/support-center/application-note/iis/getting-started-with-the-bumblebee-x/
- Using Spinnaker on ARM and Embedded Systems (flirimaging group, USB-FS memory): https://www.teledynevisionsolutions.com/support/support-center/application-note/iis/using-spinnaker-on-arm-and-embedded-systems/

Without PySpin, `educelab.devices.cameras.spinnaker` still imports; the first call that
needs the SDK raises an `ImportError` that links here.

## Transport: the PySpin `System`

- **One `System` per process.** `PySpin.System.GetInstance()` returns a process-wide
  singleton, and `ReleaseInstance()` gives it back. Whether PySpin reference-counts
  repeated `GetInstance()` calls is **unknown**, so the driver never relies on it:
  `SpinnakerSystem` handles share one instance per PySpin module, taking it when the
  first handle opens and releasing it when the last one closes. Each open camera holds
  its own handle.
- **Releasing needs everything else let go first** (assumed, from the PySpin examples and
  `pgs-scan`): every `CameraList` from `GetCameras()` must be `Clear()`ed, every camera
  `DeInit()`ed, and every Python reference to a camera dropped (`del cam`, including a
  leftover `for` loop variable). Otherwise `ReleaseInstance()` raises
  `SpinnakerException` (`-1004`, resource in use).
- **Enumeration doesn't initialize cameras.** `SpinnakerSystem.discover()` reads the
  transport-layer identity nodes, then clears the list once, after visiting every camera.

## Protocol

### Acquisition

**Decided 2026-10-15, because it is the fastest way to capture; to be verified by the rig
check.** A camera acquires continuously from `open()` to `close()`, as in `pgs-scan`
`dev`:

1. `open()`: `Init()`, then `StreamBufferHandlingMode=NewestOnly` (on the stream node
   map), `AcquisitionMode=Continuous`, `TriggerMode=Off` (a camera left in trigger mode,
   e.g. by SpinView, would never send a frame), then `BeginAcquisition()`.
2. Each capture: `GetNextImage()`, rejecting frames older than the request (see
   Timestamps) and incomplete frames, and releasing every buffer.
3. `close()`: `EndAcquisition()`, then `DeInit()`.

With `NewestOnly`, the first frame a capture gets is often one exposed before the
request, so rejecting stale frames is the main mechanism, not a safeguard. The cost of
free-running is USB bandwidth while several cameras stream at once (see USB topology
under Timing).

`PixelFormat` is read-only while acquiring (assumed, standard GenICam behavior), so
setting `pixel_format` ends acquisition, changes the format, and begins again.

#### Software trigger (opt-in)

**Decided 2026-10-19:** free-running stays the default, and the `trigger` setting
switches a camera to software triggering, so nothing streams between captures.
**Measured 2026-10-20** (rig step 10, Last run): with four cameras on one link, each
capture of all four took a median of 147 ms at minimum exposure, against about 250 ms
free-running, with no stale frames and no missing trailers. Exposure began 4–13 ms
after the request.

- **Switching:** `trigger = "Software"` ends acquisition, sets `TriggerMode=Off`,
  `TriggerSelector=FrameStart` (if it isn't already), `TriggerSource=Software`, then
  `TriggerMode=On`, and begins again, as Spinnaker's Trigger example does
  (`TriggerSource` is only writable while the mode is Off). `"Off"` sets
  `TriggerMode=Off`. `open()` always starts free-running. Node names and enum entries
  checked against PySpin 4.4.0.246 by introspection (2026-10-19), and on the
  CM3-U3-50S5M (2026-10-20).
- **Each capture:** releases any frames already waiting in the buffer (with
  `GetNextImage(EVENT_TIMEOUT_NONE)` until it times out; that an empty buffer raises
  `-1011` at once is assumed), executes
  `TriggerSoftware`, and takes the next frame. A frame whose exposure began more than
  1 ms (`_TRIGGER_TOLERANCE_S`, room for offset error) before the trigger, such as one
  from an earlier capture that timed out, counts as stale, so no frame from before the
  trigger is returned. After a stale or incomplete frame it fires again. `not_before` is
  still honoured: the trigger fires after any wait for it.
- **Missed triggers:** a camera can ignore a trigger. On the CM3-U3-50S5M, a capture
  made right after switching to `"Software"` sometimes got no frame and timed out after
  5 s (2026-10-20, hardware test; that the camera wasn't yet ready is assumed). So a
  capture waits only the exposure plus three frame periods (`_RETRIGGER_PERIODS`, from
  `AcquisitionResultingFrameRate`; 1 s if the camera lacks it) for a frame, then fires
  again, until its timeout runs out. The metadata counts these as `retriggers`. If the
  first trigger's frame arrives late after all, the next capture discards it.
- **Several cameras:** `camera.trigger()` fires now and the next capture collects that
  frame, so a caller can fire every camera before collecting (as `el-devices spinnaker
  --trigger software` does). If that trigger is older than the capture's `not_before`,
  the capture fires again.
- `TriggerActivation` only applies to hardware lines, and `TriggerOverlap` is left as
  the camera has it. With overlap Off, a trigger during readout is ignored; a capture
  waits for its frame before the next one fires, so that only matters after a retry.
- With the start-of-exposure timestamp (Timestamps), a triggered frame's `timestamp` is
  after the trigger by the camera's trigger latency.

### Settings

Every setting, its node, and its unit are in the table under
[Differences from PySpin and SpinView](#differences-from-pyspin-and-spinview). Each is
listed only if the camera has a readable node.

**`throughput_limit`** caps the bandwidth the camera streams over USB, so several
cameras can share one link (see USB topology under Timing). In SDK 4.4.0 the node is a
`GenApi::IInteger` that "Limits the maximum bandwidth(in bps) of the data that will be
streamed out by the device", in Spinnaker's `Camera.h`.

- **Units:** the setting is in MB/s, where MB is 10^6 bytes, as in `el-devices
  spinnaker`'s reports. The node counts **bytes** per second, although the header says
  "bps" (verified on the CM3-U3-50S5M, 2026-10-19: capped at 80.384 MB/s, each camera
  streamed 8.0 frames/s of 10 MB Mono16 frames, 80.4 MB/s; bits would give an eighth).
- **Range and rounding:** `minimum`, `maximum`, and `step` come from the node's
  `GetMin()`, `GetMax()`, and `GetInc()`. A written value goes to the nearest
  `minimum + k * step` (half a step rounds to the even `k`, as Python's `round()` does),
  clamped to the range. GenICam integer nodes refuse values off their increment, so the
  driver rounds them itself. `setting.nearest()` gives the value that will be kept.
- **The camera keeps it:** a limit set in one session is still set when the camera is
  next opened, until the camera loses power (verified on the CM3-U3-50S5M, 2026-10-20:
  runs without `--throughput-limit` after one with it reported 80.384 MB/s). `open()`
  doesn't reset it, so set the limit explicitly whenever it matters.
- **Limit mode:** SDK 4.4.0 also has `DeviceLinkThroughputLimitMode` (Off/On), which
  "Controls if the DeviceLinkThroughputLimit is active". On a camera that has it, the
  setting reads `None` while the mode is Off, and writing a value turns the mode On
  first. Whether the limit node is read-only until then is camera-dependent
  (unverified). Turning the mode back off isn't exposed; to remove the cap, set the
  maximum.
- **While acquiring:** the driver writes the limit straight away if the node is
  writable, and otherwise pauses acquisition around the write, as for `pixel_format`.
  Whether any rig camera needs that is unverified.
- **Does the Chameleon3 have it?** Yes (CM3-U3-50S5M, firmware 1.12.3.00, verified
  2026-10-19): 10.048 to 190.912 MB/s in steps of 10.048 on a USB 3 link. On a USB 2
  link the maximum drops to 50.24 MB/s. Other models are unknown; if a camera lacks the
  node, the setting isn't listed, and `el-devices spinnaker --throughput-limit` fails
  naming the camera.

A frame-rate setting (`AcquisitionFrameRateEnable` and `AcquisitionFrameRate` in the
4.4.0 headers) is not exposed. The throughput limit covers the bandwidth problem without
it. It would be worth adding if a rig camera lacks `DeviceLinkThroughputLimit`, after
checking that camera's node names, since older models may use other names.

**Exposure limits** (CM3-U3-50S5M, verified 2026-10-19):

- The **maximum exposure is limited by the frame rate**: 52.27 ms at ~19 frames/s in
  Mono16, and a 0.5 s exposure was rejected with `ValueError`. The driver doesn't expose
  the frame-rate nodes (see [Not exposed](#not-exposed)), so a longer exposure needs a
  lower frame rate set elsewhere, e.g. in SpinView. A lower `throughput_limit` also
  lowers the frame rate; re-read `exposure_time` after changing it.
- The **minimum exposure depends on the pixel format**: 13.35 µs in the format the
  camera was in before the run, and from about 25 µs to 0.126 ms reported in Mono16 (the
  shortest-exposure runs set 0.126 ms and 0.119 ms). `el-devices spinnaker --exposure
  min` sets each camera's minimum for the chosen pixel format.

### Timestamps

- `image.GetTimeStamp()` counts nanoseconds on the camera's own counter, which starts at
  power-on (assumed, from `pgs-scan` and multi_pyspin), not host time.
- To compare a frame with host time, the driver measures an offset: execute
  `TimestampLatch`, read the latched value, and take host time minus the latched value.
  It uses the median of 20 samples (`TIMESTAMP_SAMPLES`; the count comes from
  `pgs-scan` `dev`, which took it from multi_pyspin), with host time taken as the
  midpoint of the latch call. The offset is measured at `open()`, again after every 5
  stale frames in a capture, and on `measure_timestamp_offset()`.
- The latched value is in a node named `Timestamp` on newer cameras and
  `TimestampLatchValue` on older ones. The driver reads `Timestamp` and falls back to
  `TimestampLatchValue`. `el-devices spinnaker` prints which (`timestamp_node`). The
  Chameleon3 CM3-U3-50S5M has `Timestamp` (verified on unit 20305113, firmware
  1.12.3.00, 2026-10-16); other models unknown.
- **The camera's image timestamp marks the end of exposure** (verified on the
  CM3-U3-50S5M, firmware 1.12.3.00, 2026-10-19; other models unknown). One camera,
  Mono16, uncapped at 19 frames/s: latency minus frame age (then measured from the raw
  timestamp) was 56.7 ms at 1 ms exposure and 57.4 ms at 45 ms. A timestamp at the start
  would have added the ~44 ms difference.
- **`Frame.timestamp` is the start of exposure** (decided 2026-10-19): the device
  timestamp plus the offset, minus the `ExposureTime` read at the start of the capture.
  So `not_before` rejects any frame whose exposure began before it, which is what a rig
  wants after moving a stage or changing a light. The raw end-of-exposure counter stays
  in the metadata as `device_timestamp_ns`. Under auto exposure, the exposure read may
  differ slightly from the one the frame used. Without a timestamp offset, the start is
  estimated as host time on arrival minus the exposure.

### Capturing a frame

`capture_frame(not_before=…, timeout=…)` (the `FrameCamera` role's arguments; `not_before`
is an aware `datetime`) first waits until `not_before` if it is in the future, or raises
`DeviceTimeout` at once if it is further away than the timeout. Then it takes the next
frame and checks it:

1. **Incomplete** (`IsIncomplete()`): dropped, except status
   `SPINNAKER_IMAGE_STATUS_MISSING_TRAILER` (`IMAGE_MISSING_TRAILER` before 3.0),
   which is accepted as complete and flagged as `missing_trailer` in the frame's metadata,
   as `pgs-scan` `dev` does. They appear when the USB link is saturated (up to 59 of
   100 captures on one camera, 2026-10-19) and not when every camera is capped at
   80 MB/s. Whether those frames are good is **unknown**: nobody has looked at one yet.
2. **Stale** (offset-corrected timestamp before `not_before`): dropped.
3. Otherwise the pixels are copied out of the buffer.

Every buffer is released whatever the outcome. It keeps taking frames until one passes
or the timeout runs out (`DeviceTimeout`), so the timeout is the bound on retries. After
every 5 stale frames (`RETRIES_BEFORE_REMEASURE`, `pgs-scan`'s limit) it re-measures the
offset. The metadata reports `retries`, `stale_frames`, `incomplete_frames`,
`retriggers` (software trigger only), and `timestamp_remeasured`. `Frame.timestamp` is the start of exposure (Timestamps).

**Expect one stale frame per capture** when free-running. With newest-only buffering,
the frame being exposed or read out when `not_before` is set carries an earlier
timestamp, so it is
dropped and the next one taken: a capture costs about two frame periods. On the
CM3-U3-50S5M at ~35.7 frames/s (28 ms per frame), back-to-back captures took a median
55.9 ms, and 49 of 50 dropped exactly one stale frame; the one that didn't was the
first (verified 2026-10-16). Fewer frames per second, e.g. to share USB bandwidth,
makes each capture proportionally slower.

`preview_frame()` takes the newest frame the same way (no `not_before`; in
software-trigger mode it fires a trigger), shifts 16-bit
pixels right by 8, and subsamples so the longest side is at most 1280 pixels. That
assumes 16-bit formats use the full range (**unknown**: Blackfly S sensors are 10–12
bit, and whether `Mono16` data is left-aligned decides how dark previews look).

## Differences from PySpin and SpinView

The driver is not a thin wrapper. This section lists where it differs from driving the
camera through PySpin or SpinView. If you need raw node access, or something listed under
Not exposed, use PySpin directly; the driver doesn't hide the camera from other programs
once it is closed.

### Setting names and units

| Driver setting | Node | Driver unit → node unit | Differences |
|---|---|---|---|
| `exposure_time` | `ExposureTime` | s → µs (× 10^6) | Read-only while `ExposureAuto` isn't `Off`; range depends on pixel format and frame rate ([Settings](#settings)) |
| `gain` | `Gain` | dB → dB | Read-only while `GainAuto` isn't `Off` |
| `ExposureAuto`, `GainAuto` | same | symbol → symbol | Choices are `Off` and `Continuous`; `Once` is left out |
| `pixel_format` | `PixelFormat` | symbol → symbol | Only Mono8, Mono16, RGB8 and 8/16-bit Bayer; changing it pauses acquisition |
| `throughput_limit` | `DeviceLinkThroughputLimit` | MB/s → bytes/s (× 10^6) | Reads `None` while `DeviceLinkThroughputLimitMode` is Off; writing sets the mode On first ([Settings](#settings)) |
| `current_throughput` | `DeviceLinkCurrentThroughput` | MB/s ← bytes/s (÷ 10^6) | Read-only |
| `resulting_frame_rate` | `AcquisitionResultingFrameRate` | Hz → Hz | Read-only |
| `trigger` | `TriggerMode`, `TriggerSource`, `TriggerSelector` | `Off` → `TriggerMode=Off`; `Software` → `TriggerSelector=FrameStart`, `TriggerSource=Software`, `TriggerMode=On` | Reads `None` for any other trigger setup (e.g. a hardware line); changing it pauses acquisition ([Software trigger](#software-trigger-opt-in)) |

`exposure_time` and `gain` are canonical names shared by every camera. The others
are Spinnaker-only names, with units chosen so they read alike, not the native node names
(decided 2026-10-16).

### Behaviour

- **`open()` changes the camera:** `StreamBufferHandlingMode=NewestOnly`,
  `AcquisitionMode=Continuous`, `TriggerMode=Off`, and `PixelFormat` if the current
  format isn't one of the driver's (RGB8 if offered, else the first supported). It never
  changes exposure, gain, or the throughput limit; they keep what the camera last had.
- **Always acquiring:** `BeginAcquisition()` at `open()`, `EndAcquisition()` at `close()`,
  free-running with newest-only buffering ([Acquisition](#acquisition)), unless
  `trigger` is `Software`; then each capture fires `TriggerSoftware`, or `trigger()`
  fires it ahead.
- **Pauses for some writes:** `pixel_format`, `trigger`, and `throughput_limit` when its node isn't
  writable while acquiring, end acquisition, write, and begin again.
- **Validated, snapped and clamped:** values are checked against the setting's range
  before anything is sent (`ValueError`, `TypeError`). Inside the range, `throughput_limit`
  snaps to the node's increment, and floats are clamped to the node's limits after unit
  conversion, where GenICam would reject an off-increment or out-of-range value.
- **Host-time, start-of-exposure timestamps:** `Frame.timestamp` is the device
  timestamp plus a measured offset (read from `Timestamp` or else
  `TimestampLatchValue`), minus the exposure time, so it marks when exposure began. The
  raw counter, which marks the end of exposure, is in metadata as `device_timestamp_ns`
  ([Timestamps](#timestamps)).
- **`not_before`:** stale frames are dropped and the next one taken, re-measuring the
  offset every 5 stale frames, until the timeout ([Capturing a frame](#capturing-a-frame)).
- **Incomplete frames dropped,** except `SPINNAKER_IMAGE_STATUS_MISSING_TRAILER`, which is
  accepted and flagged as `missing_trailer`.
- **Pixels copied,** and every image buffer released, before a frame is returned.
  `preview_frame()` is 8-bit and subsampled to at most 1280 px.
- **Errors:** every `SpinnakerException` becomes a `DeviceError` with no `__cause__` or
  `__context__` ([Hazards](#hazards)):

  | PySpin | Driver |
  |---|---|
  | `SPINNAKER_ERR_TIMEOUT` (-1011) | `DeviceTimeout`; before the camera's first frame, the message adds that another program (e.g. SpinView) may be streaming from it |
  | `SPINNAKER_ERR_ACCESS_DENIED` (-1005) from `Init()` | `DeviceBusy` |
  | any error writing a setting | `CommandRejected` |
  | no camera with the serial | `DeviceNotFound`, listing the attached serials |
  | anything else | `DeviceError` |

- **One `System` per process,** shared by every handle and camera, released by the last
  to close ([Transport](#transport-the-pyspin-system)).
- **Minimum SDK:** 4.4.0, checked at `open()`; older raises `ImportError`
  ([Versions](#versions)).

### Not exposed

- Frame rate: `AcquisitionFrameRateEnable`, `AcquisitionFrameRate`. The frame rate caps
  the longest exposure ([Settings](#settings)).
- Turning `DeviceLinkThroughputLimitMode` off (set the maximum limit instead).
- `ExposureAuto`/`GainAuto` `Once`, auto-exposure and auto-gain limits.
- Triggering other than software triggering of single frames: hardware lines,
  `TriggerActivation`, `TriggerOverlap`, `TriggerDelay`, and other selectors. `open()`
  sets `TriggerMode=Off`.
- Image geometry and processing: `Width`, `Height`, offsets, binning, black level, gamma,
  white balance, PySpin's `ImageProcessor` conversions, and other pixel formats such as
  packed 12-bit.
- Buffer handling other than newest-only, single-frame or multi-frame acquisition,
  chunk data, user sets, events and callbacks, and GigE settings.
- Raw timestamps on demand (`TimestampLatch`); only the offset is exposed, as
  `timestamp_offset` and `measure_timestamp_offset()`.

## Timing

- **Capture timeout:** the role's default, `FrameCamera.capture_timeout` (30 s), unless
  the caller passes one. `pgs-scan` `dev` allowed a rig capture 2 s. Each
  `GetNextImage()` waits for whatever time is left (`EVENT_TIMEOUT_INFINITE` for
  `math.inf`). Measured latency comes from the rig check.
- **Streaming bandwidth:** every free-running camera sends its full frame rate
  over USB whether or not anyone captures. `el-devices spinnaker` estimates it from frame
  IDs and device timestamps (frames per second times frame size). That assumes
  `GetFrameID()` counts every frame the camera sent, including the ones newest-only
  buffering discarded. The rig runs support it: one CM3-U3-50S5M reported ~35.7
  frames/s and ~179 MB/s at Mono8 2448×2048 (5.0 MB per frame), which matches the
  model's ~35 fps maximum, and the estimates matched the throughput limit (190.8 MB/s
  uncapped against 190.912, 80.4 capped against 80.384). SpinView's stream statistics
  would confirm it (step 5, not run).
- **USB topology on the first rig** (verified 2026-10-16): all five cameras go through
  one Acroname USBHub3p, so they share one upstream USB 3 link of 5 Gbit/s, about
  400 MB/s usable (assumed, the usual figure for USB 3.0).
  - **Saturated with four cameras uncapped** (2026-10-19, Mono16): each streamed
    9.5 frames/s, 95.4 MB/s, about 380 MB/s in all, and two cameras had frames with
    missing trailers. A camera alone streams 190.8 MB/s.
  - **Capping each camera at 80 MB/s** (`throughput_limit` = 80, e.g. `el-devices
    spinnaker --throughput-limit 80`) removed the missing trailers but slowed each camera
    to 8.0 frames/s, adding about 90 ms per capture: the "all cameras per capture" median
    went from 158.5–166.9 ms to 249.3–249.4 ms.
  - A camera that negotiates USB 2 (bad cable or port) streams far less, about 40 MB/s
    and 4 frames/s in Mono16, caps at 50.24 MB/s, and makes every all-camera capture wait
    for it (~499 ms).
  - **Software trigger** (`trigger = "Software"`, decided 2026-10-19 as an opt-in)
    moves data only on request, so it should take about N × frame size / 400 MB/s plus
    the exposure: for four cameras at Mono16 (10 MB), about 100 ms + exposure, against
    the 159–167 ms measured free-running uncapped. Rig step 10 measures it.
  - The Acroname hub track is adding per-port USB 2 and USB 3 limits. They don't help
    here: forcing a port to USB 2 caps a camera far below 80 MB/s.
- **Error codes:** the driver maps `SPINNAKER_ERR_TIMEOUT` (`-1011`) to `DeviceTimeout`
  and `SPINNAKER_ERR_ACCESS_DENIED` (`-1005`) from `Init()` to `DeviceBusy`. The names and
  values are verified in PySpin 4.4.0.246.
- **Another program streaming from the camera** (macOS, SpinView, verified 2026-10-19):
  `Init()` does **not** fail. The camera opens, and the first capture times out with
  `-1011` ("Failed waiting for EventData on NEW_BUFFER_DATA event"), so the driver raises
  `DeviceTimeout`, with a hint about another program when the camera hasn't sent a frame
  since it was opened. When `-1005` occurs is unknown.

## State and recovery

- If `ReleaseInstance()` fails, the driver still forgets the instance and raises
  `DeviceError`, so the next open starts clean. The SDK may still hold the system until
  the process exits (unknown).

## Hazards

- **Clearing a camera list while iterating it** (`pgs-scan` #90): `_remove_cameras` called
  `Clear()` inside the loop over the list, so only the first camera was de-initialized and
  the rest were left initialized, which makes `ReleaseInstance()` fail and is a likely
  cause of "camera busy" on the next launch. The driver clears lists only after the loop.
- **Hidden camera references.** Anything that keeps a camera pointer alive blocks
  `ReleaseInstance()`: a leftover loop variable, but also an exception whose traceback
  passes through a frame holding one, kept alive by a log record or a chained `raise
  ... from`. PySpin's Python wrappers are ordinary methods, so a `SpinnakerException`
  raised by a camera method has that camera in its traceback (assumed). The driver
  indexes camera lists instead of iterating them, logs SDK errors as text, and converts
  them to `DeviceError`s raised outside the `except` block, so they carry no
  `__cause__` or `__context__`. This follows from the assumed rule above; it showed up
  with the fake PySpin in tests (2026-10) and hasn't been seen on a rig.
- **Unreleased image buffers** (`pgs-scan` #83): `pgs-scan` released only the last
  frame of its stale-frame loop. With `NewestOnly` buffering the pool is small, so leaked
  buffers pile up until `GetNextImage()` blocks, which shows up as intermittent capture
  timeouts partway through a long scan. Its loop was also unbounded and skipped the
  incomplete check on the first frame. The driver releases every buffer in a `finally`,
  bounds its retries by the capture timeout, and checks every frame.
- **Using pixels after `Release()`:** `GetNDArray()` is a view of the SDK's buffer, so the
  driver copies it before releasing (assumed, from `pgs-scan`'s `.copy()`).
- **numpy reference-count message at exit:** one `el-devices spinnaker` run ended with
  `*** Reference count error detected: an attempt was made to deallocate the dtype 4 (H)
  ***` (2026-10-20, after 100 Mono16 captures from four cameras, printed after the
  report). numpy prints this when something releases a reference to a built-in dtype
  that it doesn't own, and keeps going. Only one of the eight step 10 runs printed it.
  `GetNDArray()` and `GetData()` on images made with `Image.Create()` don't leak a dtype
  reference over 1000 calls (checked 2026-10-20), so the likeliest source is PySpin
  tearing down at interpreter exit (assumed). The hardware test of 300 captures checks
  that the dtype's reference count doesn't fall during capture.

## Manual check

`el-devices spinnaker` lists the cameras and the SDK version, then opens every camera at
once (so they all stream, as on a rig) and captures from all of them together, each
capture with `not_before` set to the moment it was requested. Per camera it reports
capture latency (request to frame returned), when exposure started relative to the
request (`Frame.timestamp` minus request; runs before 2026-10-19 printed "frame age",
measured from the end of exposure), stale and incomplete frames dropped, frames
accepted with a missing trailer, and the frame rate and MB/s the camera streams.
`--frames 0` only lists. It exits 1 if any capture failed, and reports a setting out
of range as one line naming the camera and the valid range.

- `--exposure min` sets each camera's shortest exposure for the chosen pixel format.
- `--trigger software` switches every camera to software triggering, and each round
  fires every camera's trigger before collecting the frames.
- `--save-missing-trailer DIR` writes each frame accepted with a missing trailer to
  `DIR/<serial>-missing-trailer-<capture>.png`, plus one complete frame per camera,
  `DIR/<serial>-complete.png`, to compare against. PNGs are 8- or 16-bit grayscale (or
  RGB), written without extra dependencies; Bayer frames are saved raw, as grayscale.

```bash
el-devices spinnaker                       # every camera, 20 captures each
el-devices spinnaker --serial 20000001 --frames 50 --exposure 0.01 --pixel-format Mono8
el-devices -v spinnaker --frames 0         # list only, with SDK debug logging
```

The hardware tests are `pytest -m hardware tests/test_spinnaker_hardware.py`. They use
the camera in `SPINNAKER_SERIAL`, or the first attached, and the multi-camera test uses
every camera. They put back any setting they change.

### Rig check steps

Run these on a multi-camera `pgs-scan` rig with Python 3.12, with `pgs-scan` closed (it
would hold the cameras). Record the results in **Last run** below, with the date, host
OS, SDK version, and each camera's model, serial, and firmware.

On every run, pin `--pixel-format` and `--gain`, since the cameras keep whatever the last
program left, and send each run to its own log file (e.g. `... 2>&1 | tee
spin-4-min-80.log`), so a rerun doesn't overwrite an earlier one.

1. **Install.** In a checkout of this repo: `python3.12 -m venv .venv`,
   `.venv/bin/pip install -e . --group dev`, then the PySpin wheel matching the
   installed SDK (Installing PySpin above). Note which USB controller or hub each camera
   is on.
2. **Find the cameras.** `el-devices discover` and `el-devices spinnaker --frames 0`
   should list every camera with its serial and model. Run the second twice in a row: if
   the second run reports a camera as busy, the first didn't release the SDK.
3. **One camera at a time.** For each camera:
   `el-devices spinnaker --serial <serial> --frames 50 --exposure 0.01`, adding
   `--pixel-format RGB8` on color models. Record firmware, the timestamp node, the
   latency and exposure-start medians, the dropped counts, and the streaming frame rate and
   MB/s.
4. **All cameras together, free-running** (see USB topology under Timing). Always pass
   `--gain`, so a gain left on a camera doesn't skew the comparison.
   1. Unthrottled: `el-devices spinnaker --frames 100 --pixel-format Mono16 --gain 10
      --exposure min` (the settings of `pgs-scan`'s `utils/system_check.py`), then again
      at the rig's usual exposure. Record each
      camera's latency median and its incomplete and failed counts, plus the "all
      cameras per capture" median.
   2. Throttled: repeat both runs with `--throughput-limit 80` added, which caps every
      camera at 80 MB/s. Record the same numbers, each camera's streaming frames/s, and
      the "throughput limit" and "resulting frames/s" each camera reports. If the
      command says a camera has no `DeviceLinkThroughputLimit`, set its frame rate to
      about 16 in SpinView instead, close SpinView, rerun without the option, and note
      that.
   3. Compare the "all cameras per capture" medians with the estimate for software
      triggering: the number of cameras × the frame size over ~400 MB/s, plus the
      exposure time (4 × 10 MB at Mono16 is about 100 ms). Step 10 measures it.
   Rising latency, incomplete frames, or failed captures with every camera streaming
   mean the USB link is saturated.
5. **USB bandwidth.** Add up the per-camera MB/s from step 4 per USB controller, and
   compare with what the controller can carry. For one camera, open SpinView's stream
   statistics while `el-devices spinnaker --serial <serial> --frames 200` runs, and
   compare its frame rate and throughput with the command's estimate; that confirms or
   corrects the frame-ID assumption under Timing. If the throttled run in step 4 still
   dropped frames, lower `--throughput-limit` (e.g. 70, then 60), rerun it, and record
   what worked.
6. **Which moment the timestamp marks.** For one camera of each model, run
   `el-devices spinnaker --serial <serial> --frames 50 --pixel-format Mono16 --gain 0
   --throughput-limit 190` with `--exposure 0.001`, then with `--exposure 0.045`. The
   maximum exposure is capped by the frame rate (52 ms at 19 frames/s on the
   CM3-U3-50S5M), so 0.5 s is refused; the full throughput limit undoes any cap left
   from step 4, so the frame rate and readout stay the same in both runs, and gain 0
   keeps the 45 ms frames from saturating. The driver assumes the camera stamps the end
   of exposure and subtracts the exposure, so latency minus the exposure-start median
   is exposure plus readout and transfer: it should rise by about 44 ms between the
   runs. About 88 ms means the camera stamps the start, and the driver is wrong for
   that model. (The 2026-10-19 run used the old "frame age", from the raw timestamp,
   where no jump meant end of exposure.)
7. **Hardware tests.** For one camera of each model:
   `SPINNAKER_SERIAL=<serial> pytest -m hardware -s -v tests/test_spinnaker_hardware.py`.
   `-s` shows the printed timestamp node, offset spread, exposure start, the stale and
   incomplete counts over 300 captures, and the throughput limit's range and the
   resulting frame rate, which settles whether the limit is in bytes or bits, and a
   software-triggered capture.
8. **Another program holding a camera.** Open the camera in SpinView, run
   `el-devices spinnaker --serial <serial> --frames 1`, and record the error. On macOS
   the camera opens and the capture times out (`-1011`) after the 5 s timeout. Close
   SpinView.
9. **Missing trailers.** Rerun the uncapped 20 ms run from step 4, which had the most
   missing trailers, saving them:
   `el-devices spinnaker --serial <s1> --serial <s2> ... --frames 100 --pixel-format
   Mono16 --gain 10 --exposure 0.02 --throughput-limit 190.912 --save-missing-trailer
   missing-trailer 2>&1 | tee spin-9-missing-trailer.log`. Pass the maximum limit
   explicitly: the cameras keep a lower one from an earlier run. Open the PNGs next to each camera's
   `-complete.png` and look for torn, shifted, or partly blank rows, especially at the
   bottom of the frame. Record how many were saved and whether they looked right. That
   settles whether accepting them is safe. The PNGs hold the raw 16-bit values; if
   they look nearly black, the data is right-aligned (see `preview_frame()` above),
   so stretch the contrast in the viewer.
10. **Software trigger versus free-running.** With the same four cameras on USB 3,
    Mono16, gain 10, 100 captures, run each of these with its own log, adding
    `--trigger software` to the free-running runs of step 4:
    - `--exposure min --trigger software` (`spin-10-trig-min.log`)
    - `--exposure 0.02 --trigger software` (`spin-10-trig-20ms.log`)
    - `--exposure min --trigger software --throughput-limit 80`
      (`spin-10-trig-min-80.log`)
    - `--exposure 0.02 --trigger software --throughput-limit 80`
      (`spin-10-trig-20ms-80.log`)

    Rerun step 4's four free-running runs in the same session for a fair comparison.
    Record per camera the latency and exposure-start medians, incomplete, missing
    trailer and failed counts, and the "all cameras per capture" median, against the
    ~100 ms + exposure estimate. Exposure start in trigger mode should be a few
    milliseconds at most (trigger latency).

Settled for the CM3-U3-50S5M (2026-10-19): the throughput limit is in bytes and the model
has it, the timestamp node is `Timestamp`, the camera's timestamp marks the end of
exposure (so the driver subtracts the exposure), and another program streaming shows up
as `-1011`, not `-1005`. On a shared link, software triggering beats free-running
(2026-10-20). Still open: the same facts for other models, the frame-ID assumption
(step 5), and whether frames with missing trailers are good (step 9).

### Last run

**2026-10-20**, same host, SDK and cameras as 2026-10-19. 20305114 negotiated USB 3 this
time (95 MB/s uncapped), so all five cameras shared the hub's one upstream link. Raw
logs: `spin-6c-*.log`, `spin-7b-tests.log`, `spin-9-missing-trailer.log`,
`spin-10-*.log`. Mono16, gain 10, 100 captures per camera, no incomplete frames and no
failed captures in any run.

- **Step 10, software trigger against free-running.** The cameras kept the throughput
  limit from the run before (see Settings), so both "uncapped" trigger runs were in fact
  capped at 80.384 MB/s. The free-running runs came first and are as labelled.

  | Run | All cameras per capture | Latency medians | Exposure start medians | Stale | Missing trailers |
  |---|---|---|---|---|---|
  | Free, min exposure, uncapped | 249.8 ms (158.4–360.2) | 141.0–189.3 ms | 34.8–55.3 ms | 48–134 | 26–66 per camera |
  | Free, 20 ms, uncapped | 291.6 ms (185.4–383.0) | 155.2–233.9 ms | 48.6–74.6 ms | 66–162 | 11–60 per camera |
  | Free, min exposure, 80 MB/s | 249.6 ms (232.9–419.2) | 177.1–244.2 ms | 35.2–113.8 ms | 2–104 | 0 |
  | Free, 20 ms, 80 MB/s | 249.4 ms (244.5–307.9) | 177.0–248.9 ms | 15.2–102.1 ms | 1–102 | 0 |
  | Trigger, min exposure, 80 MB/s (two runs) | 146.6 and 147.9 ms (143.5–150.3) | 119.4–136.3 ms | 2.3–10.4 ms | 0 | 0 |
  | Trigger, 20 ms, 80 MB/s (two runs) | 170.9 and 172.2 ms (165.4–175.8) | 139.7–160.4 ms | 1.8–10.3 ms | 0 | 0 |

  Triggering is about 100 ms faster per capture than free-running, varies by a few
  milliseconds instead of a hundred or more, and drops no frames, in line with the
  ~100 ms + exposure estimate. Exposure start climbs about 2 ms per camera in the order
  the CLI fires them (2.4, 4.6, 6.7, 8.5, 10.4 ms), so each `TriggerSoftware` call takes
  about 2 ms. Uncapped triggering wasn't measured.
- **Step 9:** inconclusive. The run inherited the 80 MB/s limit, so no frame had a missing
  trailer; only the five `-complete.png` reference frames were saved.
- **Step 6** (again, as `spin-6c`), 20305113 alone, uncapped, 50 captures: exposure start
  median 48.1 ms after the request at 1 ms exposure and 3.9 ms at 45 ms, so the
  start-of-exposure timestamp behaves as intended.
- **Step 7,** 20305113: 15 of 16 hardware tests passed in 56.9 s. Offset spread 37 µs; 300
  captures: 300 stale, 0 incomplete, 0 re-measured. `test_software_trigger_captures_a_fresh_frame`
  failed: a capture right after switching to `"Software"` got no frame in 5 s (`-1011`).
  The driver now fires again when no frame comes (Software trigger, Missed triggers);
  not yet rerun.
- One `spin-10-trig-min` run ended with numpy's reference-count message (Hazards).

### Run of 2026-10-19

**2026-10-19**, macOS (Apple Silicon), Spinnaker SDK 4.4.0.246, PySpin 4.4.0.246 on
Python 3.12. Five Chameleon3 CM3-U3-50S5M, firmware 1.12.3.00, all on one Acroname
USBHub3p (serial 0x1A43352C, one upstream link). Raw logs: `spin-4-*.log`,
`spin-6b-*.log`, `spin-7-tests.log`, `spin-8-busy.log`.
"Frame age" in these runs is the raw (end-of-exposure) timestamp minus the request; the
driver now reports the start of exposure instead.

- **USB 2 camera:** 20305114, on hub port 3, negotiated USB 2 only, and a power cycle
  (`el-devices hub cycle 3`) didn't bring it back to USB 3 (a bad cable is suspected).
  Its throughput limit maximum was 50.24 MB/s against 190.912 on USB 3. It was left out
  of the reruns.
- **Step 4, five cameras** (logs overwritten; numbers from the session), Mono16, gain 10:
  - minimum exposure: 20305114 streamed 4.0 frames/s, 40.2 MB/s, latency median
    498.6 ms; the other four ~9.5 frames/s, 95.4 MB/s each. All cameras per capture:
    median 498.9 ms, set by the USB 2 camera. Missing trailers: 61 on 20298479, 4 on
    20305122;
  - 20 ms: all cameras per capture median 499.0 ms; 11 missing trailers on 20298479;
  - `--throughput-limit 80` was refused: "20305114: throughput_limit=80.0 is above
    50.24".
- **Step 4, four cameras on USB 3** (20305113, 20305122, 20298452, 20298479), Mono16,
  gain 10, 100 captures each. No incomplete frames and no failures in any run.

  | Run | Per camera | Latency medians | All cameras per capture | Missing trailers |
  |---|---|---|---|---|
  | Minimum exposure (0.126 ms), uncapped | 9.5 frames/s, 95.4 MB/s | 108.1–134.9 ms | median 158.5 ms | 1 on 20298479 |
  | 20 ms, uncapped | 9.5 frames/s, 95.4 MB/s | 106.8–166.6 ms | median 166.9 ms | 58 on 20298452, 59 on 20298479 |
  | Minimum exposure (0.119 ms), capped at 80 (80.384 MB/s) | 8.0 frames/s, 80.4 MB/s | 135.1–248.7 ms | median 249.4 ms | 0 |
  | 20 ms, capped at 80 | 8.0 frames/s, 80.4 MB/s | 184.3–248.7 ms | median 249.3 ms | 0 |

  Stale frames: about 51 per 100 captures uncapped, about 101 capped. The estimate for
  software triggering is 4 × 10 MB / ~400 MB/s ≈ 100 ms plus the exposure.
- **Exposure limits:** see [Settings](#settings). The maximum was 52.27 ms at ~19 frames/s
  in Mono16; 0.5 s was rejected with `ValueError` (a traceback, before `el-devices
  spinnaker` reported setting errors on one line).
- **Step 5:** not run.
- **Step 6,** 20305113 alone, Mono16, gain 0, uncapped (19.0 frames/s, 190.8 MB/s),
  50 captures: at 1 ms, latency median 104.9 ms, frame age 48.2 ms, difference 56.7 ms;
  at 45 ms, 104.8 ms, 47.4 ms, 57.4 ms. No ~44 ms jump, so the timestamp marks the end
  of exposure.
- **Step 7,** one camera (serial not logged): 15 of 15 hardware tests passed in 51.1 s. Timestamp node
  `Timestamp`; offset spread 46 µs over 5 measurements; 300 captures: 300 stale,
  0 incomplete, 0 re-measured; throughput limit 10.048–190.912 MB/s, step 10.048.
- **Step 8,** 20305113 open in SpinView: the driver opened the camera (`Init()` didn't
  fail with `-1005`), then the capture failed after the 5 s timeout with
  `DeviceTimeout: ... Failed waiting for EventData on NEW_BUFFER_DATA event. (GenTL error
  code: -1011) [-1011]`.
- **Step 9:** missing trailers were counted (above), but no frame with one has been
  looked at, because `el-devices spinnaker` doesn't save images.

### Run of 2026-10-16

**2026-10-16**, macOS (Apple Silicon), Spinnaker SDK 4.4.0.246, PySpin 4.4.0.246 on
Python 3.12. Five Chameleon3 CM3-U3-50S5M, firmware 1.12.3.00, all on one Acroname
USBHub3p (one upstream USB 3 link).

- **Step 2:** all five listed with serial and model.
- **Step 3, measured,** one camera (20305113), 50 captures, Mono8, 10 ms exposure:
  - timestamp node `Timestamp`; offset 1792171556.358970 s, which puts the camera's
    counter near zero earlier that day, consistent with a counter that starts at
    power-on;
  - gain read 47.99 dB. The command doesn't change gain unless `--gain` is passed and
    `open()` never changes settings, so this was the camera's previous state (assumed
    left by another program). Pass `--gain` on rig runs to get comparable results;
  - latency median 55.9 ms (mean 55.6, min 36.6, max 57.6); frame age median 25.0 ms
    (min 6.8, max 26.2);
  - 49 stale frames dropped in 50 captures, 0 incomplete, 0 missing trailers. That is
    one per capture, as expected (Capturing a frame): latency of about two frame
    periods at 28 ms each;
  - streaming ~35.7 frames/s, ~178.8 MB/s.
- **Step 4, blocked by a driver bug:** every capture thread failed with
  `AttributeError: module 'PySpin' has no attribute 'IMAGE_MISSING_TRAILER'` (165
  tracebacks logged). The driver used the pre-3.0 status name, which PySpin 4.4.0
  calls `SPINNAKER_IMAGE_STATUS_MISSING_TRAILER`. The code was only reached with all
  five cameras streaming, so incomplete frames appeared only then, as expected when
  ~900 MB/s shares a ~400 MB/s link. Fixed: the driver uses 4.4.0's names, a test
  compares the fake with real PySpin, and `el-devices spinnaker` reports any exception
  in a capture thread as that camera's failure. Step 4 was rerun on 2026-10-19.
