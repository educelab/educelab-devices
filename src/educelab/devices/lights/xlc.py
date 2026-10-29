"""The XLC2 light controller for the pgs-scan gantry rigs, over USB serial.

Protocol, firmware history and flashing: docs/hardware/xlc.md.
"""

import logging
import re

from educelab.devices._serial import PortInfo, SerialTransport, handshake, list_ports
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import CommandRejected, DeviceError, DeviceNotFound
from educelab.devices.lights.base import RGB, Channel, Light

__all__ = ["XLC2"]

logger = logging.getLogger(__name__)

BAUD_RATE = 9600
# Opening the port resets the board (DTR), and pgs-scan waits 2 s before its
# handshake. See docs/hardware/xlc.md#transport.
RESET_SETTLE_S = 2.0
HANDSHAKE_TIMEOUT_S = 2.0
REPLY_TIMEOUT_S = 1.0

# Name in the handshake reply, compared ignoring case.
NAME = "EduceLab XLC2"
# 2.0.0 is the first firmware that acknowledges every command (ADR 0004).
MIN_FIRMWARE = (2, 0, 0)
# Both outputs take a duty from 0 to 255.
MAX_DUTY = 255

CHANNELS = (Channel("white"), Channel("940nm", wavelength_nm=940.0))
# The command letter that sets each channel's duty.
COMMANDS = {"white": "B", "940nm": "X"}


class XLC2(Light):
    """The XLC2 controller: a white LED strip and a 940 nm array.

    Channel ``"white"`` sets every pixel of the strip to the same level, and
    ``"940nm"`` drives the XL4 infrared array. Levels are PWM duties, so a
    level is a fraction of full drive, not of light output. The board can't
    report its outputs, so levels read back are the last ones set; opening
    and closing turn both off. Needs firmware 2.0.0 or newer.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        serial_number: USB serial number of the board's port, matched exactly
            but ignoring case. Found again on each `open()`, so it survives
            the port being renamed after a replug. Only useful for a board
            whose bridge has a unique one (docs/hardware/xlc.md).
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.

    Raises:
        ValueError: Both ``port`` and ``serial_number`` were given.
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        serial_number: str | None = None,
        transport: SerialTransport | None = None,
    ) -> None:
        if port is not None and serial_number is not None:
            raise ValueError("give an XLC2 a port or a serial number, not both")
        super().__init__()
        if transport is None and port is not None:
            transport = _default_transport(port)
        self._transport = transport
        self._port = None if transport is None else transport.port
        self._serial_number = serial_number
        self._firmware_version: str | None = None
        self._hardware_version: str | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="xlc",
            model="XLC2",
            address=self._port,
            serial_number=self._serial_number,
            firmware_version=self._firmware_version,
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """Find nothing: an XLC2 can't be recognized without opening its port.

        Its USB descriptors don't identify it, and opening a port to ask
        resets the board behind it (docs/hardware/xlc.md#identity).
        """
        return []

    @property
    def hardware_version(self) -> str | None:
        """The board's hardware version, as reported when it was opened."""
        return self._hardware_version

    @property
    def channels(self) -> tuple[Channel, ...]:
        return CHANNELS

    def __repr__(self) -> str:
        return f"<XLC2 {self._port or self._serial_number or '(no port)'}>"

    def _open(self) -> None:
        if self._serial_number is not None:
            # Found again on each open, in case a replug renamed the port.
            port = _find_port(self._serial_number)
            self._transport = _default_transport(port.device)
            self._port = port.device
            self._serial_number = port.serial_number
        if self._transport is None:
            raise DeviceNotFound(
                "XLC2 needs port= or serial_number=; it can't be found any other way"
            )
        transport = self._transport
        transport.open()
        try:
            identity = handshake(transport, "<?>", _parse_identity, HANDSHAKE_TIMEOUT_S)
            if identity is None:
                raise DeviceNotFound(
                    f"no XLC2 answered on {self._port!r}; check the port, "
                    "or that the board isn't held by another program"
                )
            hardware, firmware = identity
            if _version(firmware) < MIN_FIRMWARE:
                minimum = ".".join(map(str, MIN_FIRMWARE))
                raise DeviceError(
                    f"{self!r} has firmware {firmware}, but this driver needs "
                    f"{minimum} or newer. Reflash the board with the firmware in "
                    "firmware/xlc2/ of the educelab-devices repository; "
                    "docs/hardware/xlc.md#flashing has the steps."
                )
            self._hardware_version = hardware
            self._firmware_version = firmware
            self._all_off()
            for channel in CHANNELS:
                self._remember(channel, 0.0)
        except BaseException:
            transport.close()
            raise

    def _close(self) -> None:
        try:
            self._all_off()
        except DeviceError as exc:
            logger.warning("%r could not turn off before closing: %s", self, exc)
        finally:
            self._require_transport().close()

    def _set_level(self, channel: Channel, level: float) -> None:
        self._command(COMMANDS[channel.id], round(level * MAX_DUTY))

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise NotImplementedError("XLC2 channels are monochromatic")

    def _command(self, *fields: object) -> None:
        """Send one framed command and wait for its OK."""
        command = "<" + ",".join(map(str, fields)) + ">"
        transport = self._require_transport()
        transport.reset_input()
        transport.write_line(command)
        reply = transport.read_line()
        if reply == "OK":
            return
        if reply.startswith("ERR"):
            reason = reply[3:].strip() or "no reason given"
            raise CommandRejected(f"{self!r} rejected {command!r}: {reason}")
        raise DeviceError(f"{self!r} answered {command!r} with {reply!r}")

    def _require_transport(self) -> SerialTransport:
        assert self._transport is not None, "no transport before open()"
        return self._transport


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(
        port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S, reset_settle=RESET_SETTLE_S
    )


def _find_port(serial_number: str) -> PortInfo:
    wanted = serial_number.casefold()
    for port in list_ports():
        if port.serial_number and port.serial_number.casefold() == wanted:
            return port
    raise DeviceNotFound(f"no serial port has USB serial number {serial_number!r}")


def _parse_identity(line: str) -> tuple[str | None, str] | None:
    """(hardware, firmware) if ``line`` is an XLC2's handshake reply.

    Recognizes the pre-2.0.0 reply too, so old boards are refused as old
    rather than not found.
    """
    parts = [part.strip() for part in line.split(",")]
    if len(parts) == 3 and parts[0].casefold() == NAME.casefold():
        return parts[1], parts[2]
    old = re.fullmatch(rf"{NAME} \(FW ([^)]*)\)", line.strip(), re.IGNORECASE)
    return (None, old[1]) if old else None


def _version(text: str) -> tuple[int, ...]:
    """Parse ``"2.0.0"``; anything unparseable sorts below every release."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()
