"""The `Device` base class shared by every role and driver."""

import functools
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Concatenate, ParamSpec, Protocol, Self, TypeVar

from educelab.devices.errors import NotConnected

__all__ = [
    "Device",
    "DeviceEvent",
    "DeviceInfo",
    "Disconnected",
    "Listener",
    "synchronized",
]

logger = logging.getLogger(__name__)

D = TypeVar("D", bound="Device")
P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True)
class DeviceInfo:
    """Identifies one device, as found by `Device.discover()` or reported by `info`.

    Attributes:
        backend: Short name of the driver, e.g. ``"misha"`` or ``"simulated"``.
        model: Hardware model, e.g. ``"Foldio360"``.
        address: Where the device is attached: a serial port, BLE address, or
            USB path. ``None`` when not applicable.
        serial_number: Hardware serial number, if the device reports one.
        firmware_version: Firmware version, if known. Usually only available
            once the device is open.
        vendor: Manufacturer, as the device names itself (e.g. ``"FLIR"``).
            Every camera reports one once open, if the hardware can say.
    """

    backend: str
    model: str
    address: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    vendor: str | None = None


@dataclass(frozen=True, eq=False)
class DeviceEvent:
    """Something a device reports on its own, outside any method call.

    Roles and drivers subclass this for specific events; listeners can
    dispatch on the type.
    """

    device: "Device"


@dataclass(frozen=True, eq=False)
class Disconnected(DeviceEvent):
    """The device's connection was lost without `close()` being called."""

    reason: str


class Listener(Protocol):
    """Callback that receives a device's events."""

    def __call__(self, event: DeviceEvent, /) -> None: ...


class Device(ABC):
    """Base class for every device.

    A device owns its connection. The constructor only stores configuration;
    `open()` connects and `close()` disconnects, and both are safe to call
    twice. Devices are also context managers.

    Public methods are blocking and thread-safe: each takes the device's
    reentrant lock, so calls from different threads run one at a time.

    Events the device produces on its own (e.g. a lost connection) go to
    listeners registered with `add_listener()`. Listeners may be called from a
    driver's internal thread, so they should return quickly.

    Drivers implement `_open()`, `_close()`, and `info`, plus the hooks of the
    roles they offer.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._is_open = False
        # Separate from _lock so events can be delivered while a call is running.
        self._listeners_lock = threading.Lock()
        self._listeners: list[Listener] = []

    @property
    @abstractmethod
    def info(self) -> DeviceInfo:
        """Identity of this device."""

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """Find devices of this type attached to the host, without opening them.

        Drivers that can enumerate their hardware override this. The default
        finds nothing.
        """
        return []

    @property
    def is_open(self) -> bool:
        """Whether the device is connected."""
        return self._is_open

    def open(self) -> None:
        """Connect to the device. Does nothing if already open.

        Raises:
            DeviceNotFound: Nothing answered at the configured address.
            DeviceBusy: Another process holds the device.
        """
        with self._lock:
            if self._is_open:
                return
            self._open()
            self._is_open = True

    def close(self) -> None:
        """Disconnect from the device. Does nothing if already closed.

        The device counts as closed afterwards even if disconnecting fails.
        """
        with self._lock:
            if not self._is_open:
                return
            try:
                self._close()
            finally:
                self._is_open = False
                self._reset_state()

    def add_listener(self, listener: Listener) -> None:
        """Call ``listener`` with each event this device reports.

        Adding the same listener twice has no effect.
        """
        with self._listeners_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        """Stop calling ``listener``. Does nothing if it was never added."""
        with self._listeners_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    @abstractmethod
    def _open(self) -> None:
        """Connect to the hardware."""

    @abstractmethod
    def _close(self) -> None:
        """Release the hardware connection."""

    def _reset_state(self) -> None:
        """Forget state that a new connection can't trust (e.g. homing).

        Roles that track such state override this and call ``super()``.
        """
        return

    def _emit(self, event: DeviceEvent) -> None:
        """Deliver ``event`` to every listener, logging any listener's exception."""
        with self._listeners_lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("%r listener %r failed on %r", self, listener, event)

    def _require_open(self) -> None:
        if not self._is_open:
            raise NotConnected(f"{self!r} is not open; call open() first")


def synchronized(
    method: Callable[Concatenate[D, P], R],
) -> Callable[Concatenate[D, P], R]:
    """Run a device method under the device lock, after checking it is open."""

    @functools.wraps(method)
    def wrapper(self: D, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            self._require_open()
            return method(self, *args, **kwargs)

    return wrapper
