# IDS U3-36P0XCP-M-GL (uEye XCP, USB3 Vision)

The lab's first USB3 Vision camera that isn't a FLIR. It is driven by the generic GenICam drivers: `AravisCamera` (`educelab.devices.cameras.aravis`) and `GenTLCamera` (`educelab.devices.cameras.gentl`). How those drivers work, how to install their libraries, and what they add on top of Aravis and GenTL is in [usb3-vision.md](usb3-vision.md). This doc covers the camera.

Everything here was observed on one unit (serial `4110091493`, firmware `3.90.27889`) on the lab Mac (macOS 27, arm64) through Aravis 0.8.36, on 2026-10-22, unless marked otherwise. See the [README](README.md#how-sure-we-are) for what **verified**, **assumed** and **unknown** mean.

## Identity

- **Model.** IDS sells it as the U3-36P0XCP-M-GL (monochrome, glass lid). The camera reports its model as `U3-36PxXCP-M` and its vendor as `IDS Imaging Development Systems GmbH`. Verified.
- **Firmware.** `3.90.27889` on this unit, read from `DeviceFirmwareVersion` (the driver's `info.firmware_version`). IDS lists uEye+ firmware 3.94 with IDS peak 26.06; no changelog was found, and the lab has chosen not to update. Updating needs IDS peak's tools, which run on Linux and Windows only. Verified (version on the unit); assumed (3.94, from IDS's manual pages).
- **On the host.** USB vendor ID `1409`. Aravis's device ID is `IDS Imaging Development Systems GmbH-1409f4fb04e5-4110091493`, and both Aravis and the driver report serial number `4110091493`, the same serial the camera's `DeviceSerialNumber` gives. Verified.
- **Sensor.** 5112 × 3840 pixels, rolling shutter (`SensorShutterMode = Rolling`, the only entry), `SensorOperationMode = Default`. The sensor model is readable from the `SensorInformation` node; not yet recorded. Verified (size and shutter).

## Transport

- **Aravis** works on macOS. On Linux it got only incomplete frames ([Aravis on Linux](#aravis-on-linux)). IDS's GenTL producer, part of IDS peak, has no macOS build, so on the lab Mac Aravis is the only option. Verified (no macOS build, per IDS's readmes).
- **GenTL** through IDS peak's `ids_u3vgentl.cti` works on Linux, untimed (see [GenTL on Linux](#gentl-on-linux)), and is untried on Windows. IDS's producer opens only IDS cameras, and on Windows IDS's kernel driver claims the camera, so Aravis can't open it there. See [usb3-vision.md](usb3-vision.md#backends).
- **No timestamp through GenTL.** genicam's buffers carry no host arrival time, and this camera has no timestamp latch, so `GenTLCamera` can't relate frames to host time and refuses `not_before` on it. Verified on Linux: every `not_before` capture was refused.
- **USB 3 only.** IDS's readme says uEye XCP models don't work on USB 2.0. Assumed (IDS readme).
- **Linux.** Raise usbfs memory (`usbfs_memory_mb=1000`): one Mono8 frame is 19.6 MB, more than the kernel's 16 MB default. See [usb3-vision.md](usb3-vision.md#usbfs-memory). Assumed.

### GenTL on Linux

Fedora on an Apple Silicon Mac (Asahi Linux, arm64), IDS peak 26.06.2's `ids_u3vgentl.cti` (archive install), Python 3.14.8, genicam 1.6.0, 2026-10-22. [V for each point below, on this unit]

- **Rerun with the hardware tests that allow for it: 17 passed, 7 skipped** (the timing checks, plus no `ExposureAuto`). 300 captures without `not_before` ran at 7.0 per second with no incomplete frames.
- **Over USB 3 (5000M in `lsusb -t`): it works, untimed.** 12 of 23 hardware tests passed, 1 skipped. Every failure was the driver refusing `not_before`, because through GenTL this camera's frames can't be timed (see [Transport](#transport)). Captures without `not_before`, settings, pixel formats, the throughput limit, reopening and busy detection all worked. The hardware tests now skip timing checks for a camera that can't be timed, and check the refusal instead.
- **Over USB 2 it fails badly.** The first runs went through a USB 2 A-to-C adapter: Aravis reported `USB 2.10 (Low bandwidth)`, and its stream completed none of 218 frames. Through GenTL, only 6 of 23 tests passed. The control channel fell out of step (`ListenFailed with 'Input/Output Error'`, `ListenUnexpected command id 0x4356`, `unexpected request id 100<>101`), grabs timed out, and finally the camera disappeared. IDS says uEye XCP cameras don't work on USB 2.0. Check the link with `lsusb -t`: 480M is USB 2, 5000M USB 3.
- **Discovery** reports model `U3-36PxXCP-M`, serial `4110091493` and address `1409f4fb04e5 U3-36PxXCP-M`. genapi refuses an out-of-range exposure ("must be greater than or equal 31.712621").

### Aravis on Linux

The same machine and link, Aravis 0.8 built from source. With the driver in sync USB mode, as it then was everywhere, only discovery passed: opening timed out, because every frame arrived incomplete while the driver waited for one complete frame to time the camera's arrivals. `arv-camera-test-0.8` then showed the cause: in sync mode it reported six or more errors, while in async mode the camera streamed cleanly. The driver now uses async mode off macOS. [V, 2026-10-22]

Rerun in async mode: 22 of 23 hardware tests passed, 2 skipped (no `ExposureAuto`; the refusal test, since Aravis times this camera). [V for each point below]

- The arrival-timed offset spread over 5 measurements was 0.99 ms.
- The 20 triggered captures needed no second trigger, and exposures began 1.6–1.7 ms after the request (9.9 ms for the first, with the settle wait).
- 300 `not_before` captures met 300 stale frames, at 2.5 captures per second, as on the Mac. After a 1 s pause the frame taken was exposed 278 ms before the call.
- Both pixel formats captured, and the camera clamped an out-of-range exposure write to 31.7 µs, as on the Mac.

## Nodes

What the driver reaches, and what's missing. Verified with `arv-tool-0.8 -n <id> features` and the hardware tests.

| Setting | Node | On this camera |
|---|---|---|
| `exposure_time` | `ExposureTime` (µs) | Present. An out-of-range write is accepted and clamped by the camera (asked for less than the minimum, it read 31.7 µs). |
| `ExposureAuto` | none | **Missing.** The driver leaves the setting out. |
| `gain` | `Gain` (dB) | 1.0 to 8.04 dB. No `GainAuto`. |
| `pixel_format` | `PixelFormat` | Mono8 and `Mono10g40IDS` only; other entries are listed as "Not available". See [Pixel formats](#pixel-formats). |
| `throughput_limit` | `DeviceLinkThroughputLimit` | 108 to 300 MB/s in 2 MB/s steps; 162 MB/s as found. |
| `trigger` | `TriggerSelector`, `TriggerMode`, `TriggerSource`, `TriggerSoftware` | `FrameStart` (used) and `ExposureStart`; sources `Software` and `Line0`. |
| timestamps | `Timestamp`, `TimestampLatch` | **Missing.** See [Timestamps](#timestamps). |

Other nodes worth knowing: `TLParamsLocked` (see [Hazards](#hazards)), `TransferControlMode` (read-only, `Automatic`), `DeviceClockFrequency` (171,666,666 Hz, `DeviceClockSelector = Sensor`), and `TestPattern` (`Off`, `ColorBar`, `White`). Event timestamps exist only for errors, dropped events, a test event and the UART, not for exposure or frame start. There is no chunk data.

## Pixel formats

- **Mono8.** 5112 × 3840; the payload is 19,630,080 bytes. Verified.
- **Mono10g40IDS.** IDS's own 10-bit packed format, code `0x4000000F` (outside the PFNC; IDS calls it preliminary). The payload is 24,537,600 bytes, 1.25 times Mono8's. Verified.
  - Each group of 5 bytes holds 4 pixels. Bytes 0–3 are their upper 8 bits. Byte 4 holds their low 2 bits, pixel 0 in bits 0–1, pixel 1 in bits 2–3, and so on.
  - Checked two ways. On the `ColorBar` test pattern, the decoded values shifted down 2 bits equal the Mono8 frame on every pixel; the pattern has only levels 0 and 255, so this checks the upper bits only. For the low bits, a live scene was decoded with all 24 orders of the 2-bit pairs; this order gave the smoothest image (mean row difference 4.59, against 4.77 for the next best and 5.09 for the worst).
  - The driver unpacks it to `uint16` with values 0–1023, and frames carry `metadata["bit_depth"] = 10`. The top 6 bits of each value are zero.
- **Writes only without a stream.** The camera refuses `PixelFormat` writes ("USB3Vision write_memory error", write-protect) while an Aravis stream exists, even with acquisition stopped, and the same goes for `TriggerMode`. Aravis enables the stream channel when it creates a stream and disables it only when the stream thread stops or the stream is finalized. The Aravis driver therefore has a stream only while acquiring, and settings that need the camera stopped go through `_acquisition_paused()`. Verified.

## Timestamps

The camera has **no timestamp latch**, so the driver can't read the device clock on demand. Instead it times frames by their arrival (`timestamp_source = "arrival"`): the offset between host and device time is the smallest gap between a frame's arrival on the host (Aravis's system timestamp, taken when the frame's leader packet arrives) and its device timestamp, over the last 20 frames (`ARRIVAL_SAMPLES`). Verified:

- Offset spread over 5 remeasurements was 1.8 ms in one run and 0.46 ms in another (2–30 µs with a latch on the CM3-U3-50S5M).
- Arrival minus stamp moved between +0.5 and −1.1 ms over 10 frames as the window settled, at 5 ms exposure.
- So frame times relative to the device clock are good to about ±2 ms.

**The leader comes before the exposure (in trigger mode).** The camera sends a frame's leader, carrying its device timestamp, about 1 ms after the previous frame's trailer, as soon as it is ready for the next frame, and before the trigger that exposes it. The pixel data follows when a trigger is accepted. So a triggered frame's stamp and arrival mark when the camera became ready, not when it exposed. Verified with Aravis's packet log (`Aravis.debug_enable("sp:3")`).

- The driver dates such frames by the **first software trigger fired after the leader arrived** (`metadata["timestamp_source"] = "trigger"`): that trigger is the one that exposed the frame. The frame's timestamp is then the host time just before the trigger, which is when the exposure begins, give or take the trigger latency.
- If the camera ignores that trigger and the driver fires again, the frame is dated by the ignored trigger: early by the retry interval (exposure plus three turnarounds, about 0.4 s), but still after `not_before`.
- Before the driver did this, arrival timing dated every triggered frame before its trigger: each capture dropped its first frame as stale and fired again (about 660 ms a capture), and the frame kept was dated about 270 ms before its exposure.
- `TriggerSelector = ExposureStart` is no way out: with it, the camera delivered frames without being triggered.

**Free-running, the stamp also comes early, so timestamps are early.** Verified, 2026-10-22, Mono8 at the 162 MB/s limit:

- A free-running frame finishes arriving one frame period after its leader, at every exposure tried: 199.8 ms after it at 5 and 100 ms exposure (period 200 ms), 300.9 ms at 300 ms exposure (period 301 ms).
- Triggered, a frame finishes 124.6 ms after its exposure ends, at 5, 100 and 300 ms exposure alike: that is readout plus transfer.
- So a free-running frame's exposure starts about (exposure + 124.6 ms) before it finishes arriving, while the driver dates it at the leader minus the exposure. The timestamp is early by the frame period minus about 125 ms: 75 ms at 5 ms exposure, 176 ms at 300 ms. That assumes readout and transfer take as long free-running as triggered.
- A frame can't be sent faster than the link allows (about 121 ms per Mono8 frame at 162 MB/s), so the period never falls much below 125 ms, and a timestamp is at most a few milliseconds late. Free-running frames therefore only look older than they are: `not_before` never takes a stale frame, but drops more fresh ones, and timestamps are off by tens of milliseconds.
- For accurate timestamps on this camera, use software trigger. Where in the rolling-shutter exposure (first row or last) the trigger falls is still **unknown**; a light switched at a known host time would settle it. The hardware test `test_device_timestamp_against_buffer_arrival` prints the arrival-minus-stamp gaps.

## Software trigger

- **Turnaround.** A software trigger's frame arrives about 130 ms after the trigger, for a 19.6 MB Mono8 frame at 162 MB/s: 5 ms of exposure, then the transfer. Switching to software trigger took 0.22–0.38 s. Verified.
- **Busy just after a frame.** The camera ignores a trigger fired within about 3 ms of a frame arriving: all of 12 tries at 0–2.5 ms were ignored, and of 36 tries at 3–50 ms, only one (at 4.5 ms). The driver waits until 10 ms after the last frame before firing (`_TRIGGER_SETTLE_S`). Before that, 9 of 20 back-to-back captures needed a second trigger; after it, none of 20 did. Verified.
- **Capture rate.** 20 triggered captures through `el-devices genicam` took a median 132 ms each, one trigger fired again in 20, no stale frames. Verified.

## Timing

- **Free-running.** At the 162 MB/s throughput limit, Mono10g40IDS streamed 6.7 frames/s, the limit divided by the frame size. Mono8 should then manage about 8 frames/s (assumed, by the same arithmetic). 300 `not_before` Mono8 captures met 300 stale frames, at 3.2 captures per second. Verified unless marked.
- **Newest only.** After a 1 s pause, the frame taken was exposed 13 ms before the call in one run and 278 ms in another, so the stream keeps only the newest frame. Verified.
- **Opening.** Without a latch, open times one complete frame to get a first offset; later frames refine it.

## State and recovery

- **A wedged camera needs a power cycle.** After `TLParamsLocked = 1`, or a session closed in Aravis's async USB mode, the camera streams nothing (or only `missing-packets` frames) until it is power-cycled. Clearing the flag doesn't help. On the lab rig it is on Acroname port 6: `el-devices hub cycle 6`. Verified.
- **Busy detection works.** A second process's open fails with `LIBUSB_ERROR_ACCESS`, which the driver reports as `DeviceBusy`. Verified.
- **Reopening after a failed session** failed with `LIBUSB_ERROR_ACCESS` in the first session. The cause was a buffer left in a failed test's traceback, which kept the stream and the USB interface (found on the CM3-U3-50S5M; fixed in the driver). Verified.

## Hazards

- **Never set `TLParamsLocked`.** With it set to 1, the camera streams nothing through Aravis, and stays that way after the flag is cleared, until power-cycled. The driver leaves it alone, as Aravis's own tools do. Verified.
- **Aravis's USB mode matters on both OSes.** In async mode on macOS, closing the camera can hang forever (in `arv_uv_stream_finalize`), and the next session then gets only `missing-packets` frames until a power cycle. In sync mode on Linux, frames arrive incomplete. The driver uses sync on macOS and async elsewhere. Verified; see [usb3-vision.md](usb3-vision.md#hazards).
- **Bandwidth.** Streaming this camera with five uncapped CM3-U3-50S5M cameras through one hub gave it only `size-mismatch` frames: about 5 × 181 MB/s plus 162 MB/s on one USB 3 upstream link. Cap the FLIR cameras' throughput when they share a link. Verified.
- **Occasional `size-mismatch` frames** (2 of 22 and 4 of 25 frames, in two runs) with only this camera streaming. The cause is **unknown**. The driver drops them as incomplete and takes the next frame.
- **Exposure writes clamp silently.** An out-of-range `ExposureTime` write is accepted and clamped by the camera. The driver checks ranges itself, so callers get `ValueError`. Verified.

## Manual check

```bash
el-devices genicam --backend aravis --frames 0                 # list cameras
el-devices genicam --backend aravis --serial 4110091493 --frames 20
el-devices genicam --backend aravis --serial 4110091493 --frames 20 \
    --trigger software --exposure 0.005
el-devices genicam --backend aravis --serial 4110091493 --frames 10 \
    --pixel-format Mono10g40IDS                                # 10-bit pixels
ARAVIS_SERIAL=4110091493 pytest -m hardware tests/test_aravis_hardware.py -s
```

### Rig check steps

1. Plug the camera into a USB 3 port (on the rig, Acroname port 6). If it was left wedged, power-cycle it first: `el-devices hub cycle 6`.
2. `el-devices genicam --backend aravis --frames 0` lists it as `4110091493  U3-36PxXCP-M`.
3. Free-running, 20 frames: every frame captured, with `timestamp offset ... from frame arrivals`.
4. Software trigger, 20 frames: every frame captured and dated by its trigger, median capture about 130 ms, at most one trigger fired again.
5. Mono10g40IDS, 10 frames: `10-bit pixels`, about 6.7 frames/s.
6. The hardware tests pass: 20 passed, 1 skipped, with the multi-camera tests deselected or skipped when it is the only camera of its model.

### Last run

2026-10-22, Fedora on Asahi Linux (arm64), Python 3.14.8, firmware `3.90.27889`, over USB 3:

- `tests/test_aravis_hardware.py` (Aravis 0.8 from source, async USB mode): 22 passed, 2 skipped.
- `tests/test_gentl_hardware.py` (IDS peak 26.06.2's `ids_u3vgentl.cti`): 17 passed, 7 skipped, the timing checks among them, since GenTL can't time this camera's frames.

2026-10-22, macOS 27 (arm64), Python 3.12, Aravis 0.8.36, firmware `3.90.27889`:

- `tests/test_aravis_hardware.py`: 20 passed, 1 skipped, 2 deselected (multi-camera), with all 20 triggered captures needing no second trigger.
- `el-devices genicam`: free-running Mono10g40IDS 10/10 at 6.7 frames/s; software trigger 20/20, median 132 ms, one trigger fired again, two `size-mismatch` frames dropped.
- History: the first session passed 8 of 23 hardware tests. The fixes that followed (stream only while acquiring, `TLParamsLocked` left alone, sync USB mode, arrival timing, Mono10g40IDS, dating triggered frames by their trigger, the trigger settle time) are recorded above and in [usb3-vision.md](usb3-vision.md#hardware-runs).
