# Implementation Plan: gphoto2 Camera

**Track ID:** gphoto2-camera_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12
**Status:** [~] In Progress

## Overview

Establish the session layer and a fake gphoto2 seam first, then capture, settings, live view and keep-alive, then the focus drive, and finish with docs, CLI, and verification on the Z7 II.

## Phase 1: Session and test seam

### Tasks

- [x] Task 1.1: Declare the `[gphoto2]` extra with the Windows marker; lazy import; logging bridge.
- [x] Task 1.2: Define the thin internal interface over gphoto2 calls the driver uses, and a fake implementing it for tests.
- [x] Task 1.3: Write tests for open/close, `-53` → `DeviceBusy`, `-105` retries → `DeviceNotFound`; implement.
- [x] Task 1.4: Implement `discover()`.

### Deviations

- `-52` (`GP_ERROR_IO_USB_FIND`) is retried like `-105`: with an explicit port, a camera
  that is still re-enumerating is assumed to fail with `-52` rather than `-105`
  (unknown; see docs/hardware/gphoto2-cameras.md#opening-a-session).
- Without a port, `open()` autodetects and refuses to choose when several cameras are
  attached, rather than letting libgphoto2 open whichever it finds first; rigs of
  several DSLRs would otherwise open the wrong body silently.
- `docs/hardware/gphoto2-cameras.md` was started here (session errors, `ptpcamerad`)
  because the `DeviceBusy` message links to it; Task 5.1 completes it.

### Verification

- [x] Session tests pass on Linux and macOS CI; skipped on Windows.
  - Approved 2026-10-15, after CI on draft PR #4 passed on every OS and Python version.
  - Decided (2026-10-15): drop the Windows skip. The tests use the fake layer and run on Windows; only the extra can't install there.
  - Decided (2026-10-15): add Pillow to the `dev` dependency group so preview and contract tests run in CI. It stays in the `[gphoto2]` extra too.
  - Decided (2026-10-15): `all` stays last among the extras; that is the convention.
  - Decided (2026-10-15): leave `DeviceInfo.serial_number` unset for now. Phase 5 measures how long a config read at open takes on the Z7 II and decides then.

## Phase 2: Capture

### Tasks

- [x] Task 2.1: Write tests for single-file and multi-file (RAW + JPEG) captures, download retries, and the in-flight guard; implement `capture_files()`.
- [x] Task 2.2: Run the `FileCamera` contract suite.
  - The `Device` and `FileCamera` rules pass. The inherited `Camera` rules need settings and live view, so they are strict xfails until Task 3.4 removes the marker.

### Deviations

- Capture drains events before the release as well as after, and discards (with a
  warning) any file added before the release. The prototype downloaded only the first
  file, so it never had to decide which capture a late file belongs to; returning it
  with the next capture would silently pair the wrong files.
- "A capture in flight blocks concurrent captures" is the device lock: a second
  `capture_files()` waits for the first. The internal `_capture_in_flight` flag exists
  for the keep-alive thread (Phase 3), which must not wait on the bus.

### Verification

- [x] Capture tests and contracts pass.
  - Approved 2026-10-15, after CI on PR #4 passed all 14 jobs.
  - Decided (2026-10-15): the `-53` `DeviceBusy` hint is platform-specific: `ptpcamerad` and the `killall` workaround on macOS, `gvfs-gphoto2-volume-monitor` on Linux, and a generic message elsewhere. It never names a daemon the host doesn't run.

## Phase 3: Settings, live view, keep-alive

### Tasks

- [x] Task 3.1: Write tests for `Setting` descriptors from the config tree and `exposure_time` ↔ shutter-choice mapping; implement.
- [x] Task 3.2: Write tests for live-view enabling and `preview_frame()` decoding; implement.
  - With preview working, the whole `Camera` contract passes, so this task removed the strict-xfail marker from `TestGPhoto2Contract`.
- [x] Task 3.3: Write tests for keep-alive start/pause/stop; implement the internal thread.
- [x] Task 3.4: Run the `Camera` contract suite.
  - All 16 `Device`, `Camera`, and `FileCamera` rules pass, with keep-alive pinging every 5 ms throughout.

### Deviations

- `iso` and `aperture` are canonical as well as `exposure_time` (from `iso` and
  `f-number`/`aperture`), since the guide defines them and Nikon's `iso` widget would
  otherwise take the canonical name with string values.
- Canonical choices are the native choices that parse as numbers. When the camera is set
  to a value that doesn't (`Bulb`, `Auto`), the canonical setting is absent and the
  widget is listed under its native name.
- `close()` turns live view off if the driver turned it on (the prototype left it on).
- Keep-alive skips an interval when the device lock is held as well as during a capture,
  and logs a failed ping instead of emitting `Disconnected`.
- Widgets in the config tree's `actions` section are not settings. `Widget` gained a
  `section` field so the seam can tell.

### Verification

- [x] All tests and contracts pass.
  - Approved 2026-10-15, after CI on PR #4 passed all 14 jobs.
  - Decided (2026-10-15): pause live view around aperture reads and writes only if Phase 5 shows the Z7 II restricts aperture choices while live view is on (Task 5.6).
  - Decided (2026-10-15): after 3 consecutive failed keep-alive pings, emit `Disconnected` to listeners, still logging each failure.
  - Pending: how canonical settings behave while the camera is at a non-numeric value (`Bulb`, `Auto`). Left as is (absent, with the widget under its native name) until the maintainer decides.

## Phase 4: Focus drive

### Tasks

- [x] Task 4.1: Write tests for `step()` chunking, settle waits, busy retry, `FocusLimitReached`, and the step counter; implement.
- [x] Task 4.2: Write tests for `drive_to_limit()`; implement.
  - `_drive_to_limit()` landed with Task 4.1, since it shares `_move()`; these tests pin its chunking, counter reset, give-up limit, and failure handling.
- [x] Task 4.3: `cam.focus` is `None` when `manualfocusdrive` is missing or not range-type.
- [x] Task 4.4: Run the `FocusDrive` contract suite.
  - All 14 rules pass for `camera.focus` on the fake, with keep-alive pinging throughout.

### Deviations

- `cam.focus` is a separate `GPhoto2FocusDrive` (not a second base class of the camera),
  because whether a camera can drive focus is only known once it is open. It shares the
  camera's connection and lock: it opens with the camera, closes (forgetting its count)
  when the camera closes, and closing it leaves the camera open. The same object is
  returned on every connection.
- `cam.focus` raises `NotConnected` while the camera is closed, rather than returning
  `None`, which would wrongly say the camera can't drive focus. The first access per
  connection reads the config tree once.
- After an end stop the driver drains events and waits 1 s before the next command, as
  the a6300 code did.

### Verification

- [x] Focus tests and contracts pass.
  - Approved 2026-10-15, after CI on PR #4 passed all 14 jobs.
  - Decided (2026-10-15): a canonical setting is always present when the camera has its widget, with numeric `choices`. While the camera is at a value that isn't a number (`Bulb`, `Auto` ISO), its `value` is `None`, and writing any numeric choice leaves that mode. This needs `Setting.value` to allow `None`, which lands on main in a separate PR first; implement it after rebasing onto that, replacing Phase 3 deviation 2.
  - Decided (2026-10-15): at merge time, once the Foldio track's guide wording is on main, add `camera.focus` to `docs/guide.md` as a second example of a role handed out as a separate object.
  - Decided (2026-10-15): `-113` as the end stop is confirmed on the Z7 II in Task 5.4.

## Phase 5: Docs, CLI, hardware verification

### Tasks

- [ ] Task 5.1: Write `docs/hardware/gphoto2-cameras.md` (general gphoto2 behavior plus a Nikon Z7 II section).
- [ ] Task 5.2: Add the `el-devices dslr` subcommand.
- [ ] Task 5.3: Write `@pytest.mark.hardware` tests (capture RAW and RAW + JPEG, set exposure, preview, focus step and limits).
- [ ] Task 5.4: Run them on the Z7 II; confirm error codes and settle timing; record findings in the hardware doc.
- [ ] Task 5.5: Measure how long a config read at `open()` takes on the Z7 II, and decide whether `open()` reads the serial number into `DeviceInfo.serial_number`.
- [ ] Task 5.6: Check whether the Z7 II restricts aperture choices while live view is on; if it does, pause live view around aperture reads and writes.

### Verification

- [ ] Maintainer confirms capture, settings, live view, and focus on the Z7 II.

## Waiting on main

- [ ] After `Setting.value` allows `None` on main: rebase, then make canonical settings always present with `value=None` at non-numeric camera values (Phase 4 decision).
- [ ] After the Foldio track's guide wording is on main: add `camera.focus` to `docs/guide.md`.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
