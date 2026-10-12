"""A USB hub with no hardware behind it."""

import logging

from educelab.devices.device import DeviceInfo
from educelab.devices.hubs.base import Hub

__all__ = ["SimulatedHub"]

logger = logging.getLogger(__name__)


class SimulatedHub(Hub):
    """A hub that records which ports are powered. Every port starts powered.

    Args:
        ports: The port numbers to offer.
    """

    def __init__(self, ports: tuple[int, ...] = (0, 1, 2, 3)) -> None:
        super().__init__()
        self._ports = tuple(ports)
        self._powered = dict.fromkeys(self._ports, True)

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="simulated", model="SimulatedHub")

    @property
    def ports(self) -> tuple[int, ...]:
        return self._ports

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _set_power(self, port: int, on: bool) -> None:
        logger.debug("port %d %s", port, "on" if on else "off")
        self._powered[port] = on

    def _is_powered(self, port: int) -> bool:
        return self._powered[port]
