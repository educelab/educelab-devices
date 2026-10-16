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

- [x] Camera tests and contracts pass; #83 and #90 covered by regression tests.
  - Approved 2026-10-16. The branch was then rebased onto PR #6 (`not_before`/`timeout` on `FrameCamera`) and PR #7 (`Setting.value` may be `None`), and the driver moved to the role's `_capture_frame` hook.

### Notes and deviations

- `not_before` and `timeout` were first an override on `SpinnakerCamera`. After PR #6 moved them onto the `FrameCamera` role (merged 2026-10-16; the branch was rebased onto it), the driver implements the role's `_capture_frame(not_before, timeout)` hook instead. `not_before` is now an aware `datetime` only, and `timeout` defaults to `capture_timeout` (30 s).
- Capture retries are bounded by the timeout, not by a count. The `MAX_ATTEMPTS` cap of 10 frames and the default timeout of exposure + 2 s were dropped, because the role contract requires `DeviceTimeout` when no acceptable frame arrives in time. A future `not_before` is waited for, or fails at once if it lies past the timeout. The offset is re-measured after every 5 stale frames (`RETRIES_BEFORE_REMEASURE`, pgs-scan's limit).
- Frame metadata: `exposure_time`, `gain`, `device_timestamp_ns`, `frame_id`, `retries`, `stale_frames`, `incomplete_frames`, `missing_trailer`, `timestamp_remeasured`.
- Settings: canonical `exposure_time` (s), `gain` (dB), `pixel_format`, plus native `ExposureAuto` and `GainAuto` with choices `Off` and `Continuous`. `Once` is left out because it reverts to `Off` on its own and would never read back. `pixel_format` offers only Mono8, Mono16, RGB8 and 8/16-bit Bayer formats, which the driver can shape into arrays, and `open()` switches away from any other format. Changing it pauses acquisition.
- `open()` also turns `TriggerMode` off, so a camera left triggered by another program still free-runs.
- SDK errors become `DeviceError`s with no `__cause__` or `__context__`, because a chained `SpinnakerException` keeps its camera alive through the traceback. Error-code mapping (`-1011` timeout to `DeviceTimeout`, `-1005` on `Init()` to `DeviceBusy`) is assumed until Phase 3.
- `preview_frame()` shifts 16-bit frames right by 8 and subsamples to at most 1280 px on the longest side.
- Registered `spinnaker` in `BACKENDS` and exported `SpinnakerCamera` from `educelab.devices.cameras`.
- The acquisition rewrite (2b060c4) accidentally dropped the hardware doc's later sections; c91bea1 restored them.

## Phase 3: Docs, CLI, hardware verification

### Tasks

- [x] Task 3.1: Write `docs/hardware/spinnaker.md`.
- [x] Task 3.2: Add the `el-devices spinnaker` subcommand with the benchmark.
- [x] Task 3.3: Write `@pytest.mark.hardware` tests.
- [ ] Task 3.4: Run them on a multi-camera `pgs-scan` rig (Python 3.12); record findings.

### Verification

- [ ] Maintainer confirms capture timing and reliability on a rig.
  - Decided (2026-10-16): keep continuous free-running acquisition. Whether software triggering is faster on a shared USB link stays open until rig step 4 measures it: rerun with `--gain`, then with `DeviceLinkThroughputLimit` at ~80 MB/s per camera, and compare the per-capture latency with the ~63 ms + exposure estimate for triggering.
  - Decided (2026-10-16): drop numpy 1. numpy's floor is now `>=2`, and the CI `numpy1` job was removed. PySpin 4.4.0's cp312 wheel requires numpy>=2 anyway.
  - Not decided: whether the driver exposes a frame-rate or throughput-limit setting. Not built.

### Notes and deviations

- Tasks ran in the order 3.2, 3.3, 3.1, so the hardware doc's rig steps could cite the finished command and tests.
- `el-devices spinnaker` opens every selected camera at once, so they all stream as on a rig, and captures from all of them together in threads, with `not_before` set to each request. It reports latency, frame age, drops, missing trailers, and an estimated streaming frame rate and MB/s. The estimate comes from frame IDs and device timestamps, and assumes frame IDs count frames that newest-only buffering discarded. `--exposure`, `--gain` and `--pixel-format` set up a run, matching what `pgs-scan`'s `system_check.py` used.
- `SpinnakerCamera.timestamp_node` (read-only) was added, so the command and the hardware tests can report which node a model uses.
- The hardware doc's Manual check section holds the exact rig steps, including latency, USB bandwidth, and a test of whether the timestamp marks the start or the end of exposure. Task 3.4 (the rig run) has not been done.
- First rig run (2026-10-16, five CM3-U3-50S5M on one Acroname USBHub3p, SDK 4.4.0.246). Results are in the hardware doc's Last run. The single-camera step worked. The multi-camera step crashed on `PySpin.IMAGE_MISSING_TRAILER`, a pre-3.0 name. Fixed in 51ba634: the driver and fake use 4.4.0's names (`SPINNAKER_IMAGE_STATUS_MISSING_TRAILER`, `Image.GetImageStatusDescription()`, and `SPINNAKER_ERR_*` read from the module), new tests check the fake against real PySpin when it is installed, and `el-devices spinnaker` reports any exception in a capture thread as that camera's failure.
- Minimum SDK: 4.4.0, checked in `SpinnakerSystem.open()` through `GetLibraryVersion()`; an older SDK raises `ImportError` naming both versions. Older SDKs would need fallbacks (listed in the hardware doc), which weren't built.
- numpy: PySpin 4.4.0.246's cp312 wheel requires numpy>=2, so "numpy<2" was dropped from the install steps and CLAUDE.md/tech-stack.md were corrected. The CI `numpy1` job stays for the maintainer to decide; no supported SDK needs it.
- Five cameras at ~179 MB/s exceed the hub's one ~400 MB/s upstream link. The doc gives the per-camera budget (~80 MB/s, ~16 frames/s at Mono8) and the settings that would reach it. It also raises software triggering again as a possibly faster option on a shared link; that is the maintainer's call.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
