"""The light role and its channels."""

import math
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
    `channels` and implement `_set_level()` and `_set_color()`.
    """

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
        if channel.kind is ChannelKind.RGB:
            self._set_color(channel, (level, level, level))
        else:
            self._set_level(channel, level)

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
        self._set_color(channel, (color[0], color[1], color[2]))

    @synchronized
    def all_off(self) -> None:
        """Turn every channel off."""
        self._all_off()

    def _channel(self, channel_id: str) -> Channel:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise KeyError(f"{self!r} has no channel {channel_id!r}")

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
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number, not {type(value).__name__}")
    if math.isnan(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0, not {value}")
