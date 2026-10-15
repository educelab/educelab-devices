# Implementation Plan: Spinnaker Camera

**Track ID:** spinnaker-camera_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12
**Status:** [~] In Progress

## Overview

Build a fake PySpin first, then the system/enumeration layer, then the per-camera driver with correct buffer handling, then docs, CLI, and rig verification.

## Phase 1: Fake PySpin and system

### Tasks

- [x] Task 1.1: Build a fake PySpin module covering the node maps, camera list, image buffers (with release tracking), timestamps, and incomplete images.
- [x] Task 1.2: Write tests for lazy import and the install-hint error; implement.
- [x] Task 1.3: Write tests for `SpinnakerSystem` lifetime and `discover()` (including removal while iterating, #90); implement.

### Verification

- [x] System tests pass in CI.
  - Approved 2026-10-15, after CI on draft PR #5 passed on every OS and Python version, including the numpy<2 job.
  - Decided (2026-10-15), acquisition (Task 2.3): continuous acquisition with `StreamBufferHandlingMode=NewestOnly`, started by `BeginAcquisition()` in `open()` and ended by `EndAcquisition()` in `close()`, because it is the fastest way to capture (the maintainer's criterion). This is what `pgs-scan` `dev` does. Each capture takes the next frame and rejects ones older than `not_before`, with bounded retries, re-measuring the timestamp offset when a round of retries runs out; that rejection is the main mechanism, not a safeguard. This corrects a same-day decision for software triggering, which pays for exposure, readout and transfer on every capture; the spec's original acquisition line stands. Phase 3 measures capture latency and USB bandwidth with several cameras streaming.
  - Decided (2026-10-15), timestamp node: read `Timestamp`, falling back to `TimestampLatchValue`. Which one the rig cameras have is unknown until Phase 3.
  - Decided (2026-10-15), `IMAGE_MISSING_TRAILER`: accept these frames as complete, as `pgs-scan` does, and count them in the `Frame` metadata. Assumed safe until Phase 3.

### Notes and deviations

- `SpinnakerSystem` is a handle, not a `Device`. Handles on the same PySpin module share one `System` instance (taken on the first `open()`, released on the last `close()`), because whether PySpin reference-counts `GetInstance()` is unknown. The PySpin module is injectable (`SpinnakerSystem(pyspin=...)`) so tests use the fake without patching `sys.modules`.
- `SpinnakerSystem.discover()` returns `DeviceInfo(backend="spinnaker", model, serial_number)` with `address=None`, since the serial number is how a camera is addressed. It skips cameras whose identity can't be read and reports a camera with an unreadable serial as `serial_number=None`. `SpinnakerCamera.discover()` (Phase 2) delegates to it, and the `BACKENDS` entry lands with that class.
- `docs/hardware/spinnaker.md` was started in Phase 1, because the missing-PySpin error links to its install steps. Task 3.1 completes it.
- Found with the fake: logging a `SpinnakerException` object (rather than its text) keeps its traceback, and the camera reference in it, alive in any retained log record, so `ReleaseInstance()` fails. Recorded under Hazards in the hardware doc; Phase 2 must follow the same rule (and be careful with `raise ... from` while a camera reference is in scope).

## Phase 2: Camera driver

### Tasks

- [x] Task 2.1: Write tests for open/close configuration; implement.
- [x] Task 2.2: Write tests for settings descriptors and `exposure_time` unit conversion; implement.
- [x] Task 2.3: Decide acquisition start/stop semantics with the maintainer; implement.
- [x] Task 2.4: Write tests for `capture_frame(not_before=…)`: stale rejection, bounded retries, every buffer released (#83), incomplete rejection; implement.
- [x] Task 2.5: Implement `preview_frame()`; run the `Camera`/`FrameCamera` contract suites.

### Verification

- [ ] Camera tests and contracts pass; #83 and #90 covered by regression tests.

### Notes and deviations

- `capture_frame(not_before=None, timeout=None)` is an override on `SpinnakerCamera` with optional keyword arguments. The `FrameCamera` ABC is unchanged, so code written against the role can't pass `not_before`; promoting it to the role would be an ABC change and needs approval.
- Capture retries: 5 frames per round (`RETRIES_BEFORE_REMEASURE`, pgs-scan's limit). If a round included stale frames, the offset is re-measured, and the capture gives up with `DeviceError` after 10 frames (`MAX_ATTEMPTS`). The default timeout is the exposure time plus 2 s.
- Frame metadata: `exposure_time`, `gain`, `device_timestamp_ns`, `frame_id`, `retries`, `stale_frames`, `incomplete_frames`, `missing_trailer`, `timestamp_remeasured`.
- Settings: canonical `exposure_time` (s), `gain` (dB), `pixel_format`, plus native `ExposureAuto` and `GainAuto` with choices `Off` and `Continuous`. `Once` is left out because it reverts to `Off` on its own and would never read back. `pixel_format` offers only Mono8, Mono16, RGB8 and 8/16-bit Bayer formats, which the driver can shape into arrays, and `open()` switches away from any other format. Changing it pauses acquisition.
- `open()` also turns `TriggerMode` off, so a camera left triggered by another program still free-runs.
- SDK errors become `DeviceError`s with no `__cause__` or `__context__`, because a chained `SpinnakerException` keeps its camera alive through the traceback. Error-code mapping (`-1011` timeout to `DeviceTimeout`, `-1005` on `Init()` to `DeviceBusy`) is assumed until Phase 3.
- `preview_frame()` shifts 16-bit frames right by 8 and subsamples to at most 1280 px on the longest side.
- Registered `spinnaker` in `BACKENDS` and exported `SpinnakerCamera` from `educelab.devices.cameras`.
- The acquisition rewrite (2b060c4) accidentally dropped the hardware doc's later sections; c91bea1 restored them.

## Phase 3: Docs, CLI, hardware verification

### Tasks

- [ ] Task 3.1: Write `docs/hardware/spinnaker.md`.
- [ ] Task 3.2: Add the `el-devices spinnaker` subcommand with the benchmark.
- [ ] Task 3.3: Write `@pytest.mark.hardware` tests.
- [ ] Task 3.4: Run them on a multi-camera `pgs-scan` rig (Python 3.12); record findings.

### Verification

- [ ] Maintainer confirms capture timing and reliability on a rig.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
