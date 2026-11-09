# Specification: GenTL Stream Stall

**Track ID:** gentl-stall_20261109
**Type:** Bug
**Created:** 2026-11-09
**Status:** Draft

## Summary

A free-running CM3 on the Windows rig stopped delivering frames through FLIR's GenTL producer and never recovered while open. Find out why before fixing anything: make the `el-devices genicam` report show enough to tell the causes apart, reproduce the stall on the rig, then fix what the evidence shows. Issue #32.

## Context

pgs-scan runs five CM3-U3-50S5M on one hub. On Windows it reaches them through `GenTLCamera`. A camera that stops sending fails every capture until it's closed, which would fail a whole scan. pgs-scan's free-running preview, and any free-running scan, can hit this.

## Problem Description

**What happened** (2026-11-09, educelab-devices 0.4.0, Windows rig, five CM3s on one hub):

```
el-devices genicam --backend gentl --pixel-format Mono8 --exposure min --throughput-limit 80 --frames 100
```

- 20305113, opened last, delivered 3 frames, then none: 97 captures raised `DeviceTimeout ... no frame in 5 s`, about 8 minutes.
- No incomplete frames or missing trailers, from any camera.
- The other four captured 100/100 at 16 frames/s and 80.4 MB/s each. The 5.4 s all-camera median is the stalled camera's timeout.
- A new process a minute later opened 20305113 fine. Seven other runs (Mono8/Mono16, free-running/triggered, 80 and 64 MB/s caps) didn't stall.

**Expected:** every camera keeps delivering frames while acquiring, or a camera that stops is detected and either recovered or reported with its cause.

**Not explained by link oversubscription:** Mono16 at the same cap asks the link for the same 5 × 80.4 MB/s and didn't stall. The report's per-camera "streaming" rates come from frame IDs, assumed to count every frame sent, which is unverified on GenTL.

**Candidate causes:**

1. **The camera or link stopped sending.** 20305113 first enumerated at USB 2 and was replugged before the runs. It ran at 63–80 MB/s (USB 3) in every later run, so a fallback to USB 2 is unlikely but not ruled out.
2. **The host ran out of buffers.** Each GenTL stream announces 4 buffers, and "3 frames, then none" fits losing one per capture. If `EventManagerNewBuffer.buffer` returns the same object after each `update_event_data`, `grab()`'s newest-only loop re-queues the newer buffer and loses the older one.

`genicam` also holds the GIL while waiting for a buffer, in 100 ms slices, so capture threads take turns. That skews per-thread latency in the report and may slow buffer handling.

**Evidence (Phase 2, 2026-11-09; draft for the maintainer's check):** 90 GenTL runs of the stall configuration on the Windows rig (80) and the lab Mac (2), and 10 through Spinnaker, all on the same five cameras. No stall: 100/100 from every camera in every run. 20305113 was opened last in 10 Windows runs and first in 70.

1. **The camera or link stopped sending: not ruled out, and the most likely.**
   - *For:* the five cameras ask for 5 × 80.4 = 402 MB/s, but the rig carries 322–345 MB/s in total, through GenTL and Spinnaker alike. The original stall's four working cameras carried 4 × 80.4 = 322 MB/s, the same ceiling, so the link was oversubscribed when the stall happened. 20305113 had enumerated at USB 2 and been replugged just before the original runs, a state none of the 90 later runs had.
   - *Against:* no later run stalled, and no camera counted a transmit failure or a link recovery during a run. The original run predates those counters, so they can't say what 20305113 did then.
2. **The host ran out of buffers: ruled out.** `EventManagerNewBuffer.buffer` is a fresh wrapper bound to its own buffer (Task 1.1), so `grab()` requeues the buffer it means to. Under FLIR's `NewestOnly`, at most one frame waits, and every run ended with all four buffers queued and no underruns.
3. **FLIR's GenTL producer or `genicam` versus Spinnaker: no difference seen.** Neither stalled, and both carried the same total.

The stall rate is now far below the original 1 in 8: with that rate, 80 GenTL runs without one would happen about once in 40,000 tries. So something about the original session (most likely 20305113's USB 2 enumeration and replug) mattered, and the stall can't be reproduced on demand.

**Affected:** `cameras/gentl.py` (stream and buffer handling), `app/_capture.py` (the report), `docs/hardware/usb3-vision.md`. Possibly the core, if a silent stream should be recovered there.

## Acceptance Criteria

- [ ] `el-devices genicam` reports each camera's link speed, and for GenTL its stream's counters (frames delivered, underruns, buffers queued), where the camera and library have them.
- [ ] Whether `EventManagerNewBuffer.buffer` is a fresh object after each update is verified against real `genicam`, and `GenTLConnection.grab()` is correct for what it does, with a fake-backed test pinning it.
- [ ] The stall is reproduced on the Windows rig with the new report, or the attempts that failed to reproduce it are recorded.
- [ ] The cause is identified from evidence, and the stall is fixed; or, if the camera or link is at fault, the driver recovers or reports it clearly (decided in Phase 3, with the maintainer).
- [ ] Downstream apps have a documented way, and a method if one is needed, to detect and deal with a stalled stream (`docs/guide.md`).
- [ ] `docs/hardware/usb3-vision.md` records the five-camera Windows GenTL runs, the stall and its cause, and any change the evidence supports to the link-sharing guidance.
- [ ] Issue #32 is updated with the findings and closed by the track's PR.

## Dependencies

- Existing code: `cameras/gentl.py`, `cameras/_genicam.py`, `app/_capture.py`, `tests/fakes/gentl.py`.
- Hardware: the Windows rig with five CM3-U3-50S5M and FLIR's GenTL producer (Spinnaker 4.x), and Spinnaker on the same machine for comparison.
- None on other tracks.

## Out of Scope

- How `throughput_limit` rounds a requested cap (nearest step today; rounding down would suit a cap). A separate issue if wanted.
- pgs-scan's per-rig link budget (`USB_LINK_BUDGET_MBPS`).
- Working around `genicam` holding the GIL, unless the evidence makes it the cause.
- A general link-capacity measurement in `el-devices` (the handoff's second question), beyond what the new counters show.
- The handoff's other doc suggestions (trigger timing tables, minimum exposure by format), unless they fall out of this track's runs.

## Technical Notes

- How the counters reach the report is decided in Phase 1. Link speed fits as a read-only setting (`DeviceLinkSpeed`); stream counters live below the backend seam, so they need a way up that doesn't make the core GenTL-specific.
- The stall happened once in eight runs, so reproducing it may take repeated runs. The plan budgets for that and records every attempt.
- Hardware runs are batched at the end of each rig phase (see the plan).

---

_Generated by Conductor. Review and edit as needed._
