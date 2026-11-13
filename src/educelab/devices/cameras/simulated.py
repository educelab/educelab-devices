"""Cameras and a focus drive with no hardware behind them."""

import dataclasses
import logging
import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras._preview import HostCrop, host_crop
from educelab.devices.cameras.base import (
    EXPOSURE_TIME,
    GAIN,
    HEIGHT,
    ISO,
    PIXEL_FORMAT,
    SENSOR_HEIGHT,
    SENSOR_WIDTH,
    WIDTH,
    CameraFile,
    FileCamera,
    Frame,
    FrameCamera,
    PreviewFrame,
    PreviewGeometry,
    PreviewRegion,
    Setting,
    SettingValue,
    seconds_until,
)
from educelab.devices.cameras.focus import FocusDrive, FocusLimit
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceTimeout, FocusLimitReached

__all__ = [
    "BLUR_STEPS_PER_PX",
    "PATTERNS",
    "SimulatedCamera",
    "SimulatedFileCamera",
    "SimulatedFocusDrive",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=np.unsignedinteger[Any])

# Exposure that renders the test pattern at roughly half brightness, at 0 dB.
_REFERENCE_EXPOSURE_S = 0.01
_NOISE_SIGMA = 0.01
# A real shutter at Bulb stays open while the button is held. Simulate a short hold.
_BULB_EXPOSURE_S = _REFERENCE_EXPOSURE_S
_VENDOR = "EduceLab"
# Per-channel scale for colour frames, so the channels differ.
_RGB_TINT = np.array([1.0, 0.85, 0.7])
# GenICam's auto-mode switches and the setting each one locks while on.
_AUTO_MODES = ("Off", "Continuous")
_AUTO_LOCKS = {"ExposureAuto": EXPOSURE_TIME, "GainAuto": GAIN}
# Auto modes aim at twice the reference exposure, so auto gain alone at the
# default exposure always adds some gain. The aim wanders by _AUTO_HUNT.
_AUTO_TARGET_S = 2 * _REFERENCE_EXPOSURE_S
_AUTO_HUNT = 0.1

PATTERNS = ("gradient", "chart")
"""What a simulated camera can look at. ``"chart"`` has a Siemens star in the
middle, bars 1, 2 and 3 px wide each way at the left, a 64-px grid of 1-px
lines, a grey step wedge along the bottom, and a patch past full scale at the
bottom right."""
BLUR_STEPS_PER_PX = 25
"""Focus steps from sharp per pixel of blur (a Gaussian's sigma)."""
# Chart levels, in the units the gradient uses: 1.0 renders at half scale at
# the reference exposure and 0 dB.
_DARK, _MID, _LIGHT, _OVER = 0.15, 0.5, 1.6, 3.0
# Blurred charts kept, by sigma rounded to this many pixels.
_BLUR_ROUNDING_PX = 0.25
_BLURS_KEPT = 4


class SimulatedCamera(FrameCamera):
    """A frame camera that renders a noisy gradient, or a test chart.

    Brightness scales with ``exposure_time`` and ``gain``, and each capture
    or preview image takes ``exposure_time`` seconds. Preview crops and
    strides the rendered frame on the host.

    With ``auto``, it also offers ``ExposureAuto`` and ``GainAuto`` (``Off`` or
    ``Continuous``), as GenICam cameras do, both starting at ``Continuous``.
    While one is on, the setting it controls is read-only, and each capture
    picks a slightly different value for it, which the frame's metadata
    records. As on real cameras, exposure is adjusted first: with both on,
    gain stays at its minimum unless exposure reaches its limit. Turning a
    mode off keeps the last value.

    Args:
        width: Frame width in pixels.
        height: Frame height in pixels.
        seed: Seed for the noise and the auto modes, for repeatable frames.
        color: Act like a colour camera, which also offers ``RGB8`` and starts
            in it, as GenICam colour cameras do.
        auto: Offer auto exposure and auto gain.
        sensor_size: The whole sensor as ``(width, height)``, for a camera
            whose frames are cropped from the middle of a larger sensor.
            Defaults to the frame size.
        pattern: ``"gradient"``, or ``"chart"`` for a test chart with detail
            down to single pixels, to check previews and focus by (see
            `PATTERNS`).
        focus: A drive whose lens position blurs the image, sharpest at its
            ``sharp_at``.
    """

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        seed: int = 0,
        *,
        color: bool = False,
        auto: bool = False,
        sensor_size: tuple[int, int] | None = None,
        pattern: str = "gradient",
        focus: "SimulatedFocusDrive | None" = None,
    ) -> None:
        super().__init__()
        self._scene = _Scene(pattern, (width, height), focus)
        sensor_width, sensor_height = sensor_size or (width, height)
        if sensor_width < width or sensor_height < height:
            raise ValueError(
                f"sensor_size {sensor_size} is smaller than the frame {width}x{height}"
            )
        self._width = width
        self._height = height
        self._sensor = (sensor_width, sensor_height)
        self._crop: HostCrop | None = None
        self._color = color
        self._rng = np.random.default_rng(seed)
        # Separate, so turning auto on doesn't change the noise.
        self._auto_rng = np.random.default_rng([seed, 1])
        self._frame_count = 0
        formats = ("Mono8", "Mono16", "RGB8") if color else ("Mono8", "Mono16")
        settings = [
            Setting(EXPOSURE_TIME, 0.01, 1e-5, 10.0, unit="s", read_only=auto),
            Setting(GAIN, 0.0, 0.0, 24.0, unit="dB", read_only=auto),
            Setting(PIXEL_FORMAT, "RGB8" if color else "Mono8", choices=formats),
        ]
        if auto:
            settings += [
                Setting(n, "Continuous", choices=_AUTO_MODES) for n in _AUTO_LOCKS
            ]
        for name, size in (
            (WIDTH, width),
            (HEIGHT, height),
            (SENSOR_WIDTH, sensor_width),
            (SENSOR_HEIGHT, sensor_height),
        ):
            settings.append(Setting(name, size, unit="px", read_only=True))
        self._store = {s.name: s for s in settings}

    @property
    def info(self) -> DeviceInfo:
        model = "SimulatedColorCamera" if self._color else "SimulatedCamera"
        return DeviceInfo(backend="simulated", model=model, vendor=_VENDOR)

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _settings(self) -> Mapping[str, Setting]:
        return self._store

    def _set_setting(self, name: str, value: SettingValue) -> None:
        logger.debug("%s = %r", name, value)
        self._store[name] = dataclasses.replace(self._store[name], value=value)
        if name in _AUTO_LOCKS:
            locked = _AUTO_LOCKS[name]
            self._store[locked] = dataclasses.replace(
                self._store[locked], read_only=value != "Off"
            )

    def _start_preview(
        self, region: PreviewRegion, max_size: int | None, min_size: int | None
    ) -> PreviewGeometry:
        origin = (
            (self._sensor[0] - self._width) // 2,
            (self._sensor[1] - self._height) // 2,
        )
        self._crop = host_crop(
            region,
            max_size,
            sensor=self._sensor,
            frame=(self._width, self._height),
            origin=origin,
            capture=self._sensor,
            min_size=min_size,
        )
        return self._crop.geometry

    def _stop_preview(self) -> None:
        self._crop = None

    def _preview_frame(self, not_before: datetime | None) -> PreviewFrame:
        assert self._crop is not None
        self._run_auto()
        time.sleep(seconds_until(not_before) + _number(self._store[EXPOSURE_TIME]))
        pixel_format = str(self._store[PIXEL_FORMAT].value)
        dtype = np.uint16 if pixel_format == "Mono16" else np.uint8
        return PreviewFrame(
            _quantize(self._render(self._crop), dtype),
            pixel_format,
            datetime.now(UTC),
            self._crop.geometry,
        )

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        self._run_auto()
        # Frames are rendered on demand, so only a future not_before needs a wait.
        exposure = _number(self._store[EXPOSURE_TIME])
        wait = seconds_until(not_before)
        if wait + exposure > timeout:
            raise DeviceTimeout(f"{self!r} has no frame within {timeout:g} s")
        time.sleep(wait + exposure)
        pixel_format = str(self._store[PIXEL_FORMAT].value)
        dtype = np.uint16 if pixel_format == "Mono16" else np.uint8
        self._frame_count += 1
        metadata: dict[str, Any] = {
            EXPOSURE_TIME: exposure,
            GAIN: _number(self._store[GAIN]),
            "frame_number": self._frame_count,
            "bit_depth": 8 * np.dtype(dtype).itemsize,
            "full_scale": int(np.iinfo(dtype).max),
        }
        return Frame(
            _quantize(self._render(), dtype),
            pixel_format,
            datetime.now(UTC),
            metadata,
        )

    def _run_auto(self) -> None:
        """Pick this frame's exposure and gain for the auto modes that are on,
        with exposure tried before gain."""
        exposure_auto = self._auto_on("ExposureAuto")
        gain_auto = self._auto_on("GainAuto")
        if not (exposure_auto or gain_auto):
            return
        hunt = self._auto_rng.uniform(1 - _AUTO_HUNT, 1 + _AUTO_HUNT)
        target = _AUTO_TARGET_S * hunt
        gain = 0.0 if gain_auto else _number(self._store[GAIN])
        if exposure_auto:
            self._set_auto_value(EXPOSURE_TIME, target / 10 ** (gain / 20))
        if gain_auto:
            exposure = _number(self._store[EXPOSURE_TIME])
            self._set_auto_value(GAIN, 20 * math.log10(target / exposure))

    def _auto_on(self, name: str) -> bool:
        setting = self._store.get(name)
        return setting is not None and setting.value == "Continuous"

    def _set_auto_value(self, name: str, value: float) -> None:
        setting = self._store[name]
        assert setting.minimum is not None and setting.maximum is not None
        value = min(max(value, setting.minimum), setting.maximum)
        self._store[name] = dataclasses.replace(setting, value=value)

    def _render(self, crop: HostCrop | None = None) -> npt.NDArray[np.float64]:
        """The image, or the part of it ``crop`` keeps, in the current pixel
        format's shape: height × width × 3 for RGB8.
        """
        image = self._scene.render(
            _number(self._store[EXPOSURE_TIME]),
            _number(self._store[GAIN]),
            self._rng,
            crop,
        )
        if self._store[PIXEL_FORMAT].value == "RGB8":
            return image[..., np.newaxis] * _RGB_TINT
        return image


class SimulatedFileCamera(FileCamera):
    """A file camera that produces one 8-bit PGM file per capture.

    Files are named ``SIM_0001.pgm``, ``SIM_0002.pgm``, and so on. Each
    capture or preview image takes ``exposure_time`` seconds. Preview crops
    and strides the rendered image on the host.

    It can start like a DSLR at Bulb or Auto ISO, where ``exposure_time`` or
    ``iso`` reads as ``None`` until a value is written.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: Seed for the noise, for repeatable images.
        bulb: Start with the shutter at Bulb.
        auto_iso: Start at Auto ISO, which renders as ISO 100.
        pattern: ``"gradient"`` or ``"chart"``, as for `SimulatedCamera`.
        focus: A drive whose lens position blurs the image.
    """

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        seed: int = 0,
        *,
        bulb: bool = False,
        auto_iso: bool = False,
        pattern: str = "gradient",
        focus: "SimulatedFocusDrive | None" = None,
    ) -> None:
        super().__init__()
        self._scene = _Scene(pattern, (width, height), focus)
        self._width = width
        self._height = height
        self._crop: HostCrop | None = None
        self._rng = np.random.default_rng(seed)
        self._file_count = 0
        self._store = {
            s.name: s
            for s in (
                Setting(
                    EXPOSURE_TIME, None if bulb else 0.01, 1 / 8000, 30.0, unit="s"
                ),
                Setting(
                    ISO,
                    None if auto_iso else 100,
                    choices=(100, 200, 400, 800, 1600, 3200),
                ),
                Setting("image_format", "PGM", read_only=True),
            )
        }

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="simulated", model="SimulatedFileCamera", vendor=_VENDOR
        )

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _settings(self) -> Mapping[str, Setting]:
        return self._store

    def _set_setting(self, name: str, value: SettingValue) -> None:
        logger.debug("%s = %r", name, value)
        self._store[name] = dataclasses.replace(self._store[name], value=value)

    def _start_preview(
        self, region: PreviewRegion, max_size: int | None, min_size: int | None
    ) -> PreviewGeometry:
        size = (self._width, self._height)
        self._crop = host_crop(
            region, max_size, sensor=size, frame=size, capture=size, min_size=min_size
        )
        return self._crop.geometry

    def _stop_preview(self) -> None:
        self._crop = None

    def _preview_frame(self, not_before: datetime | None) -> PreviewFrame:
        assert self._crop is not None
        time.sleep(seconds_until(not_before) + self._exposure())
        return PreviewFrame(
            _quantize(self._render(self._crop), np.uint8),
            "Mono8",
            datetime.now(UTC),
            self._crop.geometry,
        )

    def _capture_files(self) -> list[CameraFile]:
        time.sleep(self._exposure())
        pixels = _quantize(self._render(), np.uint8)
        self._file_count += 1
        header = f"P5\n{self._width} {self._height}\n255\n".encode("ascii")
        return [
            CameraFile(f"SIM_{self._file_count:04d}.pgm", header + pixels.tobytes())
        ]

    def _render(self, crop: HostCrop | None = None) -> npt.NDArray[np.float64]:
        # ISO 100 is the 0 dB reference, like gain on a frame camera.
        iso = self._store[ISO]
        gain_db = 0.0 if iso.value is None else 20 * np.log10(_number(iso) / 100)
        return self._scene.render(self._exposure(), float(gain_db), self._rng, crop)

    def _exposure(self) -> float:
        setting = self._store[EXPOSURE_TIME]
        return _BULB_EXPOSURE_S if setting.value is None else _number(setting)


def _number(setting: Setting) -> float:
    # Callers handle the None states (Bulb, Auto ISO) before getting here.
    assert isinstance(setting.value, int | float), setting
    return float(setting.value)


class _Scene:
    """What a simulated camera looks at: a pattern, blurred by a focus drive's
    distance from sharp, then lit by the exposure and gain."""

    def __init__(
        self,
        pattern: str,
        size: tuple[int, int],
        focus: "SimulatedFocusDrive | None",
    ) -> None:
        if pattern not in PATTERNS:
            raise ValueError(f"pattern must be one of {PATTERNS}, not {pattern!r}")
        self._pattern = pattern
        self._size = size
        self._focus = focus
        self._sharp: npt.NDArray[np.float32] | None = None
        self._blurred: dict[float, npt.NDArray[np.float32]] = {}

    def render(
        self,
        exposure: float,
        gain_db: float,
        rng: np.random.Generator,
        crop: HostCrop | None = None,
    ) -> npt.NDArray[np.float64]:
        """The image in 0–1, or the part of it ``crop`` keeps."""
        width, height = self._size
        if self._pattern == "gradient" and self._focus is None:
            # As before charts existed, so seeded frames don't change.
            image = _render(width, height, exposure, gain_db, rng)
            return image if crop is None else crop.apply(image)
        base = self._base()
        return _expose(
            base if crop is None else crop.apply(base), exposure, gain_db, rng
        )

    def _base(self) -> npt.NDArray[np.float32]:
        """The pattern, blurred as the focus drive says; kept between frames."""
        if self._sharp is None:
            width, height = self._size
            draw = _chart if self._pattern == "chart" else _gradient
            self._sharp = draw(width, height).astype(np.float32)
        sigma = 0.0 if self._focus is None else self._focus._blur_px()
        sigma = round(sigma / _BLUR_ROUNDING_PX) * _BLUR_ROUNDING_PX
        if sigma < _BLUR_ROUNDING_PX:
            return self._sharp
        if sigma not in self._blurred:
            if len(self._blurred) >= _BLURS_KEPT:
                self._blurred.pop(next(iter(self._blurred)))
            self._blurred[sigma] = _blur(self._sharp, sigma)
        return self._blurred[sigma]


def _gradient(width: int, height: int) -> npt.NDArray[np.float64]:
    """A diagonal gradient from 0.5 at the top left to 1.0 at the bottom right."""
    ys, xs = np.mgrid[0:height, 0:width]
    return 0.5 + 0.5 * (xs / max(width - 1, 1) + ys / max(height - 1, 1)) / 2


def _chart(width: int, height: int) -> npt.NDArray[np.float32]:
    """A test chart at one sample per pixel (`PATTERNS` describes it)."""
    image = np.full((height, width), _MID, np.float32)
    # A grid of 1-px lines every 64 px.
    image[::64, :] = _LIGHT
    image[:, ::64] = _LIGHT
    short = min(width, height)
    # A Siemens star of 48 spokes in the middle.
    ys, xs = np.mgrid[0:height, 0:width]
    dx, dy = xs - (width - 1) / 2, ys - (height - 1) / 2
    radius = np.hypot(dx, dy)
    spokes = np.floor((np.arctan2(dy, dx) + np.pi) / (np.pi / 48)).astype(int) % 2
    star = radius <= 0.3 * short
    image[star] = np.where(spokes[star] == 1, _LIGHT, _DARK)
    # Bars 1, 2 and 3 px wide, vertical then horizontal, down the left side.
    margin, block = max(short // 32, 4), max(short // 8, 12)
    for row, width_px in enumerate((1, 2, 3)):
        top = margin + row * (block + margin)
        for col, along in enumerate((xs, ys)):
            left = margin + col * (block + margin)
            if top + block > height or left + block > width:
                continue
            cell = (slice(top, top + block), slice(left, left + block))
            dark = (along[cell] // width_px) % 2 == 0
            image[cell] = np.where(dark, _DARK, _LIGHT)
    # A step wedge along the bottom, from black to just under full scale.
    steps = 11
    wedge_top = height - margin - block // 2
    step_w = max((width - 2 * margin) // (steps + 2), 1)
    for k in range(steps):
        left = margin + k * step_w
        image[wedge_top : height - margin, left : left + step_w] = 1.9 * k / (steps - 1)
    # A patch that saturates at the reference exposure.
    side = max(short // 10, 8)
    patch = (
        slice(wedge_top - margin - side, wedge_top - margin),
        slice(-margin - side, -margin),
    )
    image[patch] = _OVER
    return image


def _blur(image: npt.NDArray[np.float32], sigma: float) -> npt.NDArray[np.float32]:
    """A Gaussian blur of ``sigma`` pixels, as three box blurs each way."""
    width = max(round(math.sqrt(12 * sigma**2 / 3 + 1)), 1)
    out: npt.NDArray[Any] = image
    for axis in (0, 1):
        for _ in range(3):
            out = _box(out, width, axis)
    return out.astype(np.float32)


def _box(image: npt.NDArray[Any], width: int, axis: int) -> npt.NDArray[np.float64]:
    """A moving average ``width`` pixels wide along ``axis``, edges repeated."""
    before, after = width // 2, width - 1 - width // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (before + 1, after)
    padded = np.pad(image.astype(np.float64, copy=False), pad, mode="edge")
    total = np.cumsum(padded, axis=axis)
    upper = [slice(None)] * 2
    lower = [slice(None)] * 2
    upper[axis], lower[axis] = slice(width, None), slice(None, -width)
    out = total[tuple(upper)] - total[tuple(lower)]
    out /= width
    return out


def _render(
    width: int,
    height: int,
    exposure: float,
    gain_db: float,
    rng: np.random.Generator,
) -> npt.NDArray[np.float64]:
    """A diagonal gradient scaled by exposure and gain, with noise, in 0–1."""
    return _expose(_gradient(width, height), exposure, gain_db, rng)


def _expose(
    pattern: npt.NDArray[Any], exposure: float, gain_db: float, rng: np.random.Generator
) -> npt.NDArray[np.float64]:
    """``pattern`` lit by the exposure and gain, with noise, clipped to 0–1."""
    scale = (exposure / _REFERENCE_EXPOSURE_S) * 10 ** (gain_db / 20)
    image = pattern * scale * 0.5 + rng.normal(0.0, _NOISE_SIGMA, pattern.shape)
    return np.clip(image, 0.0, 1.0)


def _quantize(image: npt.NDArray[np.float64], dtype: type[T]) -> npt.NDArray[T]:
    return (image * np.iinfo(dtype).max).round().astype(dtype)


class SimulatedFocusDrive(FocusDrive):
    """A focus drive over a fixed number of steps between its limits.

    Its hidden position survives close and reopen, like a real lens, even
    though `focus_steps` does not.

    Given to a simulated camera, it blurs the image by how far the lens is
    from ``sharp_at``: 1 pixel of blur per `BLUR_STEPS_PER_PX` steps.

    Args:
        travel_steps: Steps from the near limit to the far limit.
        start: Hidden starting position, in steps from the near limit.
        sharp_at: Where the image is sharpest, in steps from the near limit.
            Defaults to the middle of the travel.
    """

    def __init__(
        self, travel_steps: int = 1000, start: int = 500, sharp_at: int | None = None
    ) -> None:
        super().__init__()
        if not 0 <= start <= travel_steps:
            raise ValueError(f"start must be within 0–{travel_steps}, not {start}")
        sharp = travel_steps // 2 if sharp_at is None else sharp_at
        if not 0 <= sharp <= travel_steps:
            raise ValueError(f"sharp_at must be within 0–{travel_steps}, not {sharp}")
        self._travel = travel_steps
        self._lens_position = start
        self._sharp_at = sharp

    def _blur_px(self) -> float:
        """How blurred the lens makes the image, as a Gaussian's sigma."""
        return abs(self._lens_position - self._sharp_at) / BLUR_STEPS_PER_PX

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="simulated", model="SimulatedFocusDrive")

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _step(self, steps: int) -> None:
        target = self._lens_position + steps
        self._lens_position = min(max(target, 0), self._travel)
        logger.debug("focus at %d of %d", self._lens_position, self._travel)
        if target < 0:
            raise FocusLimitReached("near")
        if target > self._travel:
            raise FocusLimitReached("far")

    def _drive_to_limit(self, limit: FocusLimit) -> None:
        self._lens_position = 0 if limit == "near" else self._travel
