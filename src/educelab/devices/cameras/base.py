"""The camera roles and their value types."""

import math
import os
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

import numpy as np
import numpy.typing as npt

from educelab.devices.device import Device, synchronized

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
    "Setting",
    "SettingValue",
]

SettingValue = bool | int | float | str

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


@dataclass(frozen=True)
class Setting:
    """A camera setting: its current value and what it accepts.

    A setting accepts either one of ``choices`` or a value of the same type as
    ``value``, within ``minimum`` and ``maximum`` when those are set.
    """

    name: str
    value: SettingValue
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[SettingValue, ...] | None = None
    unit: str | None = None
    read_only: bool = False

    def validate(self, value: SettingValue) -> None:
        """Check that ``value`` could be written to this setting.

        Raises:
            TypeError: ``value`` has the wrong type.
            ValueError: The setting is read-only, or ``value`` is out of range
                or not one of the choices.
        """
        if self.read_only:
            raise ValueError(f"setting {self.name!r} is read-only")
        if self.choices is not None:
            if value not in self.choices:
                raise ValueError(
                    f"{value!r} is not a choice for {self.name!r}: {self.choices}"
                )
            return
        if not _same_kind(value, self.value):
            raise TypeError(
                f"setting {self.name!r} takes {type(self.value).__name__}, "
                f"not {type(value).__name__}"
            )
        if isinstance(value, bool | str):
            return
        if math.isnan(value):
            raise ValueError(f"setting {self.name!r} can't be NaN")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}={value} is below {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name}={value} is above {self.maximum}")


def _same_kind(value: SettingValue, current: SettingValue) -> bool:
    if isinstance(current, bool) or isinstance(value, bool):
        return isinstance(current, bool) and isinstance(value, bool)
    if isinstance(current, float):
        return isinstance(value, int | float)
    return type(value) is type(current)


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
        self._setting(name).validate(value)
        self._set_setting(name, value)

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
    def capture_frame(self) -> Frame:
        """Capture one frame with the current settings."""
        return self._capture_frame()

    @abstractmethod
    def _capture_frame(self) -> Frame: ...


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
