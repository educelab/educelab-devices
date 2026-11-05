# Implementation Plan: GenICam Deepening

**Track ID:** genicam-deepening_20261104
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-04
**Status:** [~] In Progress

## Overview

Move the Spinnaker tests onto the shared fake first, so the descriptor work in Phase 2 is tested once for all three backends. Then fix the public surface and the smaller leaks. Each phase leaves the suite green.

## Phase 1: Spinnaker on the shared fake

### Tasks

- [x] Task 1.1: Inventory. List what `fakes.pyspin` simulates that `fakes.genicam.FakeCamera` doesn't. Sort the tests in `test_spinnaker_camera.py`, `test_spinnaker.py`, `test_fake_pyspin.py` and `test_cli_spinnaker.py` into PySpin-specific and duplicated. Check that `GenICamHardwareTests` covers every check in `test_spinnaker_hardware.py`. Record the lists in this plan before cutting anything.
- [x] Task 1.2: Grow `fakes.genicam.FakeCamera` with anything from 1.1 that is GenICam behaviour rather than PySpin behaviour, with tests in `test_fake_genicam.py`.
- [x] Task 1.3: Rebuild `tests/fakes/pyspin.py` as a PySpin-shaped skin over `FakeCamera`: node pointers and casts, `IsReadable`/`IsWritable`, `CameraList`, `System` refcounting, `SpinnakerException` codes, image status. Keep the tests in `test_fake_pyspin.py` that check PySpin's own rules.
- [x] Task 1.4: Add `TestPySpinBackend(BackendContract)`.
- [x] Task 1.5: Delete the duplicated tests from `test_spinnaker_camera.py`. Keep the PySpin-specific ones, and keep `test_cli_spinnaker.py` passing.
- [x] Task 1.6: Make `test_spinnaker_hardware.py` subclass `GenICamHardwareTests`, keeping only Spinnaker-specific checks. Run it on the CM3.
  - Two Spinnaker-specific checks are kept, and the frame-rate unit check moved into `GenICamHardwareTests`. On CM3 20305122 (2026-11-05): 26 passed, 1 skipped (the untimed-frames check).
  - The shared `test_a_second_open_of_the_same_camera_is_busy` found a driver bug: a second open in the same process passed `Init()`, failed at `BeginAcquisition()` (-1004), and its cleanup de-initialized the first open's camera. Fixed by checking `IsInitialized()` first; `BackendContract` now covers it for every backend.

### Inventory (Task 1.1)

**`fakes.pyspin` simulates, `fakes.genicam.FakeCamera` doesn't:**

- Grow `FakeCamera` (1.2): `ignored_trigger_calls`, which ignores triggers by number. `test_cli_spinnaker.py` needs it to drop the first capture's trigger after the priming one. The `ignore_triggers` count can't wait past the priming trigger.
- Drop: `PixelColorFilter` and node units (`GetUnit`). Only `test_fake_pyspin.py` reads them; the driver doesn't.
- Constructor options become `FakeCamera` options: `throughput=False` becomes `without=[...]`. The PySpin defaults (serial, model `Blackfly S BFS-U3-51S5M`, vendor `FLIR`, formats Mono8/Mono16, firmware `1.0.0.0`) are passed explicitly.
- Everything else already has an equivalent: access that follows the auto modes, the trigger and streaming, the timestamp latch, triggers (pending, ignored, unanswered), the buffer pool and overwrite on release, queued frames, the throughput nodes and mode, the device clock, `busy` and `attached`.

**PySpin-specific, stays in the skin (1.3):** `System` refcounting (live pointers, uncleared lists, `ReleaseInstance` refusals) and `LibraryVersion`. `CameraList` with `GetBySerial` and iteration that stops when the list is cleared (#90). `CameraPtr` `Init`/`DeInit` onto `open`/`close`, and `GetNextImage(ms)`. The TL device node map, readable before `Init`, and the TL stream node map with `StreamBufferHandlingMode` (as `fakes.gentl` keeps its own). Node pointers and casts, `CEnumerationPtr` without `GetValue`, `IsReadable`/`IsWritable`/`IsAvailable`, and enum integer constants. Int-only integer writes. `SpinnakerException` codes, translated from `FakeCameraError.kind`, with IO for an unplugged camera. Image status codes and descriptions, translated from `BufferStatus`. `attach`/`detach`.

**`test_spinnaker_camera.py` (91 tests), PySpin-specific, kept (23):**

- System sharing and refcounting: `constructor_does_not_touch_the_sdk`, `close_releases_the_camera_and_the_system`, `reopening_initializes_again`, `cameras_share_the_system`, `unknown_serial_raises_device_not_found_naming_what_is_attached`, `unplugged_camera_fails_to_open_cleanly`, `close_after_unplugging_still_releases_the_system`, `close_after_captures_releases_the_system`, `close_ends_acquisition_before_deinit`, `close_raises_if_ending_acquisition_fails`, contract `closing_releases_the_sdk`.
- Error codes and no-chaining: `camera_held_elsewhere_raises_device_busy`, `failed_configuration_releases_everything` (the stream node), `a_write_the_camera_refuses_raises_command_rejected`, `no_frame_in_time_raises_device_timeout`, `timeout_before_any_frame_hints_at_another_program`.
- Timeouts: `grab_timeout_is_the_time_left` (milliseconds and `EVENT_TIMEOUT_INFINITE`).
- Image status: `incomplete_frames_are_rejected_even_when_first` and `missing_trailer_frames_are_accepted_and_flagged`, rewritten as one test of the status mapping.
- Stream setup: `open_starts_free_running_acquisition`, cut down to `StreamBufferHandlingMode`.
- `discover`: `discover_lists_attached_cameras`, `el_devices_discover_lists_spinnaker_cameras`, `el_devices_discover_without_pyspin_says_how_to_install`.
- Contracts: `TestSpinnakerCameraContract` and its software-trigger variant, as Aravis and GenTL keep.

**`test_spinnaker_camera.py`, duplicated in `test_genicam_core.py`, deleted (68 tests, plus the manual-exposure and throughput-mode contract variants):** `open_initializes_the_camera`, `info_before_open_names_the_serial`, `info_after_open_has_model_and_firmware`, `repr_names_the_serial`. The timestamp offset tests (`open_measures_…`, `…_can_be_remeasured`, `older_timestamp_node_…`). Every settings test (`settings_cover_…`, the four `exposure_time_…`, `gain_is_in_decibels`, `auto_modes_…`, `pixel_formats_are_the_ones_…`, `set_pixel_format`, `open_replaces_a_pixel_format_…`, `a_feature_the_camera_lacks_…`, `a_camera_without_a_throughput_limit_…`). Acquisition: `open_turns_off_a_trigger_mode_left_on`, `pixel_format_change_pauses_acquisition`, `failed_pixel_format_change_still_resumes_acquisition`. Every capture test not kept above (`capture_returns_a_copy_…`, `capture_timestamp_is_host_time`, `capture_metadata`, `frame_timestamp_is_the_start_of_exposure`, the five `not_before` tests, `every_grabbed_buffer_is_released`, `many_captures_never_exhaust_…`, `retries_are_bounded_…`, `offset_is_remeasured_…`, `timeout_after_a_frame_has_no_hint`, `capture_in_each_pixel_format`). All four preview tests, all seven throughput tests, and all twenty software-trigger tests.

**`test_spinnaker.py`:** all PySpin-specific (import, `SpinnakerSystem`, `discover`, SDK version). Keep it all.

**`test_fake_pyspin.py`:**

- PySpin's own rules, kept: the three `release_instance_…` tests, `clearing_a_list_mid_iteration_skips_the_rest`, `nodes_need_init_but_transport_layer_nodes_do_not`, `detached_cameras_fail`, `get_by_serial_for_a_missing_camera_is_invalid`, `queued_frames_come_first_and_carry_status` (as status codes), `released_buffers_are_overwritten` (`GetNDArray` is a view), and the five checks against real PySpin.
- GenICam behaviour, moved to `test_fake_genicam.py` where `BackendContract` doesn't already cover it: `pixel_format_is_read_only_while_acquiring` and `timestamp_latch_follows_the_host_clock`.
- Already covered by `BackendContract`, deleted: `auto_exposure_makes_exposure_time_read_only`, `end_acquisition_twice_raises`, `unreleased_buffers_exhaust_the_pool`, `trigger_mode_needs_a_trigger_per_fresh_frame`. `color_cameras_produce_rgb_arrays` is also deleted; it goes with `PixelColorFilter`.

**`test_cli_spinnaker.py`:** keep it all. Switch its reads of fake internals to `FakeCamera`: `throughput_limit._value`, `nodes["ExposureTime"]`, `init_calls`, `ignored_trigger_calls`, and PySpin status codes in `FrameSpec`.

**Hardware.** `GenICamHardwareTests` covers every check in `test_spinnaker_hardware.py` except three:

- The dtype refcount drift in `many_captures_do_not_exhaust_buffers` (PySpin's `GetNDArray`). It's Spinnaker-specific; keep it.
- The `resulting_frame_rate` check in `throughput_limit_round_trips_and_is_restored`, which settles that the limit is in bytes per second. It's an SFNC unit, not PySpin; move it into `GenICamHardwareTests`, skipped when the camera reports no resulting frame rate.
- `SpinnakerSystem().discover()` after reopening in `reopening_releases_the_sdk_each_time`. Keep it as a Spinnaker-specific check.

### Verification

- [x] The full suite passes on every supported Python version. The hardware suite passes on the CM3.
  - Locally on 3.15 (1914 passed) and 3.12 with PySpin (1963 passed); CI covers the other versions on push.

## Phase 2: Setting descriptors

### Tasks

- [x] Task 2.1: Decide the descriptor's interface: how it reads a `Setting` (or `None` when the camera lacks the feature), how it writes, and how native settings are covered without the name-equals-node fallthrough. Use design-it-twice if there's no clear winner. Record the choice here.
  - A descriptor is a small private object with a `name`, `read(camera) -> Setting | None` (None when the camera lacks the feature) and `write(camera, value)`, taking a value `Setting.validate` has accepted. It reaches the node map through the camera's helpers (`_call`, `_set_enum`, `_acquisition_paused`, `_raise_frame_rate`).
  - The camera builds an ordered `_descriptors` dict once, in `_configure()`. Per-camera differences are settled there: the exposure node, dB gain or the IDS's linear gain (its steps, or a factor range), and the resulting frame rate's fallback node.
  - `_settings()` reads every descriptor, `_setting(name)` reads one (`KeyError` for an unknown name or a missing feature), and `_set_setting()` writes one. `ExposureAuto` and `GainAuto` are entries of their own, so there's no fallthrough.
  - Rejected: a table of (reader, writer) method pairs on the camera. Fewer types, but the vendor branches would stay inside the methods.
- [x] Task 2.2: Measure a `set_setting` on the CM3 and the IDS, as the baseline.
  - `GenICamHardwareTests.test_time_to_read_and_write_one_setting` prints the median of 20 for `exposure_time`; the tag `genicam-settings-baseline` marks the code before descriptors. On 2026-11-05:

    | Camera | Backend | `settings()` | `setting()` | `set_setting()` |
    |---|---|---|---|---|
    | CM3 20305122 | Spinnaker | 1.1 ms | 1.0 ms | 1.7 ms |
    | CM3 20305122 | GenTL | 0.9 ms | 0.9 ms | 1.6 ms |
    | CM3 0135BAD4 | Aravis | 32.6 ms | 31.8 ms | 36.4 ms |
    | IDS 4110091493 | Aravis | 36.9 ms | 35.1 ms | 139.4 ms |

  - Spinnaker's node map (and its GenTL producer) answers reads without going to the camera, so descriptors can't save much there; Aravis reads the camera each time.
- [ ] Task 2.3: Write failing tests on `FakeBackend`: `setting(name)` and `set_setting(name, …)` read only that setting's nodes. `setting(name)` equals `settings()[name]` for every setting. An unknown name raises `KeyError`.
- [ ] Task 2.4: Build the descriptors. Make `_settings()`, `_setting(name)` and `_set_setting()` lookups, and remove the if/elif and the duplicated scaling.
- [ ] Task 2.5: Measure again, and record both times in `docs/hardware/spinnaker.md` and `docs/hardware/ids-u3-36p0xcp.md`.

### Verification

- [ ] The core, backend and contract tests pass, unchanged except for the new tests.

## Phase 3: Public surface and leaks

### Tasks

- [ ] Task 3.1: Export `GenICamCamera`, `TRIGGER`, `THROUGHPUT_LIMIT` and `host_time` from `educelab.devices.cameras`, and switch `app/_capture.py` to them.
- [ ] Task 3.2: Document the GenICam frame metadata keys in `docs/guide.md`.
- [ ] Task 3.3: Give `Setting.nearest()` a way to refuse values more than half a step past either end, with tests. Use it in `_capture.py`.
- [ ] Task 3.4: Remove `aravis.py`'s import of `_EXPOSURE_NODES`. Hand the drain what it needs from the core through the seam, with a `BackendContract` test if the seam changes.

### Verification

- [ ] The full suite, Ruff and mypy pass.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
