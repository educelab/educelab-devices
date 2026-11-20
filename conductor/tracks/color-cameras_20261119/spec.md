# Specification: Color GenICam Cameras

**Track ID:** color-cameras_20261119
**Type:** Feature
**Created:** 2026-11-19
**Status:** Draft

## Summary

Fully support color GenICam cameras, starting with the Chameleon3 CM3-U3-31S4C: every pixel format the camera offers (Bayer, RGB8, 12-bit packed, and YCbCr), the sensor's Bayer pattern in frame metadata, and white balance as settings, on the Spinnaker, Aravis, and GenTL backends.

## Context

Every GenICam camera checked so far has been mono (five CM3-U3-50S5M, one IDS U3-36P0XCP-M). The core already shapes RGB8 and 8/16-bit Bayer, and the fakes include color cameras, but no color camera has run on real hardware. A probe of a CM3-U3-31S4C on 2026-11-19 (macOS 27, Spinnaker 4.4.0.246, Python 3.12.15, `venv/`) found:

- **Identity.** Chameleon3 CM3-U3-31S4C, serial 19299005 (Aravis lists `01267ABD`, as for the 50S5M), firmware 1.12.3.00, Sony IMX265 (1/1.8" color CMOS), 2048 × 1536. `PixelColorFilter` reads `BayerRG` in every pixel format, including Mono. `OffsetX` and `OffsetY` step by 2, so an ROI keeps the pattern.
- **Formats.** It opens in `BayerRG8`, and the driver keeps it. It offers `Mono8`, `Mono12Packed`, `Mono12p`, `Mono16`, `BayerRG8`, `BayerRG16`, `YCbCr411_8_CbYYCrYY`, `YCbCr422_8_CbYCrY`, `YCbCr8_CbYCr`, and `RGB8`. The driver handles five of them, and all five captured with the right shape and dtype through Spinnaker. `RGB8` and `BayerRG16` also captured through Aravis and GenTL.
- **Saturation.** `Mono16` saturates at 65504 (12 bits held at the top, as on the 50S5M), but `full_scale` reports 65535. `BayerRG16` reached 65535 with odd values.
- **Throughput maximum varies by format.** Mono8 117.216, Mono16 195.424, BayerRG8 177.408, BayerRG16 195.424, and RGB8 198.24 MB/s.
- **White balance.** `BalanceWhiteAuto` is `Off`, `Once`, or `Continuous`, starting at `Continuous`. `BalanceRatioSelector` is `Red` or `Blue` (no `Green`). `BalanceRatio` runs from 0.25 to 4, and is writable only while auto is `Off`. Writes work while acquiring. The ratios apply to Bayer output as well as RGB8. `Once` hadn't reverted to `Off` after 10 frames of a dark scene, and it holds the ratios read-only. The balance nodes are unavailable in Mono formats.
- **On-camera processing.** In processed formats (RGB8, YCbCr, Mono), `SharpnessEnabled` is on with `SharpnessAuto=Continuous`. `GammaEnabled` and `HueEnabled` are off, and `Saturation` is 100%. None of these reaches Bayer output, and the camera has no color correction matrix node.
- **Open question.** `link_recoveries` rose by about one per Aravis or GenTL run (7 → 10). It stayed at 0 on the mono rig.

## User Story

As a lab developer building an acquisition app (e.g. `pgs-scan` on a color rig), I want to capture raw Bayer, RGB8, or any other format a color machine-vision camera offers, know its Bayer pattern, and fix or record its white balance, so that color captures can be repeated and every frame records the color settings it was taken with.

## Acceptance Criteria

**Formats**
- [x] Every format the CM3-U3-31S4C offers, except `YCbCr411_8_CbYYCrYY`, is in `pixel_format`'s choices and captures through Spinnaker, Aravis, and GenTL. The camera keeps any supported format it opened in.
- [x] `Mono12Packed` and `Mono12p` unpack to `uint16`, with the 12 bits in the low end and `bit_depth` 12. The 12-bit packed Bayer formats (`Bayer{RG,GB,GR,BG}12Packed` and `...12p`) unpack the same way. Each packing layout is checked against the real camera. (The Mono layouts were; the CM3-U3-31S4C offers no 12-bit Bayer format, so those share the checked unpackers unverified on hardware.)
- [x] YCbCr frames keep the camera's layout, as in common libraries:
  - `YCbCr8_CbYCr` is `(H, W, 3)` in Cb, Y, Cr order, like GStreamer's `IYU2`.
  - `YCbCr422_8_CbYCrY` is `(H, W, 2)`, like FFmpeg's `uyvy422` and OpenCV's `UYVY`.
- [x] The PFNC codes of every new format are mapped, for Aravis and GenTL.

**Bayer pattern**
- [x] `frame.metadata["color_filter"]` (revised: not `PreviewFrame.color_filter`; previews stay in the capture's format, and `docs/preview.md` recommends `RGB8` or YCbCr for color previews, per the maintainer, 2026-11-19) gives the sensor's mosaic, e.g. `"BayerRG"`, from `PixelColorFilter`, falling back to the Bayer format's name. It is `None` on a mono sensor or one that can't say.

**White balance**
- [x] On cameras that have the nodes, `BalanceWhiteAuto` (`Off` or `Continuous`; `Once` is left out) and `balance_ratio_red` and `balance_ratio_blue` (through `BalanceRatioSelector`) are settings. The ratios are read-only unless auto is `Off`. Reading or writing a ratio leaves the selector as it found it. Cameras without the nodes, or in a format without them, don't list the settings.
- [x] Frame metadata records the white balance mode and ratios when the camera has them.

**Saturation**
- [x] `full_scale` for the CM3-U3-31S4C's `Mono16` is 65504. `BayerRG16` stays at 65535 unless the hardware run says otherwise.

**Simulation and contracts**
- [x] `SimulatedCamera(color=True)` keeps opening in `RGB8`, also offers `BayerRG8` and `BayerRG16`, reports `color_filter`, and offers white balance. It passes the same contract tests as the driver fakes, which gain a fake CM3-U3-31S4C on each backend.

**Docs**
- [x] `docs/hardware/spinnaker.md` gets a CM3-U3-31S4C section with the probe and hardware run results. Its settings table gains white balance, the `SharpnessEnabled` row and the "RGB8 if offered" claims are corrected, and the new formats are listed.
- [x] `docs/guide.md` covers `color_filter`, the new formats' layouts, white balance, and the corrected simulated-camera note.
- [x] `docs/hardware/usb3-vision.md` notes the per-format throughput maximum and RGB8's 3 bytes per pixel.

**Hardware**
- [x] On the CM3-U3-31S4C, the hardware tests and `el-devices spinnaker` and `el-devices genicam --backend aravis|gentl` pass in every format, and white balance is checked against a lit scene.

## Dependencies

- Existing code: `cameras/_genicam.py` (formats, unpacking, `full_scale`, descriptors, metadata), `cameras/base.py` (`PreviewFrame`), `cameras/simulated.py`, the backends (`spinnaker.py`, `aravis.py`, `gentl.py`) only if a selector-indexed node or a new format needs it, `app/_capture.py` (PNG saving), the GenICam, PySpin, and Aravis fakes in `tests/fakes/`, and `tests/contracts/`.
- No incomplete track. `camera-lens-info_20261118` touches the `Camera` base, not formats or settings.
- **Approval needed:** adding `color_filter` to `PreviewFrame` changes a public camera type (workflow: approval before changing a role ABC).

## Out of Scope

- Debayering or converting YCbCr on the host. Frames come back in the camera's layout, and the caller converts using `color_filter`, as with gphoto2's NEFs.
- On-camera processing controls: Hue, Saturation, Gamma, `SharpnessAuto`, and black level. A follow-up GitHub issue records them: [#42](https://github.com/educelab/educelab-devices/issues/42).
- `YCbCr411_8_CbYYCrYY`. It packs 4 pixels into 6 bytes, so it has no per-pixel shape, and OpenCV can't convert it. A follow-up GitHub issue records the layout options: `(H, W × 3/2)` like FFmpeg's `uyyvyy411`, or `(H, W/4, 6)` groups ([#43](https://github.com/educelab/educelab-devices/issues/43)).
- `BalanceWhiteAuto=Once` and auto white balance regions.
- Investigating `link_recoveries`. Record it in the hardware doc, and file an issue if the hardware runs repeat it. They did: [#44](https://github.com/educelab/educelab-devices/issues/44).
- gphoto2 cameras' white balance.

## Technical Notes

- The 12-bit packings differ. `Mono12Packed` (GigE legacy) puts each pair's upper 8 bits in bytes 0 and 2, and both low nibbles in byte 1. `Mono12p` (PFNC) packs LSB-first. Unpack beside `_unpack_10g40`, and pin each with a hand-built buffer test before the hardware run.
- `balance_ratio_*` use descriptive names, which the guide allows when the hardware doc maps them to the node and selector. A descriptor that sets the selector, reads or writes, and restores it fits the existing `_Descriptor` pattern.
- White balance and `SharpnessEnabled` appear and disappear with `pixel_format`, so their descriptors read availability each time, as `SharpnessEnabled` already does.
- `full_scale` for `Mono12*` is 4095 unless the camera shows otherwise.
- The CLI's PNG saving handles 1- and 3-channel frames. YCbCr and packed frames need a rule: save YCbCr raw, as Bayer is today.

---

_Generated by Conductor. Review and edit as needed._
