"""The camera roles and their value types."""

import logging
import math
import os
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from numbers import Integral, Real
from pathlib import Path, PurePath
from typing import Any

import numpy.typing as npt

from educelab.devices.device import Device, synchronized
from educelab.devices.errors import PreviewError
from educelab.devices.settings import Rounding, Setting, SettingValue

__all__ = [
    "APERTURE",
    "CANONICAL_UNITS",
    "EXPOSURE_TIME",
    "GAIN",
    "HEIGHT",
    "ISO",
    "PIXEL_FORMAT",
    "SENSOR_HEIGHT",
    "SENSOR_WIDTH",
    "WIDTH",
    "Camera",
    "CameraFile",
    "FileCamera",
    "Frame",
    "FrameCamera",
    "PreviewFrame",
    "PreviewGeometry",
    "PreviewRegion",
    "Rounding",
    "Setting",
    "SettingValue",
]

logger = logging.getLogger(__name__)


# Canonical setting names mean the same thing on every camera.
EXPOSURE_TIME = "exposure_time"
GAIN = "gain"
ISO = "iso"
APERTURE = "aperture"
PIXEL_FORMAT = "pixel_format"
# The size of the frames a capture returns, and of the whole sensor. Read-only.
WIDTH = "width"
HEIGHT = "height"
SENSOR_WIDTH = "sensor_width"
SENSOR_HEIGHT = "sensor_height"

CANONICAL_UNITS: Mapping[str, str | None] = {
    EXPOSURE_TIME: "s",
    GAIN: "dB",
    ISO: None,
    APERTURE: "f-number",
    PIXEL_FORMAT: None,
    WIDTH: "px",
    HEIGHT: "px",
    SENSOR_WIDTH: "px",
    SENSOR_HEIGHT: "px",
}
"""Unit of each canonical setting. Settings not listed use the camera's own name."""


# Slack for float error when a region's far edge lands on the sensor edge.
_EDGE_TOLERANCE = 1e-9


@dataclass(frozen=True)
class PreviewRegion:
    """The part of the sensor a preview shows, in fractions of the sensor.

    ``x`` and ``y`` are the top-left corner and ``width`` and ``height`` the
    size, each from 0.0 to 1.0 of the sensor's width or height. The default is
    the whole sensor.

    Raises:
        TypeError: A field isn't a real number.
        ValueError: The box is empty or reaches outside the sensor.
    """

    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(
                    f"preview region {name} must be a number, not "
                    f"{type(value).__name__}"
                )
            object.__setattr__(self, name, float(value))
        x, y, w, h = self.x, self.y, self.width, self.height
        if not all(math.isfinite(v) for v in (x, y, w, h)):
            raise ValueError(f"preview region must be finite, not {self}")
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise ValueError(
                f"preview region needs x, y >= 0 and a positive size, not {self}"
            )
        if x + w > 1 + _EDGE_TOLERANCE or y + h > 1 + _EDGE_TOLERANCE:
            raise ValueError(f"preview region reaches outside the sensor: {self}")

    @classmethod
    def of(
        cls, value: "PreviewRegion | tuple[float, float, float, float] | None"
    ) -> "PreviewRegion":
        """A region from a region, an ``(x, y, width, height)`` tuple, or
        ``None`` for the whole sensor."""
        if value is None:
            return cls()
        if isinstance(value, PreviewRegion):
            return value
        if not isinstance(value, tuple | list) or len(value) != 4:
            raise TypeError(
                "preview region must be a PreviewRegion or (x, y, width, height), "
                f"not {value!r}"
            )
        return cls(*value)

    @property
    def is_full(self) -> bool:
        """Whether the region is the whole sensor."""
        return self == PreviewRegion()

    def contains(self, other: "PreviewRegion") -> bool:
        """Whether ``other`` lies within this region."""
        tol = _EDGE_TOLERANCE
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.x + other.width <= self.x + self.width + tol
            and other.y + other.height <= self.y + self.height + tol
        )


@dataclass(frozen=True)
class PreviewGeometry:
    """What a camera applied for a preview.

    Attributes:
        region: The sensor region the preview shows, after the camera rounded
            the requested one to what it can deliver.
        width: Width of the preview images, in pixels.
        height: Height of the preview images, in pixels.
        capture_size: The ``(width, height)`` in pixels a capture has of what
            regions are fractions of, or ``None`` where the camera doesn't
            say. On a body with selectable image sizes it is the one selected.
    """

    region: PreviewRegion
    width: int
    height: int
    capture_size: tuple[int, int] | None = None

    @property
    def scale(self) -> float | None:
        """Preview pixels per capture pixel: 1.0 shows every pixel a capture
        would have, 0.25 one in four each way, and 2.0 is enlarged by the
        camera. ``None`` without a `capture_size`."""
        if self.capture_size is None:
            return None
        return self.width / (self.region.width * self.capture_size[0])

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(
                f"preview output must be at least 1 x 1, not {self.width} x "
                f"{self.height}"
            )


def check_max_size(max_size: int | None, name: str = "max_size") -> int | None:
    """Check a preview's ``max_size`` or ``min_size``: a positive int, or
    ``None`` for no limit.

    Raises:
        TypeError: The size isn't an int or ``None``.
        ValueError: The size is less than 1.
    """
    if max_size is None:
        return None
    if isinstance(max_size, bool) or not isinstance(max_size, Integral):
        raise TypeError(f"{name} must be an int or None, not {type(max_size).__name__}")
    if max_size < 1:
        raise ValueError(f"{name} must be at least 1, not {max_size}")
    return int(max_size)


@dataclass(frozen=True, eq=False)
class PreviewFrame:
    """One preview image, with what it shows.

    Attributes:
        pixels: Image data, shaped ``(height, width)`` or
            ``(height, width, channels)``, at the camera's own bit depth.
        pixel_format: Layout of ``pixels``, e.g. ``"Mono16"`` or ``"RGB8"``.
        timestamp: The time of the image, as a timezone-aware host time. A
            camera that can't time its frames reports when the image arrived.
        geometry: The sensor region and size of this image.
        bit_depth: Bits per value in the pixel format, as a capture's
            ``metadata["bit_depth"]`` gives them: 16 for ``Mono16``.
        full_scale: The value a saturated pixel reads, as a capture's
            ``metadata["full_scale"]`` gives it. Below ``2**bit_depth - 1``
            where the camera has fewer bits than the format: 65520 for the
            CM3's 12-bit ``Mono16``.

    Left at 0, `bit_depth` and `full_scale` follow from the pixels' type.
    """

    pixels: npt.NDArray[Any]
    pixel_format: str
    timestamp: datetime
    geometry: PreviewGeometry
    bit_depth: int = 0
    full_scale: int = 0

    def __post_init__(self) -> None:
        if not self.bit_depth:
            object.__setattr__(self, "bit_depth", 8 * self.pixels.dtype.itemsize)
        if not self.full_scale:
            object.__setattr__(self, "full_scale", 2**self.bit_depth - 1)


@dataclass(frozen=True, eq=False)
class Frame:
    """A decoded pixel array from a frame camera.

    Attributes:
        pixels: Image data, shaped ``(height, width)`` or
            ``(height, width, channels)``.
        pixel_format: Layout of ``pixels``, e.g. ``"Mono8"`` or ``"RGB8"``.
        timestamp: The time the camera reports for the frame, as a
            timezone-aware host time. Which moment that is (exposure start,
            end, readout) depends on the camera; see its hardware doc.
        metadata: Capture metadata such as the exposure time used.
    """

    pixels: npt.NDArray[Any]
    pixel_format: str
    timestamp: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraFile:
    """A file exactly as the camera produced it, held in memory.

    Attributes:
        name: The camera's file name, e.g. ``"DSC_0001.NEF"``.
        data: The file's bytes.
    """

    name: str
    data: bytes

    @property
    def suffix(self) -> str:
        """The camera's file extension, including the dot, e.g. ``".NEF"``."""
        return PurePath(self.name).suffix

    def save(
        self, dest_dir: str | os.PathLike[str], stem: str, overwrite: bool = False
    ) -> Path:
        """Write the file to ``dest_dir/<stem><suffix>``.

        Args:
            dest_dir: An existing directory.
            stem: File name without extension. The camera's extension is kept.
            overwrite: Replace an existing file instead of raising.

        Returns:
            The path written.

        Raises:
            FileExistsError: The file exists and ``overwrite`` is false.
            ValueError: ``stem`` is not a plain file name.
        """
        if not stem or PurePath(stem).name != stem:
            raise ValueError(f"stem must be a plain file name, not {stem!r}")
        path = Path(dest_dir) / f"{stem}{self.suffix}"
        try:
            with open(path, "wb" if overwrite else "xb") as f:
                f.write(self.data)
        except FileExistsError:
            raise FileExistsError(
                f"{path} already exists; pass overwrite=True to replace it"
            ) from None
        return path


class Camera(Device):
    """A device that acquires images from a single sensor.

    Use `FrameCamera` or `FileCamera`, depending on what the hardware produces.
    Drivers implement `_settings()`, `_set_setting()`, and the preview hooks
    `_start_preview()`, `_stop_preview()` and `_preview_frame()`.

    Preview is an explicit state (ADR 0007): `start_preview()` enters it,
    `stop_preview()` and `close()` leave it, and captures refuse to run while
    it's on. Preview images keep the camera's resolution and pixel format
    unless the caller asks for less; display scaling is the caller's job.
    """

    _preview: PreviewGeometry | None = None

    @synchronized
    def settings(self) -> dict[str, Setting]:
        """Every setting the camera exposes, keyed by name."""
        return dict(self._settings())

    @synchronized
    def setting(self, name: str) -> Setting:
        """One setting, with its current value.

        Raises:
            KeyError: The camera has no such setting.
        """
        return self._setting(name)

    @synchronized
    def set_setting(self, name: str, value: SettingValue) -> None:
        """Change a setting after checking it against the camera's limits.

        Settings can change during preview and apply to preview images.

        Raises:
            KeyError: The camera has no such setting.
            TypeError: ``value`` has the wrong type.
            ValueError: The setting is read-only or ``value`` is out of range.
        """
        self._set_setting(name, self._setting(name).validate(value))

    @property
    def preview(self) -> PreviewGeometry | None:
        """What the current preview applied, or ``None`` when not previewing."""
        return self._preview

    @synchronized
    def start_preview(
        self,
        region: PreviewRegion | tuple[float, float, float, float] | None = None,
        max_size: int | None = None,
        *,
        min_size: int | None = None,
    ) -> PreviewGeometry:
        """Start previewing, or change the preview if one is running.

        Args:
            region: The part of the sensor to show, as a `PreviewRegion` or an
                ``(x, y, width, height)`` tuple in fractions of the sensor.
                ``None`` shows the whole sensor.
            max_size: Longest side of the preview images, in pixels, to have
                the camera send less data. ``None`` keeps every sensor pixel
                of the region.
            min_size: The fewest pixels wanted on the longest side, e.g. a
                view's size: the camera reduces as far as it can while
                keeping at least that many, where the region has them.
                ``max_size`` still caps it. ``None`` asks for no reduction
                unless ``max_size`` does.

        Returns:
            The region and image size the camera applied. The region lies
            within the requested one.

        Raises:
            TypeError, ValueError: ``region``, ``max_size`` or ``min_size`` is
                invalid, or ``min_size`` is above ``max_size``. The preview is
                left as it was.
            DeviceError: The camera couldn't apply the preview. Preview ends.
        """
        wanted = PreviewRegion.of(region)
        cap = check_max_size(max_size)
        floor = check_max_size(min_size, "min_size")
        if cap is not None and floor is not None and floor > cap:
            raise ValueError(f"min_size {floor} is above max_size {cap}")
        try:
            geometry = self._start_preview(wanted, cap, floor)
        except BaseException:
            self._end_preview()
            raise
        self._preview = geometry
        return geometry

    @synchronized
    def stop_preview(self) -> None:
        """Stop previewing. Does nothing when not previewing."""
        if self._preview is None:
            return
        try:
            self._stop_preview()
        finally:
            self._preview = None

    @synchronized
    def preview_frame(self, *, not_before: datetime | None = None) -> PreviewFrame:
        """The next preview image, for framing and focusing, never a capture.

        Pixels come in the camera's current pixel format, with no conversion.

        Args:
            not_before: Earliest acceptable image time, as a timezone-aware
                datetime. Pass the moment the scene or lens was ready (e.g.
                after a focus step) to skip images taken before it.

        Raises:
            PreviewError: The camera isn't previewing.
            DeviceError: ``not_before`` was given and the camera can't time
                its frames.
        """
        if self._preview is None:
            raise PreviewError(f"{self!r} isn't previewing; call start_preview() first")
        _check_not_before(not_before)
        return self._preview_frame(not_before)

    def close(self) -> None:
        """Disconnect from the device, ending any preview first. Does nothing
        if already closed."""
        with self._lock:
            if self._is_open and self._preview is not None:
                self._end_preview()
            super().close()

    def _end_preview(self) -> None:
        """Leave preview on the way out of an error or `close()`, logging
        rather than raising a failure to stop."""
        try:
            self._stop_preview()
        except Exception as exc:  # noqa: BLE001 - close() and errors must go on
            logger.warning("%r: stopping preview failed: %s", self, exc)
        finally:
            self._preview = None

    def _require_not_previewing(self, call: str) -> None:
        if self._preview is not None:
            raise PreviewError(
                f"{self!r} is previewing; call stop_preview() before {call}(), "
                "then start_preview() again"
            )

    def _reset_state(self) -> None:
        super()._reset_state()
        self._preview = None

    @abstractmethod
    def _settings(self) -> Mapping[str, Setting]: ...

    def _setting(self, name: str) -> Setting:
        try:
            return self._settings()[name]
        except KeyError:
            raise KeyError(f"{self!r} has no setting {name!r}") from None

    @abstractmethod
    def _set_setting(self, name: str, value: SettingValue) -> None: ...

    @abstractmethod
    def _start_preview(
        self, region: PreviewRegion, max_size: int | None, min_size: int | None
    ) -> PreviewGeometry:
        """Apply a preview and report what was applied. Called while
        previewing too (`preview` is the old geometry), to reconfigure.
        ``min_size`` is never above ``max_size``."""

    @abstractmethod
    def _stop_preview(self) -> None:
        """Leave preview and restore whatever capture needs."""

    @abstractmethod
    def _preview_frame(self, not_before: datetime | None) -> PreviewFrame: ...


class FrameCamera(Camera):
    """A camera that delivers decoded pixel arrays (e.g. machine-vision cameras)."""

    capture_timeout: float = 30.0
    """Default limit, in seconds, for `capture_frame()` to return a frame."""

    @synchronized
    def capture_frame(
        self,
        *,
        not_before: datetime | None = None,
        timeout: float | None = None,
    ) -> Frame:
        """Capture one frame with the current settings.

        Free-running cameras may already hold a frame exposed before the call.
        Pass ``not_before`` (e.g. the moment the scene was ready) to get a frame
        whose ``timestamp`` is no earlier than that. Cameras differ in which
        moment they timestamp, so add a margin if that matters.

        Args:
            not_before: Earliest acceptable frame time, as a timezone-aware
                datetime such as ``datetime.now(UTC)``.
            timeout: Seconds to wait from the call, including any wait for
                ``not_before``, or ``None`` for `capture_timeout`. Pass
                ``math.inf`` to wait indefinitely.

        Raises:
            DeviceTimeout: No acceptable frame arrived in time.
            PreviewError: The camera is previewing.
        """
        if timeout is None:
            timeout = self.capture_timeout
        elif not timeout > 0:
            raise ValueError(f"timeout must be positive, not {timeout}")
        _check_not_before(not_before)
        self._require_not_previewing("capture_frame")
        return self._capture_frame(not_before, timeout)

    @abstractmethod
    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame: ...


class FileCamera(Camera):
    """A camera that delivers encoded files (e.g. DSLRs).

    The files are returned untouched; decoding raw files is up to the consumer
    (ADR 0002).
    """

    @synchronized
    def capture_files(self) -> list[CameraFile]:
        """Capture once and return every file the camera produced for it.

        Raises:
            PreviewError: The camera is previewing.
        """
        self._require_not_previewing("capture_files")
        return list(self._capture_files())

    @abstractmethod
    def _capture_files(self) -> list[CameraFile]: ...


def _check_not_before(not_before: datetime | None) -> None:
    if not_before is None:
        return
    if not isinstance(not_before, datetime):
        raise TypeError(
            "not_before must be a timezone-aware datetime, not "
            f"{type(not_before).__name__}; use datetime.now(UTC)"
        )
    if not_before.tzinfo is None:
        raise ValueError("not_before must be timezone-aware")
