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
