"""The hub role."""

import time
from abc import abstractmethod

from educelab.devices.device import Device, synchronized

__all__ = ["Hub"]


class Hub(Device):
    """A device that switches power to a set of USB ports.

    Drivers declare `ports` and implement `_set_power()` and `_is_powered()`.
    """

    power_cycle_off_time: float = 1.0
    """Default seconds a port stays off during `power_cycle()`. A convention,
    not a measured hardware requirement; raise it for slow-draining devices."""

    @property
    @abstractmethod
    def ports(self) -> tuple[int, ...]:
        """The hub's switchable port numbers."""

    @synchronized
    def power_on(self, port: int) -> None:
        """Turn on power to ``port``.

        Raises:
            KeyError: The hub has no such port.
        """
        self._set_power(self._port(port), True)

    @synchronized
    def power_off(self, port: int) -> None:
        """Turn off power to ``port``.

        Raises:
            KeyError: The hub has no such port.
        """
        self._set_power(self._port(port), False)

    @synchronized
    def is_powered(self, port: int) -> bool:
        """Whether ``port`` is powered.

        Raises:
            KeyError: The hub has no such port.
        """
        return self._is_powered(self._port(port))

    @synchronized
    def power_cycle(self, port: int, off_time: float | None = None) -> None:
        """Turn ``port`` off, wait, and turn it back on, to reset what's attached.

        Args:
            port: Port number.
            off_time: Seconds to leave the port off, or ``None`` for
                `power_cycle_off_time`.

        Raises:
            KeyError: The hub has no such port.
            ValueError: ``off_time`` is negative.
        """
        port = self._port(port)
        if off_time is None:
            off_time = self.power_cycle_off_time
        if off_time < 0:
            raise ValueError(f"off_time can't be negative, not {off_time}")
        self._set_power(port, False)
        time.sleep(off_time)
        self._set_power(port, True)

    def _port(self, port: int) -> int:
        if port not in self.ports or isinstance(port, bool):
            raise KeyError(f"{self!r} has no port {port!r}")
        return port

    @abstractmethod
    def _set_power(self, port: int, on: bool) -> None: ...

    @abstractmethod
    def _is_powered(self, port: int) -> bool: ...
