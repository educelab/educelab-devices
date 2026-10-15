# Spinnaker cameras (FLIR / Teledyne)

Machine-vision cameras driven through Teledyne FLIR's Spinnaker SDK and its Python
bindings, PySpin. The lab uses them in the `pgs-scan` rigs (several cameras capturing
together). Driver: `educelab.devices.cameras.spinnaker`.

> This doc is being written alongside the `spinnaker-camera` track. Sections marked
> *to come* are filled in by later phases.

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

**Decided 2026-10-15, because it is the fastest way to capture; to be verified on a rig
in Phase 3.** A camera acquires continuously from `open()` to `close()`, as in `pgs-scan`
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
cameras stream at once; Phase 3 measures that and the per-capture latency.

`PixelFormat` is read-only while acquiring (assumed, standard GenICam behavior), so
setting `pixel_format` ends acquisition, changes the format, and begins again.

### Timestamps

- `image.GetTimeStamp()` counts nanoseconds on the camera's own counter, which starts at
  power-on (assumed, from `pgs-scan` and multi_pyspin), not host time.
- To compare a frame with host time, the driver measures an offset: execute
  `TimestampLatch`, read the latched value, and take host time minus the latched value.
  It uses the median of 20 samples (`TIMESTAMP_SAMPLES`; the count comes from
  `pgs-scan` `dev`, which took it from multi_pyspin), with host time taken as the
  midpoint of the latch call. The offset is measured at `open()`, again when a round of
  retries finds stale frames, and on `measure_timestamp_offset()`.
- The latched value is in a node named `Timestamp` on newer cameras and
  `TimestampLatchValue` on older ones. The driver reads `Timestamp` and falls back to
  `TimestampLatchValue`. **Unknown** which one the rig cameras have; `pgs-scan` `dev`
  reads `Timestamp`. Phase 3 records it.
- **Unknown:** whether the image timestamp marks the start or the end of exposure. If
  it is the start, `not_before` rejects any frame whose exposure began before the
  request, which is what a rig wants after moving a stage or changing a light.

### Capturing a frame

`capture_frame(not_before=…)` takes the next frame and checks it:

1. **Incomplete** (`IsIncomplete()`): dropped, except status `IMAGE_MISSING_TRAILER`,
   which is accepted as complete and flagged as `missing_trailer` in the frame's metadata,
   as `pgs-scan` `dev` does (**assumed** harmless until Phase 3 checks those frames).
2. **Stale** (offset-corrected timestamp before `not_before`): dropped.
3. Otherwise the pixels are copied out of the buffer.

Every buffer is released whatever the outcome. After 5 dropped frames
(`RETRIES_BEFORE_REMEASURE`, `pgs-scan`'s limit), if any were stale, the driver
re-measures the offset. It gives up with `DeviceError` after 10 (`MAX_ATTEMPTS`). The
metadata reports `retries`, `stale_frames`, `incomplete_frames`, and
`timestamp_remeasured`.

`preview_frame()` takes the newest frame the same way (no `not_before`), shifts 16-bit
pixels right by 8, and subsamples so the longest side is at most 1280 pixels. That
assumes 16-bit formats use the full range (**unknown**: Blackfly S sensors are 10–12
bit, and whether `Mono16` data is left-aligned decides how dark previews look).

## Timing

- **Capture timeout:** by default the exposure time plus 2 s (`CAPTURE_MARGIN_S`),
  matching the 2 s `pgs-scan` `dev` allowed a rig capture. Deliberately loose. Measured
  latency comes in Phase 3.
- **Error codes** (assumed, from memory of the Spinnaker headers; to confirm in Phase 3):
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
  `__cause__` or `__context__`. This follows from the assumed rule above; it showed up with the fake
  PySpin in tests (2026-10) and hasn't been seen on a rig.
- **Unreleased image buffers** (`pgs-scan` #83): `pgs-scan` released only the last
  frame of its stale-frame loop. With `NewestOnly` buffering the pool is small, so leaked
  buffers pile up until `GetNextImage()` blocks, which shows up as intermittent capture
  timeouts partway through a long scan. Its loop was also unbounded and skipped the
  incomplete check on the first frame. The driver releases every buffer in a `finally`,
  bounds the attempts, and checks every frame.
- **Using pixels after `Release()`:** `GetNDArray()` is a view of the SDK's buffer, so the
  driver copies it before releasing (assumed, from `pgs-scan`'s `.copy()`).

## Manual check

*To come (Phase 3):* `el-devices spinnaker`.
