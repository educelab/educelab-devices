"""Serial transport shared by the lab's serial drivers (MISHA, XLC, X-Carve).

Internal: drivers build on it, consumers don't. pyserial is imported only
when a port is listed or opened.
"""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    import_optional,
)

__all__ = [
    "PortInfo",
    "PortMatch",
    "SerialTransport",
    "command",
    "find_ports",
    "handshake",
    "list_ports",
    "only_port",
    "parse_version",
    "port_with_serial_number",
    "wait_for",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Opens a port and returns an object with the parts of serial.Serial's API the
# transport uses. Tests pass one that returns a fake.
Opener = Callable[[str, int, float], Any]


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


def port_with_serial_number(serial_number: str) -> PortInfo:
    """The port whose USB serial number is ``serial_number``, ignoring case.

    Raises:
        DeviceNotFound: No port has it.
        ImportError: pyserial isn't installed.
    """
    wanted = serial_number.casefold()
    for port in list_ports():
        if port.serial_number and port.serial_number.casefold() == wanted:
            return port
    raise DeviceNotFound(f"no serial port has USB serial number {serial_number!r}")


def only_port(match: PortMatch, *, name: str, plural: str, hint: str) -> PortInfo:
    """The one port that meets ``match``.

    Args:
        name: What a matching device is called, for errors, e.g. "X-Controller".
        plural: The same, for several.
        hint: What to do when none is attached.

    Raises:
        DeviceNotFound: None matches.
        DeviceError: Several match.
        ImportError: pyserial isn't installed.
    """
    found = find_ports(match)
    if not found:
        raise DeviceNotFound(f"no {name} is attached. {hint}")
    if len(found) > 1:
        choices = ", ".join(f"{p.serial_number} at {p.device}" for p in found)
        raise DeviceError(
            f"several {plural} are attached ({choices}); choose one with serial_number="
        )
    return found[0]


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


class SerialTransport:
    """A line-oriented connection to one serial port.

    Lines are ASCII and end with ``\\n`` on the way out; on the way in, the
    line ending and surrounding whitespace are stripped and blank lines are
    skipped.

    Args:
        port: The port to open, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        baudrate: Baud rate.
        timeout: Default time to wait for a line, in seconds.
        reset_settle: Time to wait after opening, in seconds. Boards that
            reset when the port opens (DTR) ignore commands until they've
            booted; output from the boot is discarded.
        opener: Opens the port. Defaults to pyserial; tests pass a fake.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int,
        timeout: float = 1.0,
        reset_settle: float = 0.0,
        opener: Opener | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.reset_settle = reset_settle
        self._opener = opener or _open_pyserial
        self._conn: Any = None
        self._partial = b""

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> None:
        """Open the port and wait for the board to finish resetting.

        Raises:
            DeviceNotFound: The port couldn't be opened.
            ImportError: The default opener needs pyserial, which isn't
                installed.
        """
        if self._conn is not None:
            return
        try:
            conn = self._opener(self.port, self.baudrate, self.timeout)
        except OSError as exc:
            raise DeviceNotFound(
                f"could not open serial port {self.port!r}: {exc}"
            ) from exc
        self._conn = conn
        self._partial = b""
        if self.reset_settle:
            time.sleep(self.reset_settle)
        self.reset_input()

    def close(self) -> None:
        """Close the port. Does nothing if it isn't open."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except OSError as exc:
            # The port is gone either way; there's nothing left to release.
            logger.debug("closing %s failed: %s", self.port, exc)

    def reset_input(self) -> None:
        """Discard anything received but not yet read."""
        conn = self._require_open()
        self._partial = b""
        try:
            conn.reset_input_buffer()
        except OSError as exc:
            raise self._lost(exc) from exc

    def write_line(self, line: str) -> None:
        """Send ``line`` followed by a newline.

        Raises:
            NotConnected: The port isn't open or the connection was lost.
        """
        conn = self._require_open()
        logger.debug("%s > %s", self.port, line)
        try:
            conn.write(f"{line}\n".encode("ascii"))
            conn.flush()
        except OSError as exc:
            raise self._lost(exc) from exc

    def write_bytes(self, data: bytes) -> None:
        """Send ``data`` as it is, with no line ending.

        For commands a device acts on byte by byte, such as GRBL's real-time
        commands. Safe to call while another thread is in `read_line()`.

        Raises:
            NotConnected: The port isn't open or the connection was lost.
        """
        conn = self._require_open()
        logger.debug("%s > %r", self.port, data)
        try:
            conn.write(data)
            conn.flush()
        except OSError as exc:
            raise self._lost(exc) from exc

    def read_line(self, timeout: float | None = None) -> str:
        """Wait for the next non-blank line.

        Args:
            timeout: Seconds to wait. Defaults to the transport's timeout.

        Raises:
            DeviceTimeout: No complete line arrived in time.
            NotConnected: The port isn't open or the connection was lost.
        """
        conn = self._require_open()
        limit = max(0.0, self.timeout if timeout is None else timeout)
        deadline = time.monotonic() + limit
        while limit > 0:
            # pyserial reconfigures the port whenever its timeout changes, so
            # it changes only when a partial line leaves less time to wait.
            if conn.timeout != limit:
                conn.timeout = limit
            try:
                chunk = conn.readline()
            except OSError as exc:
                raise self._lost(exc) from exc
            # readline() returns what it has so far when it times out.
            self._partial += chunk
            if self._partial.endswith(b"\n"):
                line = self._partial.decode("ascii", errors="replace").strip()
                self._partial = b""
                if line:
                    logger.debug("%s < %s", self.port, line)
                    return line
            limit = deadline - time.monotonic()
        raise DeviceTimeout(f"no reply from serial port {self.port!r}")

    def _require_open(self) -> Any:
        if self._conn is None:
            raise NotConnected(f"serial port {self.port!r} is not open")
        return self._conn

    def _lost(self, exc: OSError) -> NotConnected:
        self.close()
        return NotConnected(f"lost serial port {self.port!r}: {exc}")


def handshake(
    transport: SerialTransport,
    command: str,
    parse: Callable[[str], T | None],
    timeout: float | None = None,
) -> T | None:
    """Send an identification command and wait for a reply that parses.

    Lines that ``parse`` rejects (boot chatter, another device's output) are
    skipped until the timeout.

    Args:
        transport: An open transport.
        command: The identification command, e.g. ``"ID"``.
        parse: Returns the parsed reply, or ``None`` for a line that isn't one.
        timeout: Seconds to wait. Defaults to the transport's timeout.

    Returns:
        The first parsed reply, or ``None`` if none arrived in time.
    """
    transport.reset_input()
    transport.write_line(command)
    return wait_for(transport, parse, timeout)


def wait_for(
    transport: SerialTransport,
    parse: Callable[[str], T | None],
    timeout: float | None = None,
) -> T | None:
    """Wait for a line that ``parse`` accepts, skipping any others.

    Returns:
        The first parsed line, or ``None`` if none arrived in time.
    """
    deadline = time.monotonic() + (transport.timeout if timeout is None else timeout)
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = transport.read_line(remaining)
        except DeviceTimeout:
            return None
        if (reply := parse(line)) is not None:
            return reply
    return None


def command(transport: SerialTransport, line: str, device: object) -> str:
    """Send a command to lab firmware and wait for its ``OK`` (ADR 0004).

    Args:
        device: The device, for error messages.

    Returns:
        Anything after ``OK`` in the reply, e.g. a queried value.

    Raises:
        CommandRejected: The firmware answered ``ERR``.
        DeviceError: The reply was neither.
        DeviceTimeout: No reply in time.
    """
    transport.reset_input()
    transport.write_line(line)
    reply = transport.read_line()
    if reply == "OK" or reply.startswith("OK "):
        return reply[3:].strip()
    if reply.startswith("ERR"):
        reason = reply[3:].strip() or "no reason given"
        raise CommandRejected(f"{device!r} rejected {line!r}: {reason}")
    raise DeviceError(f"{device!r} answered {line!r} with {reply!r}")


def parse_version(text: str) -> tuple[int, ...]:
    """Parse ``"2.0.0"``; anything unparseable sorts below every release."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()


def _open_pyserial(port: str, baudrate: int, timeout: float) -> Any:
    pyserial = import_optional("serial", extra="serial")
    return pyserial.Serial(port, baudrate, timeout=timeout, write_timeout=timeout)
