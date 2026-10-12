"""Hubs: devices that switch power to USB ports."""

from educelab.devices.hubs.base import Hub
from educelab.devices.hubs.simulated import SimulatedHub

__all__ = ["Hub", "SimulatedHub"]
