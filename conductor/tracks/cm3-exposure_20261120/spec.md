# Specification: Exposure Integrity Across Capture Modes

**Track ID:** cm3-exposure_20261120
**Type:** Bug
**Created:** 2026-11-20
**Status:** Draft

## Summary

A GenICam camera's frame must depend only on its exposure and gain, whatever capture
mode the app chose and whatever modes the camera went through before. The exposure an
app sets must be the exposure integrated, or the write must raise. `exposure_time`'s
limits must be true for the camera's current state. Today, on FLIR Chameleon3 cameras,
none of these holds.

## Context

Reported from pgs-scan (`cm3-exposure-issue.md`, 2026-11-20): five CM3-U3-50S5M on one
USB 3 link, Aravis 0.8 on macOS, Mono16, gain 0 dB, a 99 % Spectralon under an XLC2 at
100 %.

The capture mode is the app's choice. An app may free-run all the time, stay on a
software trigger all the time, or switch, for captures and preview alike (ADR 0007:
preview shares the trigger mode). The library guarantees behavior in every mode and
documents best practices; it doesn't pick a mode for the app.

The GenICam core (`_genicam.py`) is shared by the Spinnaker, Aravis and GenTL backends
(ADR 0006), so every change here reaches all three.

## Problem Description

**1. Exposures are trimmed silently.** On the CM3, a request for 36.6 ms kept 27.872 ms
with no error, while `exposure_time.maximum` said 52 ms. `set_setting` validates against
`ExposureTime`'s range at write time (`base.py`), so the range allowed more than the camera
then kept. `_Float.write` never reads back what was kept.

What causes the cap is not established. In the report's state table, `Auto = Off` and
`Enabled = false` whenever `TriggerMode = On`, which the driver never writes outside
preview (probably firmware). 27.87 ms is one period at the sensor's whole-frame rate of
35.7 Hz, not at Mono16's throughput-limited 19 Hz. The report also contradicts itself:
it says scan cameras stay on `Continuous`, but its "triggered as a scan" state reads
`Off` and kept 40 ms.

**2. The same exposure reads 2.3× differently.** At 11 ms and gain 0:

| State | How it got there | Level | Level per ms |
| --- | --- | --- | --- |
| a | free-running (`trigger = "Off"`) | 0.261 | 0.0237 |
| b | idle on `"Software"` after pgs-scan's `_idle_hidden_cameras`, `capture_frame` fires | 0.609 | 0.0554 |
| c | on `"Software"`, the app calls `trigger()` first | 0.259 | 0.0235 |

- **The driver doesn't cause the difference.** It sends the same commands in states b
  and c (discard, `TriggerSoftware`, grab), so the difference must be camera state left
  by history.
- **Only one path has produced the bright state.** pgs-scan's `_idle_hidden_cameras`
  switches several cameras' triggers concurrently (each stops, restarts and primes its
  stream on the shared link), then lowers their throughput limits while they acquire.
  It may switch the pixel format first.
- **States a and c look correct.** Their signal is proportional through zero (0.0237,
  0.0234, 0.0230 per ms at 11, 27.9 and 40 ms). State b was measured at one exposure
  only.
- **One untested hypothesis:** in state b each row keeps integrating until it is read out
  (11 ms plus about 14 ms on average at 35.7 Hz, ≈ 2.27×).

**3. Timing knowledge is spread through the core.** About 12 places decide the frame
rate or what limits the exposure:
- `_raise_frame_rate`, with 6 call sites
- `_hold_frame_rate` and `_release_frame_rate`
- `_keep_exposure`, `_exposure_caps` and `_start_whole_frame_preview`
- `_frame_period`, `rate_nodes` and `moves_rate`

The triggered path holds none of this knowledge: `_Trigger.write` never touches the
rate. The shared fake models the exposure ceiling from image height alone, and paints
`frames_grabbed % 200`, so no test could catch a trimmed exposure or a mode-dependent
signal. `test_exposure_time_round_trips` only checks that metadata repeats the node
just written.

## Design (agreed 2026-11-20, provisional where marked)

**A timing module, private to the GenICam core.**
- **It records the app's request:** the exposure and `frame_rate`, nothing else.
- **It checks the camera against that record** after open, after every write flagged as
  affecting timing (through `_set_setting`), after every restart (through
  `_acquisition_paused`), and at preview start and stop. *Provisional:* the probe maps
  how timing settings constrain each other, and the decision stop revisits whether
  "set it back" is possible.
- **An exposure write runs in five steps:**
  1. work out the rate the exposure needs
  2. lower the rate to make room
  3. write the exposure
  4. fit the rate to the exposure
  5. read back what the camera kept
- **A trimmed exposure raises.** If the camera keeps less than requested, beyond its
  quantization, the write raises `CommandRejected` naming the requested and kept values.
  The new state stays and the record takes the kept value. *Revisited at the decision
  stop.*
- **Trigger switches leave a known state.** A switch to `"Software"` turns rate control
  off. Switching back to `"Off"` reapplies the recorded `frame_rate`. Further node
  normalization follows whatever the probe finds causes the 2.3×.
- **It replaces the scattered code.** It absorbs `_raise_frame_rate` and
  `_hold_frame_rate`, removes `_release_frame_rate`, and drives preview's host-crop
  fallback (whole frames cropped on the host when a smaller image would shorten the
  exposure) through its own read-back instead of `_exposure_caps`.

**`frame_rate`, a new writable setting.**
- **`None` (the default):** the fastest rate the exposure, image size and throughput
  allow.
- **A number:** the app's rate is held, and `exposure_time.maximum` shrinks to one frame
  period.
- **Under a software trigger** it limits nothing.
- **Offered only on cameras whose rate behavior was verified on hardware:** the CM3s and
  the IDS. Other GenICam cameras get the exposure read-back but no `frame_rate`.
- **The IDS's self-slowing quirk becomes what `None` means there.**
- **Opens as found:** a fixed rate opens as that number, an automatic rate as `None`.
  The driver takes over rate control to deliver `None`, and doesn't restore anything on
  close.
- **In preview under `None`**, whether a small region runs faster or holds the whole
  image's rate is decided at the decision stop, from probe check (d). Either way, a
  preview never shortens the exposure.

**Frame metadata.** `metadata["exposure_time"]` becomes the exposure the timing module
verified. No other keys are added (see Response to the report).

## Acceptance Criteria

**Guarantees, in every capture mode and after any history of mode switches**
- [ ] One exposure and gain give the same signal, within 3 %, whether free-running,
      triggered by the capture, triggered by `trigger()`, or after switches between
      them, on the five CM3-U3-50S5Ms, the CM3-U3-31S4C and the IDS U3-36PxXCP.
- [ ] Setting `exposure_time` to `maximum` and capturing integrates that exposure, in
      each mode and at each `frame_rate`.
- [ ] A write the camera would trim raises `CommandRejected`. A request above `maximum`
      raises `ValueError` before anything is written.
- [ ] Preview never shortens the exposure as it crops or reduces, verified by signal on
      the CM3s and the IDS.

**`frame_rate`**
- [ ] `frame_rate` is listed on the CM3s and the IDS, opens as found, and behaves as in
      Design.
- [ ] The IDS's rate behavior is unchanged in effect: a long exposure lowers the rate,
      and a shorter one raises it again.

**Tests**
- [ ] The shared fake gains an opt-in scene and timing profile built from probe data,
      with sources cited. The existing `frame_id % 200` pixels stay the default.
- [ ] `FrameCameraContract` checks that the signal is proportional to exposure × gain,
      on `SimulatedCamera` and on fakes using the scene.
- [ ] A new GenICam timing contract runs on the three backends' fakes and on hardware
      through `genicam_hardware.py`. It checks that limits are true now, that a request
      the camera would trim raises, and that the signal doesn't depend on mode history.
      It replaces `test_exposure_time_round_trips`.
- [ ] `el-devices preview` gains exposure and frame-rate keys and an overlay showing the
      requested and verified exposure and `resulting_frame_rate`. A hands-on check on
      the CM3 and the IDS finds no lost exposure, a rate that recovers after a short
      exposure, and no stutter.

**Docs**
- [ ] `docs/exposure.md` (new): the guarantees, `frame_rate`, which writes move which
      limits and when to re-read them, and what `CommandRejected` from a trimmed
      exposure means.
- [ ] `docs/multi-camera.md` (new): link budgets, idling cameras on their trigger,
      synchronized shots with `trigger()`, and what a mode switch costs.
- [ ] `docs/preview.md`'s exposure section links to `exposure.md` and keeps only what's
      specific to preview. `docs/guide.md` gets short sections linking to both new pages.
- [ ] `docs/hardware/spinnaker.md`, `usb3-vision.md` and `ids-u3-36p0xcp.md` record the
      probe's findings and the new behavior.
- [ ] `docs/hardware/onboarding/genicam-cameras.md` (new) lists what to check for a new
      GenICam model and what to record: pixel formats and packing, saturation, gain
      units and steps, throughput ranges for each format, trigger response and
      timestamps, and frame-rate behavior.
- [ ] CLAUDE.md requires running a category's onboarding checklist for a new model, and
      writing a checklist when a new category of device is added.
- [ ] ADR 0007 gains a dated amendment: preview frames carry the same exposure
      guarantees as captures, and preview no longer manages the frame rate. Its wording
      is approved by the maintainer.
- [ ] A new ADR, only if the decision stop leaves a decision worth recording.

## Response to the report

- **Which state integrates the exposure it reports, and what leaves a CM3 dim or bright?**
  Answered by the probe; recorded in the hardware docs.
- **Exposure that holds, or says so.** Done by the timing module: the five-step exposure
  write, the read-back, and `CommandRejected`.
- **Limits that are true now.** Done: `exposure_time.maximum` for the current state, and
  `frame_rate`.
- **Capture modes as one setting: declined.** `trigger` already is that setting. The
  library makes its values equivalent rather than adding a second name for the same
  switch.
- **A frame records how it was taken: declined.** `trigger` and `throughput_limit` are
  set by the app, and `resulting_frame_rate` follows from settings the app chose. Once
  the guarantees hold, none of them can change pixels. Metadata holds what the camera
  determined; `exposure_time` becomes the verified value.
- **Read-only diagnostics as settings: in part.** `frame_rate` is now a setting, and
  `resulting_frame_rate` stays as it is. `shutter_mode` is left out: the report found it
  doesn't separate the dim state from the bright one.
- **A hardware check across capture modes.** Done: the timing contract on hardware.

## Dependencies

- **Code:** `_genicam.py`, `tests/fakes/genicam.py`, `tests/contracts/cameras.py`,
  `tests/contracts/genicam_hardware.py`, and `src/educelab/devices/app/preview.py`.
- **The rig:**
  - one CM3-U3-50S5M with a Spectralon in view, for the full probe
  - the other four 50S5Ms, the CM3-U3-31S4C and the IDS, each with a still scene
  - the XLC2 at a fixed level, with room lights held constant
- **Other tracks:** none incomplete. `color-cameras_20261119` touches the same metadata
  code; rebase if both are open.

## Out of Scope (filed as issues)

- Vendor timing adapters behind a seam (architecture review, candidate 2): #46.
- The rest of the single acquisition path: descriptors no longer writing camera state,
  and one restart sequence (candidate 3): #47.
- A control over preview's host-crop fallback: #48.
- Onboarding checklists for other device categories: #49.
- A link-budget helper, from pgs-scan's `link_budget.split`: #50.
- The CM3 resetting the throughput limit on a pixel format change: #51.
- Changes to pgs-scan.

## Technical Notes

- **Probe first** (maintainer, 2026-11-20). This is an exception to batching hardware
  runs at the end of a track, because the design depends on what it finds.
- **The pages added to `docs/`** follow `preview.md`'s pattern: a page at the top of
  `docs/` that the guide links to.
- **Changes needing maintainer approval:** the ADR 0007 amendment and any new ADR. No
  role ABC signature changes.

---

_Generated by Conductor. Review and edit as needed._
