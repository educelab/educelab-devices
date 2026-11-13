# Implementation Plan: Camera Preview Mode

**Track ID:** preview-mode_20261102
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-02
**Status:** [~] In Progress

## Overview

Discovery on the hardware comes first, so each driver's preview path is chosen against known behavior. Next, the `Camera` API, the contract and a host-side implementation in every driver land together, which keeps the suite green. Hardware ROI and binning on GenICam and zoom on gphoto2 follow. Docs, the CLI and hardware checks come last.

## Design inputs from the architecture review (2026-11-04)

Phase 2 waits for the GenICam deepening track: the Spinnaker fake moves onto `fakes.genicam.FakeCamera`, and the core's settings become per-setting read/write descriptors. ROI and binning are then simulated in one fake, and the capture geometry has one place to live. Fold these into Phases 2 and 3:

- The `Camera` base owns the preview state and its guards, not each driver. `Device.close()` runs `_close()` then `_reset_state()` (`device.py:143-147`), so the base needs a `_stop_preview` hook that runs before `_close()`. Otherwise every driver stops preview in its own `_close()`, as gphoto2 already does for live view (`gphoto2.py:342-350`).
- The core's canonical `width`/`height` read the live `Width`/`Height` nodes (`_genicam.py:706-710`), so they'd report preview geometry once preview writes ROI. Keep an explicit capture-geometry record in the core instead (Task 3.3).
- `_configure` (`_genicam.py:560-584`) doesn't restore the full-sensor ROI on open, and a camera keeps its ROI until it's power-cycled. A preview interrupted by a crash would leave the next session capturing a crop.
- `_grab` (`_genicam.py:1028-1137`) tracks free-run versus software-trigger through loose flags (`_software_trigger`, `_fired_at`, `_fires`, `_trigger_turnaround`, `_delivered`, `_last_frame_at`). Make the acquisition mode explicit before preview adds a third (Task 2.5).
- gphoto2's latent `_live_view_tried`/`_live_view` (`gphoto2.py:170-172, 440-483`) become the explicit start/stop. Making `liveviewzoomarea`/`liveviewsize` read-only during preview (Task 4.2) needs the settings conversion to see the preview state.

## Phase 1: Hardware discovery

Use throwaway scripts in a scratch directory, not package code. Each finding goes into the camera's hardware doc, marked verified, assumed or unknown.

### Tasks

- [x] Task 1.1: Z7 II: record what `viewfinder=1` (and `0`) does to the rear screen and the EVF, and whether `capture_preview()` alone does the same.
  - The first `capture_preview()` starts live view and blanks the rear screen and EVF ("Connected to PC"); `viewfinder` writes change neither display, and only closing the session brings them back. So `stop_preview()` can't return the operator's screens mid-session. Details in `docs/hardware/gphoto2-cameras.md` (Live view).
- [x] Task 1.2: Z7 II: check whether `changeafarea` (or another widget) moves the `liveviewzoomarea` crop, and record its coordinate system and limits. Record the exact crop each zoom level gives at each `liveviewsize`.
  - The crop is `liveviewzoomarea` sensor pixels wide at 3:2, centred on `changeafarea` (`"<x>x<y>"` in sensor pixels, clamped to the sensor). `changeafarea` reads back `0x0` and is refused until live view runs. Writes show 0.2–0.4 s later. Details in `docs/hardware/gphoto2-cameras.md` (Z7 II zoom geometry).
- [x] Task 1.3: Z7 II: after a focus step, check whether the next preview requested can still show the lens from before the step, and how many previews it takes to settle.
  - The first preview requested after `step()` returns (which waits `focus_settle_s()`) already shows the new focus, in 4 of 4 rounds at ±2000 steps. About one fast preview in four or five repeats the previous frame's bytes.
- [x] Task 1.4: CM3-U3-50S5M and IDS U3-36P0XCP: check which of `OffsetX`/`OffsetY` and `Width`/`Height` can be written while streaming, through each backend we support on the bench (Spinnaker, Aravis, GenTL), and record their increments.
  - Through Aravis on both cameras: size writes are accepted while streaming but need an acquisition restart (0.1–0.4 s); offsets move with no restart. Spinnaker and GenTL (FLIR's producer) on the CM3 lock size, binning and decimation while streaming and write them paused (restart 0.12–0.14 s), and move offsets while streaming. The IDS through GenTL (IDS peak, Windows) locks offsets too while streaming, and can't honor `not_before` (no latch, no arrival times). Details in `docs/hardware/usb3-vision.md` (Geometry).
- [x] Task 1.5: CM3 and IDS: record the binning and decimation nodes, modes and selectors, whether each mode averages or sums, whether it changes the frame rate, and the restart cost of switching geometry.
  - Binning sums on both (4× the level at 2 × 2); decimation keeps the level. The CM3's throughput limit and its maximum shrink with the payload and don't grow back. Writing the old limit over the lower maximum broke its stream until a pixel format round trip reset it.

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

#### What Phase 1 means for Phases 3 and 4 (proposed, for review)

Phase 3 (GenICam):

- Reduce only when `max_size` asks for it, with **decimation**, not binning: binning sums, so a binned preview is 4× brighter than the capture. Then stride on the host for the rest of `max_size`.
- A preview of the same size at a new place only moves the offsets, which needs no restart. A new size, or decimation, needs a stop and start: about 0.1–0.4 s through Aravis.
- After applying geometry, raise `AcquisitionFrameRate` to its new maximum (`_raise_frame_rate`), since the IDS doesn't do it on its own.
- On `stop_preview()` and `close()`, restore the capture geometry and then the throughput limit, with a pixel format round trip first if the limit's maximum has shrunk. Never write the limit above its current maximum.
- The hardware path works on every backend checked (writes with acquisition paused, as `_acquisition_paused` does). On the IDS through GenTL, moving the preview also needs a restart, since IDS's producer locks offsets while streaming.
- `not_before` on the IDS through GenTL can't work by timestamp. Preview there either refuses `not_before`, as capture does, or falls back to arrival: wait until `not_before`, then drop the frames already buffered.

Phase 4 (gphoto2):

- Map the region to the tightest `liveviewzoomarea` whose crop (N sensor pixels wide, 3:2) contains it, centred with `changeafarea`, and host-crop the rest; the reported region is the requested one rounded inward. Regions wider than the `2048` crop (0.248 of the sensor) use zoom `0`.
- Write `changeafarea` only after live view is running, and every time the zoom changes. Discard previews for about 0.4 s after a zoom, centre or size write.
- `stop_preview()` can't give the operator the camera's screens back; only `close()` does. Say so in the guide.

The discovery scripts were run from a worktree at e91ee59 (before Phase 2's `Camera` changes) with the hardware venv; logs are in the session scratchpad, and the findings are in the hardware docs.

### Verification

- [x] The maintainer reviews the findings. Phases 3 and 4 are amended to match what was found. (Approved 2026-11-10.)

## Phase 2: Preview API and contract

### Tasks

- [x] Task 2.1: Add `PreviewGeometry` (region in sensor fractions, output `width`/`height`) and region validation in `cameras/base.py`, with unit tests.
  - The region type is `PreviewRegion(x, y, width, height)`, the glossary's term, as a frozen dataclass; `PreviewRegion.of()` also takes a 4-tuple or `None` (the whole sensor). `check_max_size()` validates `max_size`.
- [x] Task 2.2: Write the preview contract tests in `tests/contracts/cameras.py`. They cover start, reconfigure, stop as a no-op, `close()` ending preview, `preview_frame()` raising outside preview, capture raising during preview, the reported region lying within the requested one, the output staying within `max_size`, `not_before`, and setting changes applying during preview.
  - Committed red: the drivers pass it from Tasks 2.3–2.6. `not_before` is checked by the frame's timestamp and by elapsed time. Wrong-state calls raise a new `PreviewError(DeviceError)`, following `PositionError`. Setting changes are checked by writing every choice during preview and confirming frames stay at the reported size.
- [x] Task 2.3: Add `start_preview()`, `stop_preview()`, the `preview` property and `preview_frame(not_before=None)` to `Camera`, with driver hooks. Make `capture_frame()`/`capture_files()` refuse during preview.
  - `Camera.close()` stops preview before `Device.close()` runs `_close()`, logging a failure to stop; `_reset_state()` also clears it. A failed `_start_preview()` ends preview rather than leaving a half-applied one. `preview_frame()` takes `not_before` keyword-only, like `capture_frame()`.
- [x] Task 2.4: Simulated cameras: implement region and `max_size` on the host, and pass the contract.
  - The host crop and stride is a shared helper, `cameras/_preview.py` (`host_crop`), for the GenICam fallback and gphoto2 too. It rounds the region inward to whole pixels and clips it to the part of the sensor the frames cover, so the reported region always lies within the requested one. A `SimulatedCamera` with a larger `sensor_size` now takes its frames from the middle of the sensor.
- [x] Task 2.5: GenICam core: implement preview with host crop and stride, and `not_before` through `_grab()`. Pass the contract on the fakes for all three backends.
  - `PREVIEW_MAX_SIZE` is gone. An untimed camera refuses `not_before` in preview as in capture. Making the acquisition mode explicit moves to Phase 3 (Task 3.2), where preview first changes how the camera acquires.
- [x] Task 2.6: gphoto2: switch live view on and off at `start_preview()`/`stop_preview()`, stop touching continuous AF, and implement `not_before` by arrival. Pass the contract on the fake.
  - `start_preview()` grabs one image to learn the live-view size. If a setting changes that size mid-preview, the next frame re-plans the crop and `camera.preview` follows.

#### API revision after the downstream-use review (2026-11-10)

The maintainer walked through how `pgs-scan` and `pgs-spectral` use preview: framing, focusing by scroll-zoom on full-resolution images, thumbnails of every camera, often at Mono10 and above. Agreed, and applied across Tasks 2.1–2.6:

- `preview_frame()` returns a `PreviewFrame` (pixels, pixel format, timestamp, geometry).
- `max_size` defaults to `None`: full resolution unless the caller asks for less. It stays a pixel bound, not a scale factor.
- Preview keeps the camera's current pixel format. The library never converts depth; display scaling, zoom, pan, histograms and focus metrics are the consumer's.
- Captures still refuse during preview (consumers show the last capture during a scan). One stream per camera.
- Cameras that can't time their frames refuse `not_before`.

Owed hardware check: whether Mono8 is the top 8 bits of the deeper formats on the CM3 and the IDS, so 8-bit clipping matches full-depth clipping.

### Verification

- [x] The full suite passes, and every driver and simulated camera passes the preview contract. (2438 passed, 2026-11-10; mypy clean.)

## Phase 3: GenICam hardware ROI and binning

Scope as amended after Phase 1.

### Tasks

- [x] Task 3.1: Add the core's geometry mapping: region fractions to ROI rounded to the node increments, and `max_size` to binning or decimation and then host stride. Tests on the fake node map.
  - `hardware_roi()` and `pick_decimation()` in `cameras/_preview.py`. The ROI contains the region and the host crops the rest. Its size depends only on the region's size, so a pan never changes it. Decimation is the largest that still leaves `max_size` pixels, and never binning.
- [x] Task 3.2: Apply the geometry at `start_preview()`, restarting acquisition only when a locked node changes, and restore the capture geometry at `stop_preview()` and `close()`.
  - A full-sensor, full-resolution preview writes nothing to the camera. A same-size move writes only the offsets, live where the camera allows it and paused where it doesn't. Stopping restores the geometry and then the throughput limit, in one pause, with a pixel format round trip when the limit's maximum has shrunk; the limit is never written above its maximum.
  - Open now puts a leftover ROI or decimation back on the whole image, so a preview cut short by a crash can't leave the next session capturing a crop.
  - Not done: making the acquisition mode explicit. Preview turned out not to be a third mode; it grabs through whichever of free-run or software trigger is on, so the refactor would only have churned timing code that has no hardware run behind it yet.
- [x] Task 3.3: Keep the canonical `width`/`height` reporting the capture size during preview. Keep timestamps and `not_before` correct across geometry restarts.
  - After any geometry change, frames stamped before it are dropped, so a frame never carries a geometry it wasn't taken with. A camera that can't time frames drops the ones already waiting instead.
- [x] Task 3.4: Enable each path per model and per backend according to the Phase 1 findings.
  - By what the camera reports, not a table: the hardware path needs writable `Width`, `Height`, `OffsetX` and `OffsetY` before acquisition starts, decimation needs a writable `DecimationVertical`, and offsets move live only while they read as writable. Phase 1 found the path works on every camera and backend checked.

Owed hardware checks (Task 5.4): `Width`, the offsets and their maxima are in decimated pixels after a decimation write (assumed, on both cameras); the ROI reset at open; the first frame after a live offset write shows the new offsets; the CM3's limit comes back after a decimated preview; the IDS through IDS peak's producer moves with a restart.

### Verification

- [x] The core and backend tests pass, and the contract still passes. (2560 passed, 2026-11-10, including the contract on ROI fakes and an ROI preview through each backend's fake.)

## Phase 4: gphoto2 zoom and size

Scope as amended after Phase 1.

### Tasks

- [x] Task 4.1: Map the region to the nearest `liveviewzoomarea` (and to a zoom center, if Task 1.2 found a way to set one), and `max_size` to `liveviewsize`. Report the applied region and size.
  - Enabled per body from a table of measured zoom geometry (`_LIVE_VIEW`, the Z7 II only), since the crop is in sensor pixels and gphoto2 doesn't report the sensor size. `start_preview()` waits 0.4 s after a size, zoom or centre write, then takes the image it plans the crop from. `stop_preview()` restores the size and zoom as found.
- [x] Task 4.2: Report `liveviewzoomarea` and `liveviewsize` read-only through `settings()`.
  - Only on bodies in the table. Elsewhere preview doesn't touch them, so they stay writable.

Image areas (raised by the maintainer, 2026-11-10; fixed): the Z7 II's image area (FX, DX, 5:4, 1:1, 16:9) changes what captures and live view cover. All five were measured with `z7_live_view.py area`. The camera has no image-area widget, but the largest `imagesize` choice is the area's size in sensor pixels; the zoom crop stays N sensor pixels wide at 3:2; `changeafarea` counts from the area's corner; the crop is clamped to the area. The driver now reads the area at each preview start and treats the region as a fraction of it, and the table holds no sensor size. Its tests replay the 13 measured centres. On the camera in 16:9, reported regions were within 0.003 of a template match.

Owed hardware checks (Task 5.4): the driver's reported region on the camera in FX, DX, 5:4 and 1:1 (16:9 is done), including the clamp at the area's edge; that 0.4 s is enough after a centre-only write. The size and zoom restore on stop is verified.

### Verification

- [x] The gphoto2 tests and the contract pass. (2581 passed, 2026-11-10.)

## Phase 5: Docs, CLI, hardware checks

### Tasks

- [x] Task 5.1: Update `docs/guide.md` with the preview state, `PreviewGeometry`, `not_before`, and the snapshot pattern (stop, capture, start).
- [x] Task 5.2: Update the hardware docs (`gphoto2-cameras.md`, `spinnaker.md`, `usb3-vision.md`, `ids-u3-36p0xcp.md`) with each camera's preview mechanism.
- [x] Task 5.3: Move `el-devices dslr --preview` and any GenICam subcommands to the new API, with region and size options.
  - `--preview [FRAMES]`, `--region X,Y,W,H` and `--max-size PIXELS` on `dslr`, `genicam` and `spinnaker`, from a shared `app/_preview.py`. The GenICam subcommands preview each camera after its captures.
- [x] Task 5.4: Write `@pytest.mark.hardware` tests for preview (geometry, `not_before` after a focus step on the Z7 II, and a full-sensor versus zoomed region on the CM3 and IDS). Run them, and record the results.

  - Z7 II, 2026-11-10 and 11: the hardware tests pass in FX (19 passed, 7 skipped), DX, 5:4 and 1:1, and the driver's reported regions match the frames in all five image areas. Two things found and fixed on the way:
    - Stopping took 3.6 s and each start 4 s, from whole-tree reads. Preview now probes its widgets by name (`Session.find_widgets()`): a start takes 0.95-1.1 s, the first included, and a stop 0.6 s.
    - In DX the first zoom after an unzoomed view landed off its reported region: a centre written while the zoom is changing is misplaced. The driver now writes the centre twice with a new zoom level.
  - CM3 (`20305122`) through Spinnaker, Aravis and FLIR's GenTL producer, and IDS through Aravis, on the lab Mac, 2026-11-11: the eight preview tests pass on each, and the full hardware suites pass through Spinnaker and Aravis. Found and fixed: a smaller image raised the CM3's frame rate, which cut a long exposure short and left it cut after stopping. Preview now holds the frame rate at the whole image's, so the exposure stays and the camera still sends the smaller image; a camera without the frame-rate nodes falls back to cropping whole frames on the host.
  - Windows rig through GenTL, 2026-11-11 (`hardware_tests.py` in this folder): all five CM3s through FLIR's producer, 8 passed each; the IDS through IDS peak's producer, 6 passed and 2 skipped (the moved region's content, on a flat scene, and `not_before`, which that producer can't time). Recorded in `usb3-vision.md`, Hardware runs.

  - Found with the viewer on 2026-11-12, and fixed:
    - Through Aravis, a preview of a small region stopped after 1-10 s on the IDS and the CM3: Aravis's async mode loses a buffer returned within about a millisecond of its frame, which only small frames allow. The driver now rests each buffer 2 ms first.
    - On the IDS, the first frame after a live region move was torn. Preview now also drops frames from the frame period after such a move.
  - Windows rig again through GenTL, 2026-11-12, with those fixes: all five CM3s 8 passed each; the IDS through IDS peak 7 passed, 1 skipped (`not_before`), including the moved region's content that a flat scene had skipped before.
- [x] Task 5.5: Add `el-devices preview`, a viewer for checking framing, zoom and focus by eye (added at the maintainer's request). OpenCV comes from the `[viewer]` extra, and the subcommand is only registered when `cv2` can be found. Tried with the simulated camera only.

- [x] Task 5.6: Report `capture_size` and `scale` in `PreviewGeometry` (added at the maintainer's request, 2026-11-12), with the Z7 II using its selected image size, and show the scale in `el-devices preview`. Checked on the Z7 II; unit-tested only on GenICam cameras. Also from the maintainer's Z7 II session: the driver picks the live-view size that delivers the most under `max_size`, the viewer shows a captured JPEG, and zooming before a level holds the whole region was tried and ruled out.

### Verification

- [x] The maintainer confirms preview and zoom on the Z7 II, the CM3 and the IDS.
  - CM3 (Spinnaker) and IDS (Aravis) confirmed in `el-devices preview` on the lab Mac, 2026-11-12: zoom, drag, capture, Mono16 and Mono10g40IDS, and the CM3 holding a 27 ms exposure while zoomed.
  - Z7 II (gphoto2) confirmed in `el-devices preview` on the lab Mac, 2026-11-12: zoom through all three levels, moving a region, the scale in the overlay, and a capture shown as its JPEG.
  - The track stays open: the maintainer is integrating preview into `pgs-scan`'s apps before merging here, so no PR yet.
  - pgs-scan's integration is done off-rig (2026-11-12, against `9c0a191`). It found four things to decide before merging: no bit depth on `PreviewFrame`, the CM3's Mono16 clipping at 65520, `max_size` rounding down a whole step, and `host_crop` being private. See [pgs-scan-handoff.md](./pgs-scan-handoff.md).

## Phase 6: What pgs-scan's integration found

From `pgs-scan-handoff.md` (pgs-scan's track `preview-viewer_20261112`). The maintainer asked for all of it in this track and PR, the test chart included.

### Tasks

- [x] Task 6.1: A bit depth and full scale on every frame. `PreviewFrame.bit_depth` is the format's depth, as a capture's `metadata["bit_depth"]`, and `PreviewFrame.full_scale` and `metadata["full_scale"]` the value a saturated pixel reads. The CM3-U3-50S5M's `Mono16` is listed by model as 12 bits at the top (65520), from frames measured in October; other models are taken to fill the format.
- [x] Task 6.2: A floor alongside the cap. `start_preview(region, max_size, *, min_size=None)`: the most reduction that keeps at least `min_size` on the longest side, still capped by `max_size`. Through every driver's step choice: host strides (`reduction()`), GenICam decimation, and the Z7 II's live-view size. The `_start_preview` hook gains `min_size`, so drivers outside the package (pgs-scan's simulated DSLR) need it too. `--min-size` on the camera subcommands; the viewer asks for at least its window.
- [x] Task 6.3: `host_crop` and `HostCrop` exported from `educelab.devices.cameras`, for an application's own simulated camera.
- [x] Task 6.4: A test chart in both simulated cameras: `pattern="chart"` (gradient stays the default), with a Siemens star, 1/2/3-px bars, a 64-px grid, a step wedge and a clipping patch, noisy so frames change. `focus=SimulatedFocusDrive(sharp_at=...)` blurs by the lens's distance from sharp. `el-devices preview simulated` shows a CM3-sized chart, with `[` and `]` for focus.
- [x] Task 6.5: The guide, `docs/preview.md` (a section on developing off the rig), ADR 0007, `spinnaker.md` and the spec.

Owed on hardware: the CM3 and IDS previewed with `min_size` (decimation then stride), and whether `PixelDynamicRangeMax` or `AdcBitDepth` gives the CM3's full scale so the model list could go. Not owed: the handoff's question whether pixel-format and throughput-limit changes need preview stopped. They don't by the API (`test_pixel_format_can_change_during_an_roi_preview`); on hardware it is untested.

### Verification

- [ ] The maintainer confirms pgs-scan works against this phase.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
