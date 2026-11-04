# Implementation Plan: Camera Preview Mode

**Track ID:** preview-mode_20261102
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-02
**Status:** [~] In Progress

## Overview

Discovery on the hardware comes first, so each driver's preview path is chosen against known behavior. Next, the `Camera` API, the contract and a host-side implementation in every driver land together, which keeps the suite green. Hardware ROI and binning on GenICam and zoom on gphoto2 follow. Docs, the CLI and hardware checks come last.

## Phase 1: Hardware discovery

Use throwaway scripts in a scratch directory, not package code. Each finding goes into the camera's hardware doc, marked verified, assumed or unknown.

### Tasks

- [~] Task 1.1: Z7 II: record what `viewfinder=1` (and `0`) does to the rear screen and the EVF, and whether `capture_preview()` alone does the same.
- [ ] Task 1.2: Z7 II: check whether `changeafarea` (or another widget) moves the `liveviewzoomarea` crop, and record its coordinate system and limits. Record the exact crop each zoom level gives at each `liveviewsize`.
- [ ] Task 1.3: Z7 II: after a focus step, check whether the next preview requested can still show the lens from before the step, and how many previews it takes to settle.
- [ ] Task 1.4: CM3-U3-50S5M and IDS U3-36P0XCP: check which of `OffsetX`/`OffsetY` and `Width`/`Height` can be written while streaming, through each backend we support on the bench (Spinnaker, Aravis, GenTL), and record their increments.
- [ ] Task 1.5: CM3 and IDS: record the binning and decimation nodes, modes and selectors, whether each mode averages or sums, whether it changes the frame rate, and the restart cost of switching geometry.

Prepared before the rig run, smoke-tested on the fakes only:

- `z7_live_view.py` covers Tasks 1.1–1.3, in three checks. `viewfinder` asks
  the operator what the rear screen and EVF show at each step. `zoom` saves a
  JPEG for every `liveviewsize` × `liveviewzoomarea` and every `changeafarea`
  probe. Send the directory, so each crop can be measured against the
  full-sensor frame. `settle` steps focus and times each preview after the step,
  with its sharpness and whether its bytes repeat the last frame. That also
  settles whether the 15–21 ms frames are repeats.
- `genicam_geometry.py` covers Tasks 1.4–1.5 for one camera and backend per
  run. It tries each ROI, binning and decimation write while streaming, and
  retries any that fail with acquisition paused, timing the restart. It
  reports each frame's size, mean level and frame rate. It puts the geometry
  back as found, and never touches `TLParamsLocked`.

### Verification

- [ ] The maintainer reviews the findings. Phases 3 and 4 are amended to match what was found.

## Phase 2: Preview API and contract

### Tasks

- [ ] Task 2.1: Add `PreviewGeometry` (region in sensor fractions, output `width`/`height`) and region validation in `cameras/base.py`, with unit tests.
- [ ] Task 2.2: Write the preview contract tests in `tests/contracts/cameras.py`. They cover start, reconfigure, stop as a no-op, `close()` ending preview, `preview_frame()` raising outside preview, capture raising during preview, the reported region lying within the requested one, the output staying within `max_size`, `not_before`, and setting changes applying during preview.
- [ ] Task 2.3: Add `start_preview()`, `stop_preview()`, the `preview` property and `preview_frame(not_before=None)` to `Camera`, with driver hooks. Make `capture_frame()`/`capture_files()` refuse during preview.
- [ ] Task 2.4: Simulated cameras: implement region and `max_size` on the host, and pass the contract.
- [ ] Task 2.5: GenICam core: implement preview with host crop and stride, and `not_before` through `_grab()`. Pass the contract on the fakes for all three backends.
- [ ] Task 2.6: gphoto2: switch live view on and off at `start_preview()`/`stop_preview()`, stop touching continuous AF, and implement `not_before` by arrival. Pass the contract on the fake.

### Verification

- [ ] The full suite passes, and every driver and simulated camera passes the preview contract.

## Phase 3: GenICam hardware ROI and binning

Scope as amended after Phase 1.

### Tasks

- [ ] Task 3.1: Add the core's geometry mapping: region fractions to ROI rounded to the node increments, and `max_size` to binning or decimation and then host stride. Tests on the fake node map.
- [ ] Task 3.2: Apply the geometry at `start_preview()`, restarting acquisition only when a locked node changes, and restore the capture geometry at `stop_preview()` and `close()`.
- [ ] Task 3.3: Keep the canonical `width`/`height` reporting the capture size during preview. Keep timestamps and `not_before` correct across geometry restarts.
- [ ] Task 3.4: Enable each path per model and per backend according to the Phase 1 findings.

### Verification

- [ ] The core and backend tests pass, and the contract still passes.

## Phase 4: gphoto2 zoom and size

Scope as amended after Phase 1.

### Tasks

- [ ] Task 4.1: Map the region to the nearest `liveviewzoomarea` (and to a zoom center, if Task 1.2 found a way to set one), and `max_size` to `liveviewsize`. Report the applied region and size.
- [ ] Task 4.2: Report `liveviewzoomarea` and `liveviewsize` read-only through `settings()`.

### Verification

- [ ] The gphoto2 tests and the contract pass.

## Phase 5: Docs, CLI, hardware checks

### Tasks

- [ ] Task 5.1: Update `docs/guide.md` with the preview state, `PreviewGeometry`, `not_before`, and the snapshot pattern (stop, capture, start).
- [ ] Task 5.2: Update the hardware docs (`gphoto2-cameras.md`, `spinnaker.md`, `usb3-vision.md`, `ids-u3-36p0xcp.md`) with each camera's preview mechanism.
- [ ] Task 5.3: Move `el-devices dslr --preview` and any GenICam subcommands to the new API, with region and size options.
- [ ] Task 5.4: Write `@pytest.mark.hardware` tests for preview (geometry, `not_before` after a focus step on the Z7 II, and a full-sensor versus zoomed region on the CM3 and IDS). Run them, and record the results.

### Verification

- [ ] The maintainer confirms preview and zoom on the Z7 II, the CM3 and the IDS.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
