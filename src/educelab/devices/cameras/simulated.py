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
    Setting,
    SettingValue,
)
from educelab.devices.cameras.focus import FocusDrive, FocusLimit
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceTimeout, FocusLimitReached

__all__ = ["SimulatedCamera", "SimulatedFileCamera", "SimulatedFocusDrive"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=np.unsignedinteger[Any])

# Exposure that renders the test pattern at roughly half brightness, at 0 dB.
_REFERENCE_EXPOSURE_S = 0.01
_NOISE_SIGMA = 0.01
_PREVIEW_STRIDE = 4
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


class SimulatedCamera(FrameCamera):
    """A frame camera that renders a noisy gradient.

    Brightness scales with ``exposure_time`` and ``gain``, and each capture
    takes ``exposure_time`` seconds.

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
            whose frames are cropped from a larger sensor. Defaults to the
            frame size.
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
    ) -> None:
        super().__init__()
        sensor_width, sensor_height = sensor_size or (width, height)
        if sensor_width < width or sensor_height < height:
            raise ValueError(
                f"sensor_size {sensor_size} is smaller than the frame {width}x{height}"
            )
        self._width = width
        self._height = height
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

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        return _quantize(self._render(_PREVIEW_STRIDE), np.uint8)

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        self._run_auto()
        # Frames are rendered on demand, so only a future not_before needs a wait.
        exposure = _number(self._store[EXPOSURE_TIME])
        wait = 0.0
        if not_before is not None:
            wait = max((not_before - datetime.now(UTC)).total_seconds(), 0.0)
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

    def _render(self, stride: int = 1) -> npt.NDArray[np.float64]:
        """The image in the current pixel format's shape: height × width × 3 for
        RGB8. Every ``stride``-th pixel is kept, before tinting.
        """
        image = _render(
            self._width,
            self._height,
            _number(self._store[EXPOSURE_TIME]),
            _number(self._store[GAIN]),
            self._rng,
        )[::stride, ::stride]
        if self._store[PIXEL_FORMAT].value == "RGB8":
            return image[..., np.newaxis] * _RGB_TINT
        return image


class SimulatedFileCamera(FileCamera):
    """A file camera that produces one 8-bit PGM file per capture.

    Files are named ``SIM_0001.pgm``, ``SIM_0002.pgm``, and so on. Each
    capture takes ``exposure_time`` seconds.

    It can start like a DSLR at Bulb or Auto ISO, where ``exposure_time`` or
    ``iso`` reads as ``None`` until a value is written.

    Args:
        width: Image width in pixels.
        height: Image height in pixels.
        seed: Seed for the noise, for repeatable images.
        bulb: Start with the shutter at Bulb.
        auto_iso: Start at Auto ISO, which renders as ISO 100.
    """

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        seed: int = 0,
        *,
        bulb: bool = False,
        auto_iso: bool = False,
    ) -> None:
        super().__init__()
        self._width = width
        self._height = height
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

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        image = self._render()[::_PREVIEW_STRIDE, ::_PREVIEW_STRIDE]
        return _quantize(image, np.uint8)

    def _capture_files(self) -> list[CameraFile]:
        time.sleep(self._exposure())
        pixels = _quantize(self._render(), np.uint8)
        self._file_count += 1
        header = f"P5\n{self._width} {self._height}\n255\n".encode("ascii")
        return [
            CameraFile(f"SIM_{self._file_count:04d}.pgm", header + pixels.tobytes())
        ]

    def _render(self) -> npt.NDArray[np.float64]:
        # ISO 100 is the 0 dB reference, like gain on a frame camera.
        iso = self._store[ISO]
        gain_db = 0.0 if iso.value is None else 20 * np.log10(_number(iso) / 100)
        return _render(
            self._width,
            self._height,
            self._exposure(),
            float(gain_db),
            self._rng,
        )

    def _exposure(self) -> float:
        setting = self._store[EXPOSURE_TIME]
        return _BULB_EXPOSURE_S if setting.value is None else _number(setting)


def _number(setting: Setting) -> float:
    # Callers handle the None states (Bulb, Auto ISO) before getting here.
    assert isinstance(setting.value, int | float), setting
    return float(setting.value)


def _render(
    width: int,
    height: int,
    exposure: float,
    gain_db: float,
    rng: np.random.Generator,
) -> npt.NDArray[np.float64]:
    """A diagonal gradient scaled by exposure and gain, with noise, in 0–1."""
    ys, xs = np.mgrid[0:height, 0:width]
    pattern = 0.5 + 0.5 * (xs / max(width - 1, 1) + ys / max(height - 1, 1)) / 2
    scale = (exposure / _REFERENCE_EXPOSURE_S) * 10 ** (gain_db / 20)
    image = pattern * scale * 0.5 + rng.normal(0.0, _NOISE_SIGMA, pattern.shape)
    return np.clip(image, 0.0, 1.0)


def _quantize(image: npt.NDArray[np.float64], dtype: type[T]) -> npt.NDArray[T]:
    return (image * np.iinfo(dtype).max).round().astype(dtype)


class SimulatedFocusDrive(FocusDrive):
    """A focus drive over a fixed number of steps between its limits.

    Its hidden position survives close and reopen, like a real lens, even
    though `focus_steps` does not.

    Args:
        travel_steps: Steps from the near limit to the far limit.
        start: Hidden starting position, in steps from the near limit.
    """

    def __init__(self, travel_steps: int = 1000, start: int = 500) -> None:
        super().__init__()
        if not 0 <= start <= travel_steps:
            raise ValueError(f"start must be within 0–{travel_steps}, not {start}")
        self._travel = travel_steps
        self._lens_position = start

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
