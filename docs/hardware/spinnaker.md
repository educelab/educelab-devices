# Spinnaker cameras (FLIR / Teledyne)

Machine-vision cameras driven through Teledyne FLIR's Spinnaker SDK and its Python
bindings, PySpin. The lab uses them in the `pgs-scan` rigs (several cameras capturing
together). Driver: `educelab.devices.cameras.spinnaker`.

> Nothing here has been checked on a rig yet. The rig check (see Manual check) settles
> the facts marked **unknown** and confirms or corrects the ones marked assumed.

## Identity

- **Models:** Blackfly S USB3 bodies on the `pgs-scan` rigs (assumed; exact models to be
  recorded during the rig check).
- **On the host:** cameras are enumerated by the SDK, not by a serial port or path. Each
  camera reports `DeviceSerialNumber`, `DeviceModelName`, and `DeviceVendorName` on its
  transport-layer node map (`cam.TLDevice`), which can be read **without** initializing
  the camera (assumed, from `pgs-scan` `dev`). The serial number is how the driver
  addresses a camera.

## Installing PySpin

PySpin is not on PyPI, so `educelab-devices` has no extra for it.

1. **Python 3.12.** As of Spinnaker SDK 4.4.0, PySpin ships wheels for CPython 3.10 and
   3.12 only, and this package requires 3.11+, so 3.12 is the only overlap.
2. **numpy<2.** PySpin 4.4.0 is built against numpy 1.x. Install `numpy<2` in the
   environment before PySpin. The rest of `educelab-devices` works with numpy 1.26.
3. **Install the Spinnaker SDK** for your OS from Teledyne Vision Solutions (free
   account required). On Linux, the SDK installer also offers to add your user to the
   camera group and raise the USB-FS memory limit; accept both for USB3 cameras
   (assumed, from the vendor's install notes).
4. **Install the PySpin wheel from the same release.** The Python bindings are a separate
   download ("Spinnaker Python") per OS and Python version. Its version must match the
   installed SDK exactly; a mismatched pair can fail at import or at runtime.

```bash
python3.12 -m venv .venv
.venv/bin/pip install "numpy<2" "educelab-devices @ git+https://github.com/educelab/educelab-devices"
.venv/bin/pip install ./spinnaker_python-4.4.0.*-cp312-cp312-*.whl
.venv/bin/python -c "import PySpin; print(PySpin.System.GetInstance().GetLibraryVersion())"
```

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
request, so rejecting stale frames is the main mechanism, not a safeguard. A software
trigger per capture was considered and rejected: it pays for exposure, readout, and
transfer on every capture. The cost of free-running is USB bandwidth while several
cameras stream at once; the rig check measures that and the per-capture latency.

`PixelFormat` is read-only while acquiring (assumed, standard GenICam behavior), so
setting `pixel_format` ends acquisition, changes the format, and begins again.

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
  `TimestampLatchValue`. **Unknown** which one the rig cameras have; `pgs-scan` `dev`
  reads `Timestamp`. `el-devices spinnaker` prints it (`timestamp_node`).
- **Unknown:** whether the image timestamp marks the start or the end of exposure. If
  it is the start, `not_before` rejects any frame whose exposure began before the
  request, which is what a rig wants after moving a stage or changing a light. If it is
  the end, a frame exposed partly before the request passes, and rig code should add the
  exposure time to `not_before`. Manual check step 6 settles it.

### Capturing a frame

`capture_frame(not_before=…, timeout=…)` (the `FrameCamera` role's arguments; `not_before`
is an aware `datetime`) first waits until `not_before` if it is in the future, or raises
`DeviceTimeout` at once if it is further away than the timeout. Then it takes the next
frame and checks it:

1. **Incomplete** (`IsIncomplete()`): dropped, except status `IMAGE_MISSING_TRAILER`,
   which is accepted as complete and flagged as `missing_trailer` in the frame's metadata,
   as `pgs-scan` `dev` does (**assumed** harmless until the rig check looks at those
   frames).
2. **Stale** (offset-corrected timestamp before `not_before`): dropped.
3. Otherwise the pixels are copied out of the buffer.

Every buffer is released whatever the outcome. It keeps taking frames until one passes
or the timeout runs out (`DeviceTimeout`), so the timeout is the bound on retries. After
every 5 stale frames (`RETRIES_BEFORE_REMEASURE`, `pgs-scan`'s limit) it re-measures the
offset. The metadata reports `retries`, `stale_frames`, `incomplete_frames`, and
`timestamp_remeasured`. `Frame.timestamp` is the offset-corrected image timestamp; see
Timestamps for which moment that is.

`preview_frame()` takes the newest frame the same way (no `not_before`), shifts 16-bit
pixels right by 8, and subsamples so the longest side is at most 1280 pixels. That
assumes 16-bit formats use the full range (**unknown**: Blackfly S sensors are 10–12
bit, and whether `Mono16` data is left-aligned decides how dark previews look).

## Timing

- **Capture timeout:** the role's default, `FrameCamera.capture_timeout` (30 s), unless
  the caller passes one. `pgs-scan` `dev` allowed a rig capture 2 s. Each
  `GetNextImage()` waits for whatever time is left (`EVENT_TIMEOUT_INFINITE` for
  `math.inf`). Measured latency comes from the rig check.
- **Streaming bandwidth:** every camera free-runs, so each sends its full frame rate
  over USB whether or not anyone captures. `el-devices spinnaker` estimates it from frame
  IDs and device timestamps (frames per second times frame size). That assumes
  `GetFrameID()` counts every frame the camera sent, including the ones newest-only
  buffering discarded (**unknown**; the rig check compares it with SpinView).
- **Error codes** (assumed, from memory of the Spinnaker headers; the rig check confirms
  them):
  `-1011` is a timeout, mapped to `DeviceTimeout`; `-1005` from `Init()` is access
  denied, mapped to `DeviceBusy` (another process, e.g. SpinView, holds the camera).

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

## Manual check

`el-devices spinnaker` lists the cameras and the SDK version, then opens every camera at
once (so they all stream, as on a rig) and captures from all of them together, each
capture with `not_before` set to the moment it was requested. Per camera it reports
capture latency (request to frame returned), frame age (frame timestamp minus request),
stale and incomplete frames dropped, frames accepted with a missing trailer, and the
frame rate and MB/s the camera streams. `--frames 0` only lists. It exits 1 if any capture
failed.

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

1. **Install.** In a checkout of this repo: `python3.12 -m venv .venv`,
   `.venv/bin/pip install -e . --group dev "numpy<2"`, then the PySpin wheel matching the
   installed SDK (Installing PySpin above). Note which USB controller or hub each camera
   is on.
2. **Find the cameras.** `el-devices discover` and `el-devices spinnaker --frames 0`
   should list every camera with its serial and model. Run the second twice in a row: if
   the second run reports a camera as busy, the first didn't release the SDK.
3. **One camera at a time.** For each camera:
   `el-devices spinnaker --serial <serial> --frames 50 --exposure 0.01`, adding
   `--pixel-format RGB8` on color models. Record firmware, the timestamp node, the
   latency and frame-age medians, the dropped counts, and the streaming frame rate and
   MB/s.
4. **All cameras together, as `pgs-scan` captures.** `el-devices spinnaker --frames 100
   --pixel-format Mono16 --gain 10 --exposure <the shortest exposure the rig uses>`
   (the settings of `pgs-scan`'s `utils/system_check.py`), then again at the rig's usual
   exposure. Compare each camera's latency with step 3. Rising latency,
   incomplete frames, or failed captures with every camera streaming mean the USB link
   is saturated.
5. **USB bandwidth.** Add up the per-camera MB/s from step 4 per USB controller, and
   compare with what the controller can carry. For one camera, open SpinView's stream
   statistics while `el-devices spinnaker --serial <serial> --frames 200` runs, and
   compare its frame rate and throughput with the command's estimate; that confirms or
   corrects the frame-ID assumption under Timing. If step 4 dropped frames, lower
   `DeviceLinkThroughputLimit` (or the frame rate) in SpinView, rerun step 4, and record
   what worked.
6. **Which moment the timestamp marks.** For one camera, run step 3 with `--exposure
   0.001` and again with `--exposure 0.5`. Latency minus frame age is roughly readout
   and transfer time if the timestamp marks the end of exposure, and that plus the
   exposure time if it marks the start. A jump of about 500 ms between the two runs means
   start of exposure.
7. **Hardware tests.** For one camera of each model:
   `SPINNAKER_SERIAL=<serial> pytest -m hardware -s -v tests/test_spinnaker_hardware.py`.
   `-s` shows the printed timestamp node, offset spread, frame age, and the stale and
   incomplete counts over 300 captures.
8. **Another program holding a camera.** Open the camera in SpinView, run
   `el-devices spinnaker --serial <serial> --frames 1`, and record the error. The driver
   expects `DeviceBusy` from error code `-1005`. Close SpinView.
9. **Missing trailers.** If any run reported frames "accepted with a missing trailer",
   say how often, and whether those frames looked right. That settles whether accepting
   them is safe.

Settle in this doc: the timestamp node per model, start or end of exposure, the frame-ID
assumption, the `-1005` and `-1011` codes, how often missing trailers occur, and whether
continuous acquisition fits the rig's USB bandwidth.

### Last run

Not run on hardware yet.
