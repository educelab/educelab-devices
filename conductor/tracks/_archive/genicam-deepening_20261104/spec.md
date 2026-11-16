# Specification: GenICam Deepening

**Track ID:** genicam-deepening_20261104
**Type:** Refactor
**Created:** 2026-11-04
**Status:** Complete

## Summary

Make the GenICam core cheaper to test and to change before camera preview mode builds on it. The Spinnaker fake moves onto the shared GenICam fake, so every GenICam behaviour is simulated and tested once. Each GenICam setting becomes one descriptor that knows how to read and write it. A few smaller leaks are fixed alongside: `GenICamCamera` gets a public home, the gain-rounding rule moves onto `Setting`, and Aravis stops reaching past the backend seam.

From the architecture review of 2026-11-04. Lands before Phase 2 of `preview-mode_20261102`.

## Context

ADR 0006 put the GenICam behaviour in one core behind a backend seam: a node map, a stream, and discovery. The Aravis and GenTL fakes (`tests/fakes/aravis.py`, `tests/fakes/gentl.py`) are library-shaped skins over one in-memory camera, `fakes.genicam.FakeCamera`, and both run `BackendContract`. The Spinnaker fake predates the core and was kept unchanged through the migration. ADR 0006 has been amended to say it now moves.

## Problem Description

**Two simulations of the same camera.**
- `tests/fakes/pyspin.py` (1154 lines) has its own node classes and its own `FakeCamera`, with its own trigger, timestamp latch and buffer pool.
- 74 of the 91 tests in `test_spinnaker_camera.py` share names with tests in `test_genicam_core.py`.
- The PySpin backend has no `BackendContract` subclass.
- `test_spinnaker_hardware.py` (280 lines) repeats checks that `contracts/genicam_hardware.py` already makes for Aravis and GenTL.
- Preview mode needs ROI, binning and decimation in the fakes. As things stand they'd have to be built twice.

**Reads and writes of a setting live apart.**
- `_settings()` (`_genicam.py:696-722`) builds each `Setting` from a per-node builder that knows the node and its scale.
- `_set_setting()` (`_genicam.py:724-758`) maps the name back to a node and scale in an if/elif. `_US_PER_S` is applied at 729 and again at 763, and the gain-table and linear-gain branches are split across 730-734 and 771-832.
- The fallthrough at 758 assumes the native name is the node name.
- The core doesn't override `_setting(name)`, so every `setting()` and `set_setting()` builds every setting first, about 40 node reads, many of them USB register reads. `app/_capture.py` and auto-exposure loops pay this cost on each write.

**Smaller leaks.**
- `app/_capture.py` imports `GenICamCamera`, `TRIGGER`, `THROUGHPUT_LIMIT` and `host_time` from the private `cameras._genicam`, and reads GenICam-only metadata keys that aren't documented. pgs-scan would have to do the same.
- `_capture.py:240-249` holds a rounding rule that belongs to `Setting`: take the nearest choice, but refuse a value more than half a step past either end.
- `aravis.py:24` imports the core's private `_EXPOSURE_NODES` and reads the exposure to time its stream drain (406-414). That puts GenICam knowledge below the backend seam.

## Acceptance Criteria

- [x] `tests/fakes/pyspin.py` is a PySpin-shaped skin over `fakes.genicam.FakeCamera` and holds no camera simulation of its own.
- [x] `TestPySpinBackend(BackendContract)` passes.
- [x] `test_spinnaker_camera.py` keeps only PySpin-specific behaviour: `System` sharing and refcounting, the SDK version check, error-code and timeout translation, the image-status mapping, no-chaining, and `discover`. The duplicated tests are deleted, not kept as a smoke subset.
- [x] `test_spinnaker_hardware.py` subclasses `GenICamHardwareTests`, keeps only Spinnaker-specific checks, and passes on the CM3.
- [x] Each GenICam setting is one descriptor that reads and writes it. `_settings()`, `_setting(name)` and `_set_setting()` are lookups.
- [x] `setting(name)` and `set_setting(name, …)` read only the nodes that setting needs. A test on the fake backend counts node reads. `setting(name)` equals `settings()[name]` for every canonical and native setting.
- [x] The time of a `set_setting` on the CM3 and the IDS, before and after, is recorded in their hardware docs.
- [x] `educelab.devices.cameras` exports `GenICamCamera`, `TRIGGER`, `THROUGHPUT_LIMIT` and `host_time`. `_capture.py` imports only public names. `_genicam` stays private.
- [x] `docs/guide.md` documents the GenICam frame metadata keys.
- [x] `Setting.nearest()` can refuse values more than half a step past either end, and `_capture.py` uses it.
- [x] `aravis.py` no longer imports from `_genicam` past the backend seam.

## Dependencies

- Existing code: `cameras/_genicam.py`, `cameras/spinnaker.py`, `cameras/aravis.py`, `cameras/__init__.py`, `settings.py`, `app/_capture.py`. Tests: `tests/fakes/{pyspin,genicam}.py`, `tests/contracts/{genicam,genicam_hardware}.py`, `tests/test_spinnaker*.py`, `tests/test_fake_pyspin.py`, `tests/test_cli_spinnaker.py`, `tests/test_genicam_core.py`.
- Hardware: the CM3-U3-50S5M (Spinnaker, Python 3.12) and the IDS U3-36P0XCP for the timing and hardware suites.
- Blocks Phase 2 of `preview-mode_20261102`.

## Out of Scope

- Preview mode itself, including the capture-geometry record in the core. The preview-mode plan carries those inputs.
- Making setting descriptors a mechanism shared with gphoto2 or the simulated cameras. gphoto2 already reads one widget per setting, and one adapter doesn't justify a shared seam.
- gphoto2's `_writers` ordering rule. It's guaranteed by `Camera.set_setting` and documented in place.
- Changing the backend seam (`Connection`, `Buffer`), except as far as Aravis's drain needs.

## Technical Notes

- The descriptor's interface is decided in Phase 2 of the plan, with design-it-twice if it's contested.
- `fakes.genicam.FakeCamera` may need to grow to cover what only the PySpin fake simulates today. Grow the shared fake; don't add to the skin.
- ADR 0006 was amended on 2026-11-04 to allow the test move.

---

_Generated by Conductor. Review and edit as needed._
