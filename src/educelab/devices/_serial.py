"""Serial transport shared by the lab's serial drivers (MISHA, XLC, X-Carve).

Internal: drivers build on it, consumers don't. pyserial is imported only
when a port is listed or opened.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from educelab.devices.errors import import_optional

__all__ = ["PortInfo", "PortMatch", "find_ports", "list_ports"]


@dataclass(frozen=True)
class PortInfo:
    """A serial port as the host's USB stack describes it.

    Attributes:
        device: The name to open, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        vid: USB vendor ID, if the port is a USB device.
        pid: USB product ID, if the port is a USB device.
        serial_number: USB serial number, if the device reports one.
        description: The host's human-readable description of the port.
    """

    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str | None = None


def list_ports() -> list[PortInfo]:
    """Every serial port on the host.

    Raises:
        ImportError: pyserial isn't installed.
    """
    pyserial_list_ports = import_optional("serial.tools.list_ports", extra="serial")
    return [
        PortInfo(
            device=port.device,
            vid=port.vid,
            pid=port.pid,
            serial_number=port.serial_number,
            description=port.description,
        )
        for port in pyserial_list_ports.comports()
    ]


@dataclass(frozen=True)
class PortMatch:
    """Criteria that pick a device's port out of the host's serial ports.

    A port matches only if it meets every criterion given, and criteria that
    aren't given are ignored. With no criteria at all, nothing matches.

    Attributes:
        vid: USB vendor ID, or a collection of acceptable IDs.
        pid: USB product ID, or a collection of acceptable IDs.
        serial_number: Text the port's serial number must contain, ignoring
            case.
        description: Text the port's description must contain, ignoring case.
    """

    vid: int | Iterable[int] | None = None
    pid: int | Iterable[int] | None = None
    serial_number: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        for name in ("vid", "pid"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, int):
                object.__setattr__(self, name, frozenset(value))

    def matches(self, port: PortInfo) -> bool:
        """Whether ``port`` meets every criterion given."""
        checks = [
            _id_matches(self.vid, port.vid),
            _id_matches(self.pid, port.pid),
            _text_matches(self.serial_number, port.serial_number),
            _text_matches(self.description, port.description),
        ]
        given = [check for check in checks if check is not None]
        return bool(given) and all(given)

    def matching(self, ports: Iterable[PortInfo]) -> list[PortInfo]:
        """The ports that match, in the order given."""
        return [port for port in ports if self.matches(port)]


def find_ports(match: PortMatch) -> list[PortInfo]:
    """The host's serial ports that meet ``match``.

    Raises:
        ImportError: pyserial isn't installed.
    """
    return match.matching(list_ports())


# Each returns None when the criterion wasn't given.
def _id_matches(want: int | Iterable[int] | None, have: int | None) -> bool | None:
    if want is None:
        return None
    if have is None:
        return False
    return have == want if isinstance(want, int) else have in want


def _text_matches(want: str | None, have: str | None) -> bool | None:
    if want is None:
        return None
    return have is not None and want.casefold() in have.casefold()
