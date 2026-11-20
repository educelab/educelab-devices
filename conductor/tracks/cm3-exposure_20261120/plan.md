# Implementation Plan: Exposure Integrity Across Capture Modes

**Track ID:** cm3-exposure_20261120
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-20
**Status:** [ ] Not Started

**Phase checkpoints:** stop for maintainer verification after each phase (workflow
default). Phase 1 ends with a decision stop. Hardware runs are batched in Phase 5, except
the probe, which the design depends on (maintainer, 2026-11-20).

## Overview

Probe first: find which CM3 state is true, how the timing settings constrain each other,
and whether the frame rate can be fitted without shortening the exposure. Then build a
fake that can be wrong in the ways the camera is, and write contracts that fail against
today's code. A private timing module in the GenICam core then makes them pass. It
records the requested exposure and `frame_rate`, and checks the camera against them
after every change that affects timing. Viewer keys, docs, and one hardware session
finish the track.

## Phase 1: Probe

A scripted probe, kept in this track's folder (`probe/`) so it can be rerun. It drives the
cameras through the library, reading nodes directly through the connection where needed.
It logs every `Acquisition*`, `Trigger*`, `Sensor*`, `Exposure*` and
`DeviceLinkThroughput*` node in each state.

**Prep (maintainer):**
- one CM3-U3-50S5M with a Spectralon filling a region of its view
- the other four 50S5Ms, the CM3-U3-31S4C and the IDS U3-36PxXCP, each with any still,
  unsaturated scene that has some detail
- the XLC2 at a fixed level, with room lights held constant (or off)

Nothing moves during a camera's run.

### Tasks

- [ ] Task 1.1: Write the probe script with these checks:
  - **(a)** Which state is true: states a, b and c at 3 or more exposures, each level
    fitted against exposure for its intercept, and a row profile of a flat target in
    each state.
  - **(b)** Replay pgs-scan's `_idle_hidden_cameras` sequence through the library:
    concurrent trigger switches, throughput caps written while acquiring, and pixel
    format switches. Drop one step at a time to find which leaves a camera bright. Vary
    the throughput limit in the bright state.
  - **(c)** Rate nodes and the exposure ceiling, free-running and triggered:
    - Does `AcquisitionFrameRateAuto = Off` with `AcquisitionFrameRateEnabled = false`
      make `ExposureTime`'s range the sensor's own?
    - What lifts the triggered 27.9 ms cap?
    - Is the range stale until a restart?
    - Build a table of which writes move which other settings' ranges: pixel format,
      throughput, ROI and decimation, trigger, rate, exposure.
  - **(d)** Fit the rate to a long exposure through ROI and decimation changes, as a
    preview does. Check the exposure is kept by signal, not only by the node.
  - **(e)** Time a trigger switch, and how long each step of it takes.
  - **Cross-mode comparison:** one exposure, free-running against triggered, and after
    a mode switch.
- [ ] Task 1.2: Run it:
  - **One 50S5M:** everything, (a) to (e).
  - **The other 50S5Ms:** the cross-mode comparison.
  - **The 31S4C:** the cross-mode comparison, (c) and (d), in Mono16 and RGB8.
  - **The IDS:** the cross-mode comparison, (c), (d) and (e).
- [ ] Task 1.3: Write up the findings in this plan, under Probe results, and in the
  hardware docs (`spinnaker.md`, `usb3-vision.md`, `ids-u3-36p0xcp.md`).

### Decision stop

- [ ] Whether the camera can be "set back" to the requested exposure and `frame_rate`,
  given the table from (c) of which writes move which ranges.
- [ ] Preview's rate under `frame_rate = None`: run faster, or hold the whole image's
  rate (from (d)).
- [ ] What happens when the camera trims an exposure: keep the state and raise, or undo
  the write.
- [ ] Which nodes a trigger switch must set, from (a) and (b).
- [ ] The FLIR and IDS numbers for the fake's profiles.
- [ ] ADR 0007's amendment worded, and whether a new ADR is needed.

## Phase 2: Fake and Contracts

### Tasks

- [ ] Task 2.1: Add an opt-in scene and timing profile to the shared fake
  (`tests/fakes/genicam.py`), with tests of the fake itself:
  - integrated exposure = min(requested, the ceiling for the current state)
  - pixels = scene × integrated exposure × gain
  - FLIR and IDS profiles built from Phase 1, with sources in their docstrings
  - the default stays the `frame_id % 200` counter
- [ ] Task 2.2: Add the check that signal is proportional to exposure × gain to
  `FrameCameraContract`, running on `SimulatedCamera` and on fakes using the scene.
- [ ] Task 2.3: Add a GenICam timing contract (`tests/contracts/`) for the three
  backends' fakes and `genicam_hardware.py`:
  - limits are true now, in each mode and at each `frame_rate`
  - a request the camera would trim raises
  - the signal doesn't depend on mode history
  - replaces `test_exposure_time_round_trips`

  It fails against the current code; its failures are marked expected until Phase 3.
  If the 2.3× can't be modelled from Phase 1's findings, note that the mode-history test
  only proves anything on hardware.

### Verification

- [ ] Full suite: `pytest`, `ruff check .`, `ruff format --check .`. The new contract
  fails only where expected.

## Phase 3: Timing Module and `frame_rate`

### Tasks

- [ ] Task 3.1: The timing module, private to the GenICam core:
  - records the requested exposure and `frame_rate`
  - checks the camera against them after open, after writes flagged as affecting timing
    (through `_set_setting`), after restarts (through `_acquisition_paused`), and at
    preview start and stop
  - descriptors declare whether a write affects timing; `moves_rate` goes
- [ ] Task 3.2: The five-step exposure write: work out the rate, make room, write, fit,
  read back. A trimmed exposure raises `CommandRejected` as decided at the stop.
- [ ] Task 3.3: The `frame_rate` setting on the CM3s and the IDS:
  - opens as found
  - `None` is the fastest the exposure allows, a number is held
  - the IDS's `_raise_frame_rate` becomes what `None` means there
  - `_SELF_SLOWING_VENDORS` stays as the switch inside the module
- [ ] Task 3.4: Trigger switches set the rate-mode nodes (and whatever Phase 1 found) to
  a known state. Switching back to `"Off"` reapplies the recorded `frame_rate`.
- [ ] Task 3.5: Rewire preview through the module:
  - `_hold_frame_rate` and `_release_frame_rate` go
  - the host-crop fallback is driven by the module's read-back, and `_exposure_caps` goes
  - the preview rate under `None` as decided at the stop
- [ ] Task 3.6: `metadata["exposure_time"]` is the exposure the module verified.
  `_frame_period` reads the rate from the module.

### Verification

- [ ] Full suite passes, with the Phase 2 contract's expected failures now passing.

## Phase 4: Viewer and Docs

### Tasks

- [ ] Task 4.1: `el-devices preview` gains:
  - keys to step the exposure up and down and to cycle `frame_rate` (`None` and a few
    fixed rates)
  - an overlay showing the requested and verified exposure and `resulting_frame_rate`
- [ ] Task 4.2: Write `docs/exposure.md` and `docs/multi-camera.md`, trim
  `docs/preview.md`'s exposure section to link to them, and add short sections to
  `docs/guide.md` linking out.
- [ ] Task 4.3: Write `docs/hardware/onboarding/genicam-cameras.md`, and add the two
  CLAUDE.md rules: run a category's onboarding checklist for a new model, and write a
  checklist when a new category of device is added.
- [ ] Task 4.4: Add ADR 0007's amendment as worded at the decision stop, and the new ADR
  if one was decided.

### Verification

- [ ] Full suite passes. Docs build, if the docs site exists by then.

## Phase 5: Hardware Session

### Hardware runs owed

- [ ] The timing contract through Aravis on the five CM3-U3-50S5Ms, the CM3-U3-31S4C and
  the IDS. On one 50S5M and the IDS, also through Spinnaker or GenTL where installed.
- [ ] The cross-mode comparison on every camera, within 3 %.
- [ ] A hands-on viewer check on a CM3 and the IDS, run by the maintainer:
  - step through long and short exposures while zooming and panning
  - the exposure is never lost
  - the rate recovers after a short exposure
  - the preview doesn't stutter
- [ ] `el-devices genicam` and `pytest -m hardware` on each backend installed.
- [ ] Results recorded in the hardware docs.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

## Probe results

_Filled in by Task 1.3._

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
