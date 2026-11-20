# Implementation Plan: Color GenICam Cameras

**Track ID:** color-cameras_20261119
**Spec:** [spec.md](./spec.md)
**Created:** 2026-11-19
**Status:** [~] In Progress

**Phase checkpoints:** stop for maintainer verification after each phase (workflow default). On 2026-11-19 the maintainer asked to "work as far as you can without me or the rig", so phases run back to back in that session; approval gates (Task 3.1, `PreviewFrame`) still stop. Hardware runs are batched into Phase 6.

**Probe 2 (2026-11-19, raw PySpin, camera attached, room light):**
- PySpin's `GetNDArray` refuses `Mono12Packed`, `Mono12p`, and both YCbCr 4:2:x formats ("not supported", -1003). `GetData` gives the raw bytes, so the Spinnaker backend shapes those through `shaped_pixels`.
- `GetPixelFormatName` gives the legacy names `YUV411_8`, `YUV422Packed`, `YUV444Packed` for the YCbCr formats. `GetTLPixelFormat` gives the code the camera sent, which is the PFNC `YUV411_8_UYYVYY` (0x020C001E), `YUV422_8_UYVY` (0x0210001F) and `YUV8_UYV` (0x02180020), not the `YCbCr*` codes (0x020C003C, 0x02100043, 0x0218003A; harvesters `_pfnc.py`). Same byte layouts, so both map to the SFNC names.
- Unpacking checked against `Mono16 >> 4` of the same scene: `Mono12Packed` as GigE legacy (high bytes 0 and 2, low nibbles in byte 1, pixel 0's low) correlates 0.985, the PFNC layout 0.54; `Mono12p` as PFNC LSB-first 0.986, legacy 0.56. `Mono16`'s low 4 bits were always 0.
- `YCbCr8_CbYCr` channel 1 tracks luma (mean 85 vs RGB luma 95, auto exposure), so the order is Cb, Y, Cr; `YCbCr422_8_CbYCrY` byte 1 of each pair is Y. `GetNDArray` gives `(H, W, 3)` for `YCbCr8_CbYCr`, equal to its raw bytes.
- `BalanceRatioSelector` lists `Red`, `Green`, `Blue` (Green presumably unavailable; probe 1 saw only Red and Blue). `BalanceRatio` has no increment and no unit. `SharpnessEnabled` is writable in Mono8, Mono12p, Mono16, YCbCr422 and RGB8, unavailable in Bayer. `PixelColorFilter` reads `BayerRG` in every format.

## Overview

Fakes first: every backend's fake gains a CM3-U3-31S4C with the formats, nodes, and defaults the 2026-11-19 probe found, so the rest can be built test-first. Then pixel formats (packed 12-bit, YCbCr, saturation), the Bayer pattern, and white balance, all in the shared GenICam core so every backend gets them at once. Docs follow, and the hardware runs are batched into the final phase.

**Probe 3 (2026-11-19, static scene, Spinnaker): how close the camera's processed formats are to a host demosaic.** Exposure 17.9 ms, gain 18.3 dB, ratios red 1.07 and blue 3.83 (auto's choices, then fixed), 8 frames averaged per format. Camera values fitted per channel as `a × host + b`:
- RGB8 against OpenCV's bilinear `COLOR_BayerBG2RGB` of the BayerRG8 frame: a = 1.001, b ≈ 0, r = 0.9997–0.9998, mean residual 1–2 counts (about the noise between the averaged sets). OpenCV names Bayer patterns by the second row, so the CM3's `BayerRG` is OpenCV's `BayerBG`; the other three codes fit worse (r 0.993–0.996, residual 4–5).
- RGB8 keeps the black-level pedestal (minimum 22) and applies the same white balance as the raw frame; no color matrix shows (Saturation 100 is identity here; Gamma and Hue off).
- Sharpening on and off differ by about the noise (r 0.9997, residual 1–2) in this dim, high-gain scene; not settled.
- YCbCr8_CbYCr and YCbCr422_8_CbYCrY are BT.601 limited-range (Y 16–235) encodings of the same RGB: a = 1.00–1.003, b within ±0.7. Full-range conversion is off by ×1.165. OpenCV's `COLOR_YUV2RGB_UYVY` converts YCbCr422 directly.
- That match holds only with the camera's processing at identity, as this camera was: SaturationEnabled on at 100 (or off), GammaEnabled off (or on at Gamma 1.0), HueEnabled off (or Hue 0), LUTEnable off, defect correction off. There is no color matrix node. Taking each away from identity in turn (probe 4, same scene and fixed settings), RGB8 against the host demosaic:
  - Saturation 200 doubles chroma (camera/host 1.94); Saturation 0 gives grey (0.004).
  - Gamma 0.5: mean difference 39.8 counts, offset −38. Gamma is writable only while GammaEnabled is on.
  - Hue 30: chroma rotates (mean difference 2.1 against 1.5 at identity). Hue is writable only while HueEnabled is on.
  - Sharpness manual 4095 doubles the per-channel residual (2.6 against 1.2); manual 0 and SharpnessEnabled off are within noise of auto (which read 880–1109), so auto sharpening is mild.
  - BayerRG8 moved by no more than the noise (1.75–1.79 counts) under every setting: processing never reaches Bayer output.
  - Quirk: SaturationEnabled off and back on left Saturation at 85.94 (raw 880 of 1024), not the 100 it had. Restored by writing Saturation.
  - The LUT (`LUTSelector` Luminance, Red, Green, Blue; 9-bit in and out) wasn't tried: it needs table writes.
  - Everything restored after: SharpnessAuto Continuous, Saturation 100, gamma and hue off.
- So a camera left by another program (e.g. SpinView) with saturation, gamma, hue or a LUT set gives processed colors in RGB8 and YCbCr that no longer match a demosaic of its raw frames, and the driver neither shows nor resets them (follow-up issue on processing controls).
- Decision (maintainer): previews stay in the capture's pixel format and `PreviewFrame` doesn't change; the guide recommends that apps wanting color previews select RGB8 or YCbCr422 for previewing. Task 3.1 is dropped; `color_filter` stays in frame metadata only.

## Phase 1: Color CM3 Fakes

### Tasks

- [x] Task 1.1: Add a CM3-U3-31S4C to the shared GenICam fake (`tests/fakes/genicam.py`). It opens in `BayerRG8` and offers the ten formats the camera does. It has `PixelColorFilter=BayerRG`, `BalanceWhiteAuto` (`Off`/`Once`/`Continuous`, starting at `Continuous`), and `BalanceRatioSelector` (`Red`/`Blue`) with `BalanceRatio` (0.25–4, writable only while auto is `Off`). The balance nodes are unavailable in Mono formats, and `SharpnessEnabled` is available only in processed formats. It produces payloads of the right size for each format.
- [x] Task 1.2: Expose it through the PySpin, Aravis, and GenTL fakes, with the new formats' PFNC codes in the Aravis and GenTL fakes.
- [x] Task 1.3: Tests of the fake itself (`tests/test_fake_genicam.py`, `tests/test_fake_pyspin.py`, `tests/test_fake_aravis.py`): selector-indexed ratios, access that follows auto mode, and availability that follows the format.

### Verification

- [x] Full suite: `pytest`, `ruff check .`, `ruff format --check .`. Two real-PySpin name checks (`test_fake_module_names_exist_in_pyspin`, `test_fake_node_names_exist_in_pyspin`) fail on the rig Mac, as they did before this track: PySpin's QuickSpin has no `AcquisitionFrameRateAuto_*`, `TransmitFailureCount`, `USB3LinkRecoveryCount` or `SharpnessEnabled` attributes. They skip in CI.

## Phase 2: Pixel Formats

### Tasks

- [x] Task 2.1: Tests, then unpack `Mono12Packed` (GigE legacy: high bytes in 0 and 2, both low nibbles in byte 1) and `Mono12p` (PFNC, LSB-first) to `uint16`, from hand-built buffers whose expected values are written out. `bit_depth` 12, `full_scale` 4095.
- [x] Task 2.2: Tests, then the 12-bit packed Bayer formats in all four orders (`...12Packed`, `...12p`) through the same unpackers.
- [x] Task 2.3: Tests, then YCbCr: `YCbCr8_CbYCr` as `(H, W, 3)` and `YCbCr422_8_CbYCrY` as `(H, W, 2)`, unchanged bytes. `YCbCr411_8_CbYYCrYY` stays out of the choices.
- [x] Task 2.4: Add the new formats to `_PIXEL_FORMATS` and their PFNC codes to `_PFNC` (values from the PFNC, as in Aravis's `arvenums.h`). Test that each captures with the right shape and dtype through every backend's fake CM3-U3-31S4C, and that open keeps `BayerRG8`.
- [x] Task 2.5: Tests, then `full_scale` 65504 for the CM3-U3-31S4C's `Mono16`. Consider keying `_SATURATION` by sensor or model family rather than one model at a time.
- [x] Task 2.6: Tests, then the CLI's PNG saving: 12-bit formats save as 16-bit grayscale, and YCbCr saves raw, as Bayer does. `--pixel-format` help lists the families.

### Notes

- The Spinnaker backend now names a buffer's format by `GetTLPixelFormat()` through `pfnc_name`, as Aravis and GenTL do, since `GetPixelFormatName()` gives legacy YCbCr names. Formats `GetNDArray()` refuses are shaped from `GetData()`.
- `_PFNC` maps both the PFNC's `YCbCr*` codes and the `YUV*` codes the CM3 sends to the SFNC names.
- `_SATURATION` stays keyed by model: both CM3s hold 12 bits at the top of `Mono16`, but other Chameleon3s have other sensors, so a family key would claim too much.
- The fake's default color formats gained `YCbCr411_8_CbYYCrYY`, now the format the driver can't shape; `Mono12Packed` is shapeable.

### Verification

- [x] Full suite (the two pre-existing real-PySpin name checks aside).

## Phase 3: Bayer Pattern

### Tasks

- [-] Task 3.1 (dropped, see Probe 3): **Approval needed:** adding `color_filter` to `PreviewFrame` (`cameras/base.py`). Confirm the name and type (`str | None`) with the maintainer before changing it.
- [x] Task 3.2 (frame metadata only; the `PreviewFrame` half went with 3.1): Tests, then the core reads `PixelColorFilter` at open, falling back to the Bayer format name, or `None`. The value goes in `frame.metadata["color_filter"]` and in `PreviewFrame.color_filter`.
- [x] Task 3.3 (frame metadata only; the preview half went with 3.1): Tests, then `SimulatedCamera(color=True)` reports `color_filter="BayerRG"` and offers `BayerRG8` and `BayerRG16` (mosaicked from its RGB image), still opening in `RGB8`. Mono simulated cameras and gphoto2 report `None`.
- [x] Task 3.4 (frames only; the preview half went with 3.1): Contract tests: a camera reports `color_filter` in every frame's metadata and every preview frame, and a Bayer format's name agrees with it.

### Notes

- `GenICamCamera.color_filter` exposes the mosaic too, as `timestamp_offset` does, for callers that want it before a capture.
- The fake Aravis now follows `pIsAvailable` in `is_feature_available`, as real Aravis does; without it a node unavailable in the current format (SharpnessEnabled in Bayer) read as readable.
- `el-devices preview` shows YCbCr frames' Y channel as grayscale; it crashed on 2-channel YCbCr422 frames otherwise.
- `TestColorCM3Contract` runs the frame-camera contract on the fake CM3-U3-31S4C through all four backends.
- For 3.1: a host-strided preview of a Bayer format (stride 2, or an odd crop origin) no longer shows the mosaic `color_filter` names. Decide with the `PreviewFrame` change whether preview reports `None` then, the shifted order, or keeps Bayer previews on even origins and strides.

### Verification

- [x] Full suite.

## Phase 4: White Balance

### Tasks

- [x] Task 4.1: Tests, then `BalanceWhiteAuto` as an `_Enum` with `_AUTO_MODES` (`Off`, `Continuous`), offered while readable.
- [x] Task 4.2: Tests, then a selector-indexed descriptor for `balance_ratio_red` and `balance_ratio_blue`. It sets `BalanceRatioSelector`, reads or writes `BalanceRatio`, and restores the selector, including when the read or write fails. Read-only while auto isn't `Off`, unitless, 0.25–4 on the 31S4C.
- [x] Task 4.3: Tests, then frame metadata records `BalanceWhiteAuto` and both ratios when the camera has them. Check what this costs per capture on the fake (node reads), and cache it if it matters.
- [x] Task 4.4: Tests, then the settings appear and disappear with `pixel_format` (gone in Mono formats), on every backend's fake.
- [x] Task 4.5: Tests, then `SimulatedCamera(color=True)` offers the same three settings, with ratios that tint its frames, and passes the contract suite.
- [x] Task 4.6: Contract tests: where a camera offers white balance, the ratios are writable only while auto is `Off`, and a written ratio reads back.

### Notes

- Settings `BalanceWhiteAuto`, `balance_ratio_red`, `balance_ratio_blue`; frame metadata uses the same keys. The ratios are unitless and clamped to the node's range like other floats.
- 4.3 cost on the fake: recording reads only values, 18 node accesses and 2 selector writes per capture in a color format (from 26 when it read ranges and access too); none in Mono. Not cached: under `Continuous` the ratios change between frames. Timed on the real camera in Phase 6.
- `SimulatedCamera(color=True)` offers the ratios (1.0, writable); with `auto=True` also `BalanceWhiteAuto`, starting `Continuous`, which picks ratios that even out its tint. Ratios are applied before clipping, so a high one saturates.
- `GenICamHardwareTests` gained color checks: 12-bit against Mono16, `color_filter` in every format, white balance moving its channels, and white balance per format. Its `restore` fixture now restores the pixel format first and skips settings the format lacks.

### Verification

- [x] Full suite (the pre-existing real-PySpin name checks aside).

## Phase 5: Docs and Follow-ups

### Tasks

- [x] Task 5.1: `docs/hardware/spinnaker.md`:
  - A CM3-U3-31S4C section with the probe results.
  - White balance rows in the settings table.
  - The corrected `SharpnessEnabled` row.
  - The new formats.
  - Fixes to the "RGB8 if offered" claims and the "Not exposed" list.
  - The `link_recoveries` observation.
- [x] Task 5.2: `docs/hardware/usb3-vision.md`: the throughput maximum per format, RGB8's 3 bytes per pixel, and the color camera on each backend.
- [x] Task 5.3: `docs/guide.md`: `color_filter`, the new formats' layouts (with the OpenCV and Pillow notes), white balance, and the simulated color camera.
- [x] Task 5.4: File the follow-up GitHub issues: on-camera processing controls (Hue, Saturation, Gamma, `SharpnessAuto`, black level), and `YCbCr411_8_CbYYCrYY` with its two layout options. Link them from the spec.

### Notes

- Besides the three planned docs, `docs/preview.md` gained "Color cameras: preview in `RGB8` or YCbCr" (the maintainer's preview decision) and the format switch in a capture's round trip.
- Issues filed 2026-11-19: [#42](https://github.com/educelab/educelab-devices/issues/42) on-camera processing controls (with probe 4's measurements), [#43](https://github.com/educelab/educelab-devices/issues/43) YCbCr411, [#44](https://github.com/educelab/educelab-devices/issues/44) `link_recoveries` rising per open and close.

### Verification

- [x] The docs agree with the code, and every relative link and anchor in `docs/` resolves (checked by script). There's no `mkdocs.yml` in the repo, so `mkdocs build --strict` can't run.

## Phase 6: Hardware Runs (batched)

Owed on the CM3-U3-31S4C (serial 19299005), with a lit scene, run from `venv/`:

### Tasks

- [x] Task 6.1: Every format through `el-devices spinnaker` and `el-devices genicam --backend aravis|gentl`. Record shape, dtype, the maximum read at saturation, and frame rate and MB/s.
- [x] Task 6.2: Check the 12-bit unpacking against `Mono16` frames of the same scene, as `Mono10g40IDS` was checked: the values should agree after shifting.
- [x] Task 6.3: White balance: set ratios with auto `Off` and check each channel moves as expected in `BayerRG8`, `RGB8`, and `YCbCr422`. Check the metadata, and that the settings disappear in `Mono8`.
- [x] Task 6.4 (captures; previews dropped with 3.1): `color_filter` in captures and previews, including a cropped preview.
- [x] Task 6.5: `pytest -m hardware` for the Spinnaker, Aravis, and GenTL suites. Watch `link_recoveries`, and file an issue if it keeps rising.
- [x] Task 6.6: Record everything in `spinnaker.md` and `usb3-vision.md` (Manual check and Hardware runs sections). Leave the camera in `BayerRG8` with auto modes `Continuous`.

### Verification

- [x] Results are recorded in the hardware docs.

### Results (2026-11-19, rig Mac, room scene; static for the runs below)

The maintainer warned the scene moved during the first runs; only the runs after it was static are recorded here, plus scene-independent facts.

- **6.1, every format, 60 frames, 2 ms exposure** (`el-devices spinnaker`, `genicam --backend aravis|gentl`): all 60/60 on all three backends, same rates on each. Throughput limit maximum / streamed: Mono8 117.216 / 115.2 MB/s at 36.6 fps; Mono12Packed and Mono12p 175.232 / 172.9 at 36.6; Mono16 195.424 / 195.3 at 31.0; BayerRG8 177.408 / 174.8 at 55.6; BayerRG16 195.424 / 195.3 at 31.0; YCbCr422 195.424 / 195.3 at 31.0; YCbCr8 and RGB8 198.24 / 198.3 at 21.0. The processed 8- and 12-bit mono formats stop at 36.6 fps below their link limit; BayerRG8 reaches 55.6.
- **6.2, 12-bit vs Mono16 >> 4:** Mono12Packed and Mono12p correlate 0.997–0.999 with it on all three backends, and differ by about the frame-to-frame noise (7.3 vs 7.1 counts on Spinnaker; 31–39 vs 27–35 in a brighter frame on Aravis and GenTL). Saturated: 4094 in both 12-bit formats.
- **6.3, white balance (BayerRG16 and BayerRG8, exposure 10 ms, gain 0):** the ratios scale the signal above a black-level pedestal of 3840 in 16 bits (15 in 8; `BlackLevel` reads 5.859, percent of full scale): red 2 and 4 give ×2.01 and ×4.09, blue ×2.02 and ×4.07. Below 1 the camera raises the other channels instead, keeping its smallest gain at 1: red 0.5 leaves red and doubles green (×2.04) and blue; 0.25 gives green ×4.08. The hardware test checks this pedestal-free on all three backends. In RGB8 and both YCbCr formats the ratios move the right channels (red ratio 2: R 19.5 → 24.6 in RGB8, Cr 128.0 → 130.2 in YCbCr; blue ratio 2: B 16.0 → 17.3, Cb 126.2 → 126.9) in a dim scene, so direction only. Frame metadata matched every write. Camera left in BayerRG8 with ExposureAuto, GainAuto and BalanceWhiteAuto on Continuous.
- **6.4:** `color_filter` is `BayerRG` in every format on all three backends. Previews wait on Task 3.1.
- **6.5:** `pytest -m hardware`: Spinnaker 39 passed, 1 skipped; Aravis and GenTL 38 passed, 1 skipped. The white balance check failed once in four Spinnaker runs (red's step ratio at the edge of 1 ± 0.15). Bayer previews correlate about 0.85 with their region (Mono8 0.95), close to the move test's 0.8 bar.
- **`link_recoveries`** (scene-independent) rises by one per open and close through Spinnaker or Spinnaker's GenTL producer, rarely through Aravis, and never while streaming (300 captures over three format changes: none). It read 138 at the end of the runs.
- **Cost of recording white balance** (scene-independent): reading the ratios takes about 1.2 ms each; recording took 4.9 ms per capture on Spinnaker and GenTL and 6.8 ms on Aravis. Cached while auto is `Off`: 0.8 and 1.2 ms. Under `Continuous` it stays at 5–7 ms.

Written into `spinnaker.md` and `usb3-vision.md` in Phase 5.

## Final Verification

- [x] All acceptance criteria met (two revised; see spec)
- [ ] Tests passing on CI (Ruff, pytest matrix). Locally: Ruff clean; pytest 3359 passed, with the 3 real-PySpin name checks that fail on `main` too; mypy's 4 errors are `main`'s.
- [x] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
