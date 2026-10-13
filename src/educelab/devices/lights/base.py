"""The light role and its channels."""

import math
import numbers
from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from educelab.devices.device import Device, synchronized

__all__ = ["RGB", "Channel", "ChannelKind", "Light"]

RGB = tuple[float, float, float]


class ChannelKind(StrEnum):
    MONO = "mono"
    RGB = "rgb"


@dataclass(frozen=True)
class Channel:
    """One independently addressable emitter of a light.

    Attributes:
        id: The channel's ID, unique within its light.
        kind: Monochromatic or RGB.
        wavelength_nm: Peak wavelength in nanometres, for monochromatic
            channels where it is known.
    """

    id: str
    kind: ChannelKind = ChannelKind.MONO
    wavelength_nm: float | None = None


class Light(Device):
    """A device that emits illumination through one or more channels.

    Levels are fractions from 0.0 (off) to 1.0 (full). Drivers declare
    `channels` and implement `_set_level()` and `_set_color()`. Drivers whose
    hardware reports its levels also override `_get_level()` and `_get_color()`.
    """

    _remembered: dict[str, float | RGB] | None = None

    @property
    @abstractmethod
    def channels(self) -> tuple[Channel, ...]:
        """The light's channels, in the driver's natural order."""

    @synchronized
    def set_level(self, channel_id: str, level: float) -> None:
        """Set a channel's level.

        On an RGB channel this sets all three components to ``level``.

        Raises:
            KeyError: No channel has this ID.
            ValueError: ``level`` is outside 0.0–1.0.
        """
        channel = self._channel(channel_id)
        _check_level(level, "level")
        level = float(level)
        if channel.kind is ChannelKind.RGB:
            self._set_color(channel, (level, level, level))
            self._remember(channel, (level, level, level))
        else:
            self._set_level(channel, level)
            self._remember(channel, level)

    @synchronized
    def set_color(self, channel_id: str, color: RGB) -> None:
        """Set an RGB channel's red, green, and blue levels.

        Raises:
            KeyError: No channel has this ID.
            ValueError: The channel isn't RGB, or a component is outside
                0.0–1.0.
        """
        channel = self._channel(channel_id)
        if channel.kind is not ChannelKind.RGB:
            raise ValueError(f"channel {channel_id!r} of {self!r} is not RGB")
        if len(color) != 3:
            raise ValueError(f"color needs three components, not {len(color)}")
        for name, value in zip("rgb", color, strict=True):
            _check_level(value, name)
        rgb = (float(color[0]), float(color[1]), float(color[2]))
        self._set_color(channel, rgb)
        self._remember(channel, rgb)

    @synchronized
    def all_off(self) -> None:
        """Turn every channel off."""
        self._all_off()
        for channel in self.channels:
            off = (0.0, 0.0, 0.0) if channel.kind is ChannelKind.RGB else 0.0
            self._remember(channel, off)

    @synchronized
    def level(self, channel_id: str) -> float | None:
        """A monochromatic channel's level.

        Unless the hardware reports it, this is the last level set since
        `open()`, or ``None`` if none has been.

        Raises:
            KeyError: No channel has this ID.
            ValueError: The channel is RGB; use `color()`.
        """
        channel = self._channel(channel_id)
        if channel.kind is ChannelKind.RGB:
            raise ValueError(f"channel {channel_id!r} of {self!r} is RGB; use color()")
        return self._get_level(channel)

    @synchronized
    def color(self, channel_id: str) -> RGB | None:
        """An RGB channel's color.

        Unless the hardware reports it, this is the last color set since
        `open()`, or ``None`` if none has been.

        Raises:
            KeyError: No channel has this ID.
            ValueError: The channel isn't RGB; use `level()`.
        """
        channel = self._channel(channel_id)
        if channel.kind is not ChannelKind.RGB:
            raise ValueError(
                f"channel {channel_id!r} of {self!r} is not RGB; use level()"
            )
        return self._get_color(channel)

    def _channel(self, channel_id: str) -> Channel:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise KeyError(f"{self!r} has no channel {channel_id!r}")

    def _remember(self, channel: Channel, value: float | RGB) -> None:
        if self._remembered is None:
            self._remembered = {}
        self._remembered[channel.id] = value

    def _reset_state(self) -> None:
        super()._reset_state()
        self._remembered = None

    def _get_level(self, channel: Channel) -> float | None:
        """Read a monochromatic channel's level. Defaults to the last one set."""
        value = (self._remembered or {}).get(channel.id)
        return value if isinstance(value, float | int) else None

    def _get_color(self, channel: Channel) -> RGB | None:
        """Read an RGB channel's color. Defaults to the last one set."""
        value = (self._remembered or {}).get(channel.id)
        return value if isinstance(value, tuple) else None

    @abstractmethod
    def _set_level(self, channel: Channel, level: float) -> None:
        """Set a monochromatic channel's level."""

    @abstractmethod
    def _set_color(self, channel: Channel, color: RGB) -> None:
        """Set an RGB channel's color."""

    def _all_off(self) -> None:
        """Turn every channel off. Override when the hardware has one command."""
        for channel in self.channels:
            if channel.kind is ChannelKind.RGB:
                self._set_color(channel, (0.0, 0.0, 0.0))
            else:
                self._set_level(channel, 0.0)


def _check_level(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a number, not {type(value).__name__}")
    if math.isnan(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0, not {value}")
