"""Serial transport shared by the lab's serial drivers (MISHA, XLC, X-Carve).

Internal: drivers build on it, consumers don't. pyserial is imported only
when a port is listed or opened.
"""

from dataclasses import dataclass

from educelab.devices.errors import import_optional

__all__ = ["PortInfo", "list_ports"]


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
