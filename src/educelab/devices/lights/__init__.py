"""Lights: devices that emit illumination through channels."""

from educelab.devices.lights.base import (
    RGB,
    Channel,
    ChannelConcurrency,
    ChannelKind,
    Light,
)
from educelab.devices.lights.simulated import SimulatedLight

__all__ = [
    "RGB",
    "Channel",
    "ChannelConcurrency",
    "ChannelKind",
    "Light",
    "SimulatedLight",
]
