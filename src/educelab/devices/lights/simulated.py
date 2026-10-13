"""A light with no hardware behind it."""

import logging

from educelab.devices.device import DeviceInfo
from educelab.devices.lights.base import RGB, Channel, ChannelKind, Light

__all__ = ["SimulatedLight"]

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS = (
    Channel("white"),
    Channel("uv", wavelength_nm=365.0),
    Channel("rgb", ChannelKind.RGB),
)


class SimulatedLight(Light):
    """A light that remembers each channel's level, starting with all off.

    Args:
        channels: The channels to offer. Defaults to a white, a 365 nm, and
            an RGB channel.
    """

    def __init__(self, channels: tuple[Channel, ...] = DEFAULT_CHANNELS) -> None:
        super().__init__()
        self._channels = tuple(channels)
        self._levels: dict[str, float | RGB] = {}
        self._turn_off()

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="simulated", model="SimulatedLight")

    @property
    def channels(self) -> tuple[Channel, ...]:
        return self._channels

    def _open(self) -> None:
        self._turn_off()

    def _close(self) -> None:
        pass

    def _set_level(self, channel: Channel, level: float) -> None:
        logger.debug("%s level %g", channel.id, level)
        self._levels[channel.id] = level

    def _set_color(self, channel: Channel, color: RGB) -> None:
        logger.debug("%s color %s", channel.id, color)
        self._levels[channel.id] = color

    def _get_level(self, channel: Channel) -> float | None:
        value = self._levels[channel.id]
        return value if isinstance(value, float) else None

    def _get_color(self, channel: Channel) -> RGB | None:
        value = self._levels[channel.id]
        return value if isinstance(value, tuple) else None

    def _turn_off(self) -> None:
        for channel in self._channels:
            off: float | RGB = 0.0 if channel.kind is ChannelKind.MONO else (0.0,) * 3
            self._levels[channel.id] = off
