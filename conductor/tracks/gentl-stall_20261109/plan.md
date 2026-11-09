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
- [x] Task 1.5: Report the counters in `el-devices genicam`: per camera at the end of the run, and when a camera's capture first times out, so a stall shows its state at the time. Show the frames the host received next to the frame-ID-based streaming rate, so the rate's assumption can be checked. Tests in `test_cli_genicam.py`.
  - **Result:** the report reads `diagnostics()` before the first capture and after the last, prints the counters at the end of each camera's report, and through GenTL a `host received ~N frames/s (frames_started)` line under the frame-ID streaming rate. A camera's first failed capture (any error, not only a timeout) prints its counters at once.

### Verification

- [x] Full suite, `ruff check .` and `ruff format --check .` pass.
- [x] Maintainer check before Phase 2 (approved 2026-11-09, after a local run).
- **Local run (2026-11-09, lab Mac, five CM3s, the stall configuration, twice):** no stall; the report's new lines work. The received rate matched the frame-ID rate on every camera. `link_recoveries` rises by one per open, never during a run; the report's new "during the run" line shows each counter's change over the run (buffer counts left out). Logs in the session scratchpad (`report_mac_run1.log`, `report_mac_run2.log`).

## Phase 2: Reproduce on the Windows rig

Each run is `el-devices genicam --backend gentl --pixel-format Mono8 --exposure min --throughput-limit 80 --frames 100` on the five CM3s, unless a task says otherwise. Log every run to the PNY drive and record each one here, stalled or not. [`stall-runs.sh`](./stall-runs.sh) (Git Bash or MSYS2) repeats a configuration, logs each run to its own file with a summary, and stops at the first stall (see its examples for 2.1, 2.3 and 2.4).

### Tasks

- [x] Task 2.1: Run the stall configuration up to 10 times, or until it stalls. On a stall, record the counters, the link speed, and whether the camera still answers node reads.
  - **Runs (2026-11-09, Windows rig `DESKTOP-Q940IU7`, educelab-devices at this branch, Python 3.12.10; logs `PNY:/gentl-stall/20261109-164944-gentl-run01..10.log`):** 10 of 10 ok, no stall. Every camera captured 100/100 in every run (1,000 each); no failures, `underruns`, `transmit_failures` or `link_recoveries`, which stayed 0 at every open too (unlike the Mac, where it rose at each open). Link 625 MB/s on all five. All-camera capture median 124–187 ms.
  - The producer listed and opened the cameras in the same order every run: 20305114, 20305122, 20298479, 20298452, 20305113. So 20305113 was last each time, as in the original stall, and didn't stall.
  - The received rate (`frames_started`) matched the frame-ID rate on every camera in every run (within 0.1 frames/s), so the frame-ID rate holds on Windows too.
  - **The link is oversubscribed in this configuration.** The five cameras ask for 5 × 80.4 = 402 MB/s but streamed 286–344 MB/s together (median about 331). 20298479 and 20305113 held the cap (78–80 MB/s) in most runs; 20305122 (43–73) and 20298452 (40–55) got less. The original stall's four working cameras streamed 4 × 80.4 = 322 MB/s, about the same total, so the stalled camera got none of the shared link rather than a share. This weakens the spec's argument against oversubscription: Mono16 at the same cap was oversubscribed too, so its not stalling shows only that oversubscription doesn't always stall. Evidence for 2.5.
  - **More runs (logs `PNY:/gentl-stall-re/`):** 10 more (`20261109-165759`) and 50 more (`20261109-173924`) in default order, all ok. From these on, the producer listed 20305113 first (order 20305113, 20298452, 20298479, 20305122, 20305114), so it was opened first in all 60. Totals 322–345 MB/s; the squeezed cameras moved to 20305113 (41–69 MB/s) and 20305114 (42–67). 6,000 captures per camera, no failures, every error counter 0, received rate within 0.3 frames/s of the frame-ID rate throughout.
- [ ] Task 2.2: On a stalled camera, stop and restart acquisition (a scratch script, or a `pixel_format` write, which pauses acquisition). Record whether frames come back without a power cycle.
  - Waits for a stall: none in 2.1. Not done: none of the 90 Phase 2 runs stalled. Recovery by restarting acquisition is untested on hardware.
- [x] Task 2.3: Repeat 2.1 with 20305113 opened first (`--serial` in that order; check the CLI opens cameras in the order given). Does the stall follow the camera or the last position?
  - **Runs (`PNY:/gentl-stall-re/20261109-172901-*`):** 10 of 10 ok with `--serial 20305113 20305122 20305114 20298452 20298479`, opened in that order. Totals 322–336 MB/s. With the 60 default-order runs above, 20305113 was opened first in 70 runs and last in 10, and never stalled, so the stall followed neither the camera nor the last position.
  - Checked in code: `--serial` opens cameras in the order given (`app/genicam.py`); without it they open in the producer's listing order, which differed between two runs on the Mac. So to put 20305113 first, give every serial: `--serial 20305113 --serial ...`.
- [x] Task 2.4: Run the same configuration through Spinnaker (`el-devices spinnaker`) on the same machine, as many times as 2.1 took. Separates FLIR's GenTL producer and `genicam` from the camera and link.
  - **Runs (`PNY:/gentl-stall-re/20261109-173502-spinnaker-*`):** 10 of 10 ok through Spinnaker. Totals 331–341 MB/s, the same ceiling as GenTL; no transmit failures or link recoveries. No difference between FLIR's GenTL producer and Spinnaker.
- [x] Task 2.5: Classify the cause from 2.1–2.4: the camera or link, the host's buffers, or something else. Record the evidence for and against each candidate in the spec.
  - **Draft in the spec (Evidence):** camera or link most likely, not proven; host buffers ruled out; producer vs Spinnaker no difference. Not reproducible on demand: 0 stalls in 80 GenTL runs, against the original 1 in 8. Agreed with the maintainer (2026-11-09).

### Verification

- [x] Every run is recorded here.
- [x] Maintainer agrees on the cause; Phase 3's tasks are refined to match (2026-11-09).

## Phase 3: Fix and record

### Tasks

- [x] Task 3.1: Agree the API with the maintainer. Chosen shape (2026-11-09): the driver detects a stall and reports it, and gives apps a tested way to restart the stream; it doesn't retry on its own, since no restart has been seen to clear a real stall. To agree:
  - A `StreamStalled(DeviceTimeout)` error in `errors.py` (shared, so it needs approval), raised when a free-running camera's capture times out while acquiring. Its message says the camera stopped sending and includes `diagnostics()`. Triggered capture keeps raising `DeviceTimeout`, since a missing trigger looks the same.
  - A public `GenICamCamera.restart_acquisition()` that stops and starts acquisition and logs the counters before and after.
  - A warning at open when `link_speed` is below USB 3 (SuperSpeed).
- [x] Task 3.2: Implement 3.1 test-first on the fakes: a fake stream that stops delivering, the error and its counters, a restart bringing frames back, and the USB 2 warning.
  - **Result:** a free-running capture that gets no frame at all raises `StreamStalled`; one that saw stale or incomplete frames, or a software-triggered one, still raises `DeviceTimeout`. With newest-only buffering a waiting frame is always taken at once, so no frame in a capture means nothing came since the previous one. The fake camera has a `sending` switch, cleared by the next start. The report's first-failure line no longer adds counters to a `StreamStalled`, whose message has them.
- [x] Task 3.3: On the Windows rig: rerun Phase 2's stall configuration 10 times with the new code; then force a stall by executing `AcquisitionStop` behind the driver's back ([`force-stall.py`](./force-stall.py), once per backend), and check that capture raises `StreamStalled` with the counters and that `restart_acquisition()` brings frames back. Also run the configuration 10 times at `--throughput-limit 64`, to check that every camera then gets its cap. Record the results in the guide's stalled-stream section and `usb3-vision.md`.
  - **Result (2026-11-09, logs `PNY:/gentl-stall-p3/`, `PNY:/hw-logs/force-stall-*.txt`):** `force-stall.py` on `20305113`: through GenTL and Spinnaker alike, one late frame, then `StreamStalled` with the counters after the 2 s timeout; `restart_acquisition()` brought back 10 of 10 frames, with frame IDs and GenTL's stream counters reset to 0. The first version of the script expected no frame after the stop and failed on the frame in flight; fixed. 10 runs at 80 MB/s on this code: all ok, 321.7–326.4 MB/s together, shares uneven (43–80 MB/s). 10 at 64 MB/s (kept as 65.312): all ok, every camera 65.3 MB/s, all-camera median 154 ms against 125–139 at 80.
  - **Mono16, to settle the cap (logs `PNY:/gentl-stall-mono16/`):** 10 runs each, all ok. 80 MB/s: 321.6 MB/s together, two cameras at half (40.2), all-camera median 250 ms. 64 MB/s (kept as 60.288): 301.5 MB/s, every camera 60.3, median 333 ms. Agreed with the maintainer to keep recommending 80 MB/s; 64 is for even rates.
- [x] Task 3.4: Update `docs/hardware/usb3-vision.md`: the five-camera Windows GenTL runs (Hardware runs), the stall and what's known of its cause (Hazards), what 1.1 found about `genicam`'s buffers, and the link-sharing guidance: this rig carries about 330 MB/s in total, so five cameras need a cap of about 64 MB/s, not 80. Flag the cap for pgs-scan's link budget (outside this track).
- [x] Task 3.5: Give downstream apps (e.g. pgs-scan) a documented way to deal with a stalled stream in `docs/guide.md`: how a stall shows up (`StreamStalled`, after the capture timeout), what the driver does (detects and reports, no retry), and what the app should try, in order, with an example: `restart_acquisition()`, then close and reopen, then power-cycle the camera's port through the hub, then fail the scan. (Maintainer's note, 2026-11-09.)
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
