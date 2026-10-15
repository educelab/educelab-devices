"""The camera roles and their value types."""

import os
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

import numpy as np
import numpy.typing as npt

from educelab.devices.device import Device, synchronized
from educelab.devices.settings import Rounding, Setting, SettingValue

__all__ = [
    "APERTURE",
    "CANONICAL_UNITS",
    "EXPOSURE_TIME",
    "GAIN",
    "ISO",
    "PIXEL_FORMAT",
    "Camera",
    "CameraFile",
    "FileCamera",
    "Frame",
    "FrameCamera",
    "Rounding",
    "Setting",
    "SettingValue",
]


# Canonical setting names mean the same thing on every camera.
EXPOSURE_TIME = "exposure_time"
GAIN = "gain"
ISO = "iso"
APERTURE = "aperture"
PIXEL_FORMAT = "pixel_format"

CANONICAL_UNITS: Mapping[str, str | None] = {
    EXPOSURE_TIME: "s",
    GAIN: "dB",
    ISO: None,
    APERTURE: "f-number",
    PIXEL_FORMAT: None,
}
"""Unit of each canonical setting. Settings not listed use the camera's own name."""


@dataclass(frozen=True, eq=False)
class Frame:
    """A decoded pixel array from a frame camera.

    Attributes:
        pixels: Image data, shaped ``(height, width)`` or
            ``(height, width, channels)``.
        pixel_format: Layout of ``pixels``, e.g. ``"Mono8"`` or ``"RGB8"``.
        timestamp: When the frame was captured (timezone-aware).
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
    Drivers implement `_settings()`, `_set_setting()`, and `_preview_frame()`.
    """

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

        Raises:
            KeyError: The camera has no such setting.
            TypeError: ``value`` has the wrong type.
            ValueError: The setting is read-only or ``value`` is out of range.
        """
        self._set_setting(name, self._setting(name).validate(value))

    @synchronized
    def preview_frame(self) -> npt.NDArray[np.uint8]:
        """A low-resolution image for framing and focusing, not for capture."""
        return self._preview_frame()

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
    def _preview_frame(self) -> npt.NDArray[np.uint8]: ...


class FrameCamera(Camera):
    """A camera that delivers decoded pixel arrays (e.g. machine-vision cameras)."""

    @synchronized
    def capture_frame(
        self,
        *,
        not_before: datetime | float | None = None,
        timeout: float | None = None,
    ) -> Frame:
        """Capture one frame with the current settings.

        Free-running cameras may already hold a frame exposed before the call.
        Pass ``not_before`` (e.g. the moment the scene was ready) to get a frame
        whose ``timestamp`` is no earlier than that.

        Args:
            not_before: Earliest acceptable frame time: a timezone-aware
                datetime, or seconds since the epoch.
            timeout: Seconds to wait for a frame, or ``None`` for the camera's
                default.

        Raises:
            DeviceTimeout: No acceptable frame arrived in time.
        """
        if isinstance(not_before, datetime):
            if not_before.tzinfo is None:
                raise ValueError("not_before must be timezone-aware")
        elif not_before is not None:
            not_before = datetime.fromtimestamp(not_before, UTC)
        return self._capture_frame(not_before, timeout)

    @abstractmethod
    def _capture_frame(
        self, not_before: datetime | None, timeout: float | None
    ) -> Frame: ...


class FileCamera(Camera):
    """A camera that delivers encoded files (e.g. DSLRs).

    The files are returned untouched; decoding raw files is up to the consumer
    (ADR 0002).
    """

    @synchronized
    def capture_files(self) -> list[CameraFile]:
        """Capture once and return every file the camera produced for it."""
        return list(self._capture_files())

    @abstractmethod
    def _capture_files(self) -> list[CameraFile]: ...
