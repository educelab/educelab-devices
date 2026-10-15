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

**Decided 2026-10-15; to be verified on a rig in Phase 3.** A camera acquires from
`open()` to `close()` in software-trigger mode, so it stays ready but sends a frame over
USB only when one is requested:

1. `open()`: `Init()`, then `TriggerMode=Off`, `TriggerSelector=FrameStart`,
   `TriggerSource=Software`, `TriggerMode=On`, then `BeginAcquisition()`.
   `TriggerSource` is writable only while `TriggerMode` is off (assumed, from FLIR's
   `Trigger.py` example), so the driver turns triggering off first even when the camera
   was left triggered.
2. Each capture: execute `TriggerSoftware`, then `GetNextImage()`.
3. `close()`: `EndAcquisition()`, then `DeInit()`.

`pgs-scan` `dev` instead streamed continuously with `NewestOnly` buffering and threw
away frames older than the request. Triggering should send less over USB with several
cameras on one host and make stale frames rare, at the cost of waiting a full exposure
and readout per capture. Phase 3 measures both: per-capture latency, and USB bandwidth
with several cameras.

The driver still keeps `StreamBufferHandlingMode=NewestOnly`, rejects frames older than
`not_before`, rejects incomplete frames, and releases every buffer, as safeguards. The
trigger setup is two methods (`_configure_acquisition()` and `_request_frame()`), so a
continuous mode could be added without a rewrite.

`PixelFormat` is read-only while acquiring (assumed, standard GenICam behavior), so
setting `pixel_format` ends acquisition, changes the format, and begins again.

## Timing

*To come (Phase 3).*

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
  ... from`. The driver indexes camera lists instead of iterating them, and logs SDK
  errors as text. This follows from the assumed rule above; it showed up with the fake
  PySpin in tests (2026-10) and hasn't been seen on a rig.
- **Unreleased image buffers** (`pgs-scan` #83): see Phase 2.

## Manual check

*To come (Phase 3):* `el-devices spinnaker`.
