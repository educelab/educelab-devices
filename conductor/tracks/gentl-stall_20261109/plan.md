# Implementation Plan: GenTL Stream Stall

**Track ID:** gentl-stall_20261109
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-09
**Status:** [~] In Progress

## Overview

Diagnose before fixing. Phase 1 makes the report show what a stall needs to explain itself, and settles the one library question the fakes can't answer. Phase 2 reproduces the stall on the Windows rig. Phase 3's fix depends on what Phase 2 finds, so its tasks are refined at Phase 2's checkpoint.

Hardware: most runs are batched into Phase 2 and Task 3.3. Task 1.1 is the exception. It's a short probe on the lab Mac (FLIR's macOS GenTL producer, a CM3 attached), needed before Task 1.2 can be written.

## Phase 1: Instruments

### Tasks

- [x] Task 1.1: Find out whether `EventManagerNewBuffer.buffer` is a fresh object after each `update_event_data`, against real `genicam` and FLIR's producer on the lab Mac. A scratch script grabs several buffers and records each one's identity and its token's context. Also record what `NewestOnly` leaves in the output queue. Record the results here and in `usb3-vision.md`.
  - **Result (2026-11-09, CM3 20305122, macOS 27.0.1, Spinnaker 4.4.0.246, genicam 1.6.0, Python 3.13):** `buffer` is a new wrapper on every read, even twice without an update, and never `is` an announced buffer. Each wrapper keeps pointing at its own buffer: four held wrappers kept contexts 2, 3, 0, 1 and frames 34–37 after later updates. Requeueing a wrapper works. So candidate cause 2 (a reused `buffer` losing the older frame) is ruled out.
  - With `NewestOnly`, at most one frame waits (`num_awaiting_delivery` 1, one event queued), so `grab()`'s drain loop never found an older frame in 30 grabs 250 ms apart. All four buffers were back in the input pool after each (`num_queued` 4). Holding all four without requeueing starved the stream (no event in 1 s), so a lost buffer would still stall it.
  - `num_started` tracks the frame ID (frame 7 at 8 started), counting frames the producer received, including those `NewestOnly` discarded; `num_delivered` counts only frames handed to the host. `num_underrun` stayed 0, even when all four buffers were held.
- [x] Task 1.2: Pin `GenTLConnection.grab()`'s buffer handling. A fake-backed test captures many frames with older ones waiting, and checks every announced buffer is back in the input pool afterwards. If 1.1 shows `buffer` is reused, make the fake reuse it too (red), then fix `grab()` (green), e.g. by requeueing the announced buffer that matches the event's context.
  - **Result:** no driver change. 1.1 showed `buffer` isn't reused, and `grab()` doesn't rely on identity. The fake's event now gives a fresh wrapper on every read, as `genicam` does, and `test_many_grabs_with_older_frames_waiting_keep_every_buffer` runs 50 grabs with older frames waiting, with and without `StreamBufferHandlingMode`, and checks all four buffers are queued again. The fake still lets frames pile up under `NewestOnly`, where FLIR's producer keeps one; that only matters if a test needs the producer's own discards.
- [x] Task 1.3: Add a read-only `link_speed` setting (MB/s) from `DeviceLinkSpeed`, left out where a camera lacks it, with a fake test. Show it on the report's camera line.
  - **Probe (2026-11-09, CM3 20305122 and 20305113, FLIR's macOS producer):** the CM3 has no `DeviceLinkSpeed` (nor `DeviceLinkCurrentThroughput`). It has `U3VCurrentSpeed` (enum LowSpeed/FullSpeed/HighSpeed/SuperSpeed; both read SuperSpeed), and the producer's device node map has `DeviceCurrentSpeed` (SuperSpeed). It also has `TransmitFailureCount` (0 on both, with a `TransmitFailureCountReset` command) and `USB3LinkRecoveryCount` (6 and 1; 20305122's went from 5 to 6 between two opens a minute apart), both camera nodes, so the core can read them without GenTL. `DeviceMaxThroughput` is 180.864 MB/s.
  - **Deviation (agreed with the maintainer):** `link_speed` reads `DeviceLinkSpeed` where a camera has it, otherwise the nominal rate of `U3VCurrentSpeed` (SuperSpeed 625, HighSpeed 60, FullSpeed 1.5, LowSpeed 0.1875 MB/s); an unknown entry leaves it out. `TransmitFailureCount` and `USB3LinkRecoveryCount` go with Task 1.4's counters. The report's camera line shows `link 625 MB/s` before the throughput limit.
- [x] Task 1.4: Decide how stream counters get above the backend seam without making the core GenTL-specific (e.g. an optional `Connection` method returning a mapping, surfaced by a `GenICamCamera` method). It's new public API on `GenICamCamera`, so agree it with the maintainer, then implement it for GenTL (frames delivered, underruns, buffers announced, queued and awaiting delivery) with fake tests. Other backends return nothing.
  - **Result (shape agreed with the maintainer):** `Connection.stream_counters()` on every backend (GenTL: `frames_started`, `frames_delivered`, `underruns`, `buffers_announced`, `buffers_queued`, `buffers_awaiting_delivery`; Aravis and Spinnaker: `{}`), checked by the backend contract. Public `GenICamCamera.diagnostics()` merges them with the camera's `transmit_failures` and `link_recoveries`, leaving out what can't be read. Documented in `guide.md`.
- [ ] Task 1.5: Report the counters in `el-devices genicam`: per camera at the end of the run, and when a camera's capture first times out, so a stall shows its state at the time. Show the frames the host received next to the frame-ID-based streaming rate, so the rate's assumption can be checked. Tests in `test_cli_genicam.py`.

### Verification

- [ ] Full suite, `ruff check .` and `ruff format --check .` pass.
- [ ] Maintainer check before Phase 2.

## Phase 2: Reproduce on the Windows rig

Each run is `el-devices genicam --backend gentl --pixel-format Mono8 --exposure min --throughput-limit 80 --frames 100` on the five CM3s, unless a task says otherwise. Log every run to the PNY drive and record each one here, stalled or not.

### Tasks

- [ ] Task 2.1: Run the stall configuration up to 10 times, or until it stalls. On a stall, record the counters, the link speed, and whether the camera still answers node reads.
- [ ] Task 2.2: On a stalled camera, stop and restart acquisition (a scratch script, or a `pixel_format` write, which pauses acquisition). Record whether frames come back without a power cycle.
- [ ] Task 2.3: Repeat 2.1 with 20305113 opened first (`--serial` in that order; check the CLI opens cameras in the order given). Does the stall follow the camera or the last position?
- [ ] Task 2.4: Run the same configuration through Spinnaker (`el-devices spinnaker`) on the same machine, as many times as 2.1 took. Separates FLIR's GenTL producer and `genicam` from the camera and link.
- [ ] Task 2.5: Classify the cause from 2.1–2.4: the camera or link, the host's buffers, or something else. Record the evidence for and against each candidate in the spec.

### Verification

- [ ] Every run is recorded here.
- [ ] Maintainer agrees on the cause; Phase 3's tasks are refined to match.

## Phase 3: Fix and record

### Tasks

- [ ] Task 3.1: Choose the fix with the maintainer. Likely shapes:
  - Lost buffers: done by 1.2, if that was the cause.
  - The camera or link stops: detect a stream silent for several frame periods while acquiring, restart acquisition once and log it, or raise an error that names the cause.
  - A USB 2 link: warn or refuse at open when `link_speed` is below USB 3.
- [ ] Task 3.2: Implement it test-first on the fakes.
- [ ] Task 3.3: Rerun Phase 2's stall configuration on the Windows rig as many times as it took to reproduce. No stall, or the stall is recovered or reported as designed.
- [ ] Task 3.4: Update `docs/hardware/usb3-vision.md`: the five-camera Windows GenTL runs (Hardware runs), the stall and its cause (Hazards), what 1.1 found about `genicam`'s buffers, and the link-sharing guidance where the evidence changes it. Update `docs/guide.md` if callers can see the new counters.
- [ ] Task 3.5: Give downstream apps (e.g. pgs-scan) a documented way to deal with a stalled stream: how a stall shows up to a caller (which error, after how long), what the driver already does about it, and what the app should do (e.g. a method to restart the stream, or close and reopen), with an example in `docs/guide.md`. Shaped by 3.1's choice. (Maintainer's note, 2026-11-09.)
- [ ] Task 3.6: Post the findings on issue #32 and close it from the track's PR.

### Verification

- [ ] Full suite, `ruff check .` and `ruff format --check .` pass.
- [ ] Maintainer check.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
