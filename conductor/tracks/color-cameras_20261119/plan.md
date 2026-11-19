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

## Phase 1: Color CM3 Fakes

### Tasks

- [ ] Task 1.1: Add a CM3-U3-31S4C to the shared GenICam fake (`tests/fakes/genicam.py`). It opens in `BayerRG8` and offers the ten formats the camera does. It has `PixelColorFilter=BayerRG`, `BalanceWhiteAuto` (`Off`/`Once`/`Continuous`, starting at `Continuous`), and `BalanceRatioSelector` (`Red`/`Blue`) with `BalanceRatio` (0.25–4, writable only while auto is `Off`). The balance nodes are unavailable in Mono formats, and `SharpnessEnabled` is available only in processed formats. It produces payloads of the right size for each format.
- [ ] Task 1.2: Expose it through the PySpin, Aravis, and GenTL fakes, with the new formats' PFNC codes in the Aravis and GenTL fakes.
- [ ] Task 1.3: Tests of the fake itself (`tests/test_fake_genicam.py`, `tests/test_fake_pyspin.py`, `tests/test_fake_aravis.py`): selector-indexed ratios, access that follows auto mode, and availability that follows the format.

### Verification

- [ ] Full suite: `pytest`, `ruff check .`, `ruff format --check .`.

## Phase 2: Pixel Formats

### Tasks

- [ ] Task 2.1: Tests, then unpack `Mono12Packed` (GigE legacy: high bytes in 0 and 2, both low nibbles in byte 1) and `Mono12p` (PFNC, LSB-first) to `uint16`, from hand-built buffers whose expected values are written out. `bit_depth` 12, `full_scale` 4095.
- [ ] Task 2.2: Tests, then the 12-bit packed Bayer formats in all four orders (`...12Packed`, `...12p`) through the same unpackers.
- [ ] Task 2.3: Tests, then YCbCr: `YCbCr8_CbYCr` as `(H, W, 3)` and `YCbCr422_8_CbYCrY` as `(H, W, 2)`, unchanged bytes. `YCbCr411_8_CbYYCrYY` stays out of the choices.
- [ ] Task 2.4: Add the new formats to `_PIXEL_FORMATS` and their PFNC codes to `_PFNC` (values from the PFNC, as in Aravis's `arvenums.h`). Test that each captures with the right shape and dtype through every backend's fake CM3-U3-31S4C, and that open keeps `BayerRG8`.
- [ ] Task 2.5: Tests, then `full_scale` 65504 for the CM3-U3-31S4C's `Mono16`. Consider keying `_SATURATION` by sensor or model family rather than one model at a time.
- [ ] Task 2.6: Tests, then the CLI's PNG saving: 12-bit formats save as 16-bit grayscale, and YCbCr saves raw, as Bayer does. `--pixel-format` help lists the families.

### Verification

- [ ] Full suite.

## Phase 3: Bayer Pattern

### Tasks

- [ ] Task 3.1: **Approval needed:** adding `color_filter` to `PreviewFrame` (`cameras/base.py`). Confirm the name and type (`str | None`) with the maintainer before changing it.
- [ ] Task 3.2: Tests, then the core reads `PixelColorFilter` at open, falling back to the Bayer format name, or `None`. The value goes in `frame.metadata["color_filter"]` and in `PreviewFrame.color_filter`.
- [ ] Task 3.3: Tests, then `SimulatedCamera(color=True)` reports `color_filter="BayerRG"` and offers `BayerRG8` and `BayerRG16` (mosaicked from its RGB image), still opening in `RGB8`. Mono simulated cameras and gphoto2 report `None`.
- [ ] Task 3.4: Contract tests: a camera reports `color_filter` in every frame's metadata and every preview frame, and a Bayer format's name agrees with it.

### Verification

- [ ] Full suite.

## Phase 4: White Balance

### Tasks

- [ ] Task 4.1: Tests, then `BalanceWhiteAuto` as an `_Enum` with `_AUTO_MODES` (`Off`, `Continuous`), offered while readable.
- [ ] Task 4.2: Tests, then a selector-indexed descriptor for `balance_ratio_red` and `balance_ratio_blue`. It sets `BalanceRatioSelector`, reads or writes `BalanceRatio`, and restores the selector, including when the read or write fails. Read-only while auto isn't `Off`, unitless, 0.25–4 on the 31S4C.
- [ ] Task 4.3: Tests, then frame metadata records `BalanceWhiteAuto` and both ratios when the camera has them. Check what this costs per capture on the fake (node reads), and cache it if it matters.
- [ ] Task 4.4: Tests, then the settings appear and disappear with `pixel_format` (gone in Mono formats), on every backend's fake.
- [ ] Task 4.5: Tests, then `SimulatedCamera(color=True)` offers the same three settings, with ratios that tint its frames, and passes the contract suite.
- [ ] Task 4.6: Contract tests: where a camera offers white balance, the ratios are writable only while auto is `Off`, and a written ratio reads back.

### Verification

- [ ] Full suite.

## Phase 5: Docs and Follow-ups

### Tasks

- [ ] Task 5.1: `docs/hardware/spinnaker.md`:
  - A CM3-U3-31S4C section with the probe results.
  - White balance rows in the settings table.
  - The corrected `SharpnessEnabled` row.
  - The new formats.
  - Fixes to the "RGB8 if offered" claims and the "Not exposed" list.
  - The `link_recoveries` observation.
- [ ] Task 5.2: `docs/hardware/usb3-vision.md`: the throughput maximum per format, RGB8's 3 bytes per pixel, and the color camera on each backend.
- [ ] Task 5.3: `docs/guide.md`: `color_filter`, the new formats' layouts (with the OpenCV and Pillow notes), white balance, and the simulated color camera.
- [ ] Task 5.4: File the follow-up GitHub issues: on-camera processing controls (Hue, Saturation, Gamma, `SharpnessAuto`, black level), and `YCbCr411_8_CbYYCrYY` with its two layout options. Link them from the spec.

### Verification

- [ ] The docs build (`mkdocs build --strict`) and agree with the code.

## Phase 6: Hardware Runs (batched)

Owed on the CM3-U3-31S4C (serial 19299005), with a lit scene, run from `venv/`:

### Tasks

- [ ] Task 6.1: Every format through `el-devices spinnaker` and `el-devices genicam --backend aravis|gentl`. Record shape, dtype, the maximum read at saturation, and frame rate and MB/s.
- [ ] Task 6.2: Check the 12-bit unpacking against `Mono16` frames of the same scene, as `Mono10g40IDS` was checked: the values should agree after shifting.
- [ ] Task 6.3: White balance: set ratios with auto `Off` and check each channel moves as expected in `BayerRG8`, `RGB8`, and `YCbCr422`. Check the metadata, and that the settings disappear in `Mono8`.
- [ ] Task 6.4: `color_filter` in captures and previews, including a cropped preview.
- [ ] Task 6.5: `pytest -m hardware` for the Spinnaker, Aravis, and GenTL suites. Watch `link_recoveries`, and file an issue if it keeps rising.
- [ ] Task 6.6: Record everything in `spinnaker.md` and `usb3-vision.md` (Manual check and Hardware runs sections). Leave the camera in `BayerRG8` with auto modes `Continuous`.

### Verification

- [ ] Results are recorded in the hardware docs.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing on CI (Ruff, pytest matrix)
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
