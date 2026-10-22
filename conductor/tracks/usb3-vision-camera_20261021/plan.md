# Implementation Plan: USB3 Vision Camera

**Track ID:** usb3-vision-camera_20261021
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-21
**Status:** [~] In Progress

## Overview

Settle the open choices and the seam first. Then move the Spinnaker driver onto a shared GenICam core, using its existing tests as the safety net. On that core, build the Aravis backend (Linux/macOS, primary), then the GenTL backend (Windows, secondary), then the CLI, docs, and hardware runs.

## Phase 1: Decisions, ADR and the shared seam

### Tasks

- [x] Task 1.1: Research and record install steps per OS:
  - Aravis + PyGObject on macOS (Homebrew) and Linux, including the udev rule.
  - IDS peak's GenTL producer on Windows and Linux.
  - Whether the IDS producer accepts cameras from other vendors.
  - Harvesters versus calling `genicam` directly.
  - Recorded in `docs/hardware/usb3-vision.md`. Recommendation: call `genicam` directly (Harvesters is unmaintained, and its threads and fetch model don't fit ADR 0001). The IDS producer has refused other vendors' cameras since IDS peak 2.0.
- [x] Task 1.2: Draft ADR 0006 (GenICam cameras share a core with one backend per transport). Needs maintainer approval.
  - Approved 2026-10-21.
- [x] Task 1.3: Decide the names: backends, extras (e.g. `[gentl]`), driver classes, module layout under `cameras/`, and the CLI subcommand.
  - Core and seam: `cameras/_genicam.py`. Backends: `cameras/spinnaker.py`, `cameras/aravis.py`, `cameras/gentl.py`.
  - Classes `SpinnakerCamera`, `AravisCamera`, `GenTLCamera`. `DeviceInfo.backend` is `"spinnaker"`, `"aravis"` or `"gentl"`.
  - Aravis and PyGObject are installed manually, like PySpin, with no extra.
  - CLI: `el-devices genicam --backend aravis|gentl`. The default is Aravis on Linux/macOS and GenTL on Windows.
  - `genicam` becomes a `[gentl]` extra, included in `[all]` (decided 2026-10-22). Its licence isn't OSI-approved, but declaring it as an extra carries the same risk as PySpin.
- [x] Task 1.4: Define the backend seam:
  - Node map: read/write float, int, enum, and command nodes, with their ranges and access mode.
  - Stream: start/stop, grab with a timeout, buffer status, device timestamp, release.
  - Discovery and error translation into `DeviceError` subclasses.
  - Done as `Protocol`s in `cameras/_genicam.py`, following `_gphoto2.py`. Opening a connection sets up newest-only buffering, which each backend does in its own way. A missing serial returns `None`, so the core can name the cameras that are attached.
- [x] Task 1.5: Build an in-memory fake backend that implements the seam, for testing the core on its own.
  - `tests/fakes/genicam.py`: `FakeCamera` simulates an SFNC camera without depending on any library, and `FakeBackend` exposes it through the seam. The Aravis and GenTL fakes will wrap the same `FakeCamera`.
  - `tests/contracts/genicam.py`: `BackendContract`, which every backend must pass on its fake.

### Verification

- [x] ADR approved. The seam and fake backend pass their own tests.

## Phase 2: Move Spinnaker onto the core

### Tasks

- [x] Task 2.1: Write core tests against the fake backend. They cover settings (units, snap/clamp, auto modes), pausing acquisition for some writes, software trigger priming and re-firing, the timestamp offset, the `not_before` loop, and handling of incomplete and missing-trailer frames.
- [x] Task 2.2: Move that logic out of `spinnaker.py` into the core.
- [x] Task 2.3: Implement the PySpin backend on the seam: `System` lifecycle, node access, image status, and exception codes.
  - Deviation: to keep the Spinnaker tests unmodified, `spinnaker.py` still imports `time` and re-exports `_RETRIGGER_TURNAROUNDS`, because the tests reach for both there.
  - Unverified: `is_readable`/`is_writable` pass PySpin an uncast `INode`, where the old code passed typed pointers. Check this in the Phase 2 hardware run.
- [x] Task 2.4: Add probing for optional features (trigger, throughput limit, timestamp node, auto modes, `AcquisitionResultingFrameRate`). Settings for missing features are left out.
  - Deviation: features are checked each time settings are read, through `has()` and `is_readable()`, rather than cached at `open()`. Access changes at runtime (e.g. `ExposureTime` is read-only under auto exposure), so a cache would go stale. `AcquisitionMode` is now optional. Tests cover each feature being absent.

### Verification

- [x] Every existing Spinnaker unit and contract test passes unmodified.
- [ ] `pytest -m hardware` passes on a CM3-U3-50S5M. Outstanding: no PySpin or camera on the dev machine; the maintainer chose to continue to Phase 3 (2026-10-22).
- [x] `docs/hardware/spinnaker.md` is unchanged except for links to the core.

## Phase 3: Aravis backend (Linux, macOS)

### Tasks

- [x] Task 3.1: Build a fake `gi.repository.Aravis` covering discovery, the device feature API, streams and buffers with release tracking, buffer status, and timestamps.
- [x] Task 3.2: Write tests for the lazy import and for the install hint that links to the per-OS steps; implement.
- [x] Task 3.3: Write tests for discovery and open/close by serial, including what `open()` configures; implement.
- [x] Task 3.4: Write tests for the stream (buffer pool, newest-only behaviour, timeouts, incomplete buffers) and error mapping (busy, not found, timeout); implement.
- [x] Task 3.5: Run the `Camera`/`FrameCamera` contract suites against the fake.
  - The Aravis backend opens by serial, turns on Aravis's range and access checks (they're off by default), and keeps a pool of `BUFFER_COUNT` buffers. It allocates a new pool when a pixel-format change alters the payload, and empties the output queue on stop. "Newest only" works by draining frames that have already arrived and pushing the older ones back. `tests/test_fake_aravis.py` checks the fake against real Aravis when it's installed.
  - Assumed until the hardware run (Phase 3 verification):
    - A camera that is listed but won't open is held by another process (`DeviceBusy`).
    - `get_feature_access_mode` reflects locking (e.g. `ExposureTime` under auto).
    - Buffer timestamps are the device counter and aren't 0.
- [x] Task 3.6: Add hardware tests, mirroring the Spinnaker set where it applies.
  - `tests/test_aravis_hardware.py` (`ARAVIS_SERIAL` picks the camera). Beyond the Spinnaker set, it checks each Phase 3 assumption: a second open raises `DeviceBusy`, auto exposure locks `ExposureTime`, buffer timestamps aren't 0, and Aravis's range check refuses out-of-range writes. It also prints the gap between the device's stamp and the buffer's arrival time, for the rolling-shutter question, and the newest frame's age after a pause. Run with `-s`.

### Verification

- [ ] CI is green.
- [x] Real Aravis 0.8.36 on macOS, without a camera (2026-10-22). The drift tests pass, and `tests/test_aravis_library.py` drives `AravisCamera` through Aravis's simulated camera. This found two changes: falling back to `ExposureTimeAbs`, and skipping the 1 s device-list update when reopening.
- [ ] IDS U3-36P0XCP-M-GL hardware tests pass on macOS and Linux.
- [ ] The rolling-shutter timestamp behaviour is checked.

## Phase 4: GenTL backend (Windows, Linux)

### Tasks

- [x] Task 4.1: Build a fake for the chosen library (Harvesters or `genicam`), covering producer loading, discovery, the node map, buffers, and error codes.
- [x] Task 4.2: Write tests for the lazy import, the install hint, and a missing producer (`GENICAM_GENTL64_PATH`); implement.
- [x] Task 4.3: Write tests for discovery, open/close, the stream, and error mapping; implement.
- [x] Task 4.4: Run the contract suites against the fake.
- [x] Task 4.5: Add hardware tests.
  - The GenTL backend calls `genicam` directly:
    - One shared producer per `.cti`, reference-counted, found through `GENICAM_GENTL64_PATH` or passed as `producer=`. A missing or unloadable producer raises `ImportError` with the steps to fix it.
    - The camera's XML is fetched from its port (`local:` padded or zipped, or `file:`).
    - Buffers are announced on start and revoked on stop. `TLParamsLocked` is held while streaming, and the camera's `AcquisitionStart`/`AcquisitionStop` are executed.
    - The stream is set to `NewestOnly` when its node map has the mode, and older frames are drained anyway.
    - Waits are cut into 100 ms slices (harvesters #508).
  - `tests/test_gentl_library.py` drives the node-map code through real `genapi` 1.6.0 with a hand-written camera XML. It skips without `genicam`. Checked under Python 3.12: `symbolics` leaves out unavailable entries, and locks make nodes read-only.
  - The hardware checks are now shared: `tests/contracts/genicam_hardware.py` (`GenICamHardwareTests`) is subclassed by `test_aravis_hardware.py` (which adds the arrival-timing check) and by `test_gentl_hardware.py` (`GENTL_SERIAL`).
  - `[gentl]` = `genicam>=1.6` was added to `pyproject.toml` now (part of Task 5.3), because the driver's install hint names it. It is included in `[all]`.

### Verification

- [ ] CI is green.
- [x] Under Python 3.12, the full suite and the real-`genapi` node-map test pass (2026-10-22).
- [ ] IDS hardware tests pass on Linux through IDS peak, and on Windows if a Windows machine is available. Otherwise the Windows check is recorded as outstanding.

## Phase 5: CLI, docs and hardware checks

### Tasks

- [ ] Task 5.1: Add the `el-devices` subcommand with backend selection: list cameras, capture N frames, and a latency benchmark, matching `el-devices spinnaker`.
- [ ] Task 5.2: Write the hardware docs:
  - USB3 Vision backends: install per OS, how the driver differs from Aravis and GenTL, hazards.
  - The IDS U3-36P0XCP-M-GL doc: identity, nodes, timing, rolling shutter, rig steps.
- [ ] Task 5.3: Update `pyproject.toml` extras, `CLAUDE.md`, `tech-stack.md`, the README, and `docs/guide.md` (only if callers can notice the difference).
- [ ] Task 5.4: Run the manual checks on the IDS camera: Aravis on macOS and Linux, GenTL on Linux and Windows. Record the results in the hardware doc.
- [ ] Task 5.5 (stretch): Run the CM3-U3-50S5M through both generic backends and compare it with the Spinnaker driver.

### Verification

- [ ] The manual checks are recorded, and `pytest -m hardware` results are in the doc.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
