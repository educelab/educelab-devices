"""XLC1 and XLC2 light controllers for the pgs-scan gantry rigs, over USB serial.

Protocol, firmware history and flashing: docs/hardware/xlc.md.
"""

import logging
import re
from typing import ClassVar

from educelab.devices._serial import SerialTransport, handshake
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import CommandRejected, DeviceError, DeviceNotFound
from educelab.devices.lights.base import RGB, Channel, ChannelKind, Light

__all__ = ["XLC1", "XLC2"]

logger = logging.getLogger(__name__)

BAUD_RATE = 9600
# Opening the port resets the board (DTR), and pgs-scan waits 2 s before its
# handshake. See docs/hardware/xlc.md#transport.
RESET_SETTLE_S = 2.0
HANDSHAKE_TIMEOUT_S = 2.0
REPLY_TIMEOUT_S = 1.0

# 2.0.0 is the first firmware that acknowledges every command (ADR 0004).
MIN_FIRMWARE = (2, 0, 0)
# Every XLC output takes a duty from 0 to 255.
MAX_DUTY = 255


class _XLCLight(Light):
    """What XLC1 and XLC2 share: framing, handshake, and acknowledgements.

    Neither board can report its outputs, so levels read back are the last
    ones set. Opening turns everything off, so they're known from the start.
    """

    MODEL: ClassVar[str]

    def __init__(
        self,
        port: str | None = None,
        *,
        transport: SerialTransport | None = None,
    ) -> None:
        super().__init__()
        if transport is None and port is not None:
            transport = _default_transport(port)
        self._transport = transport
        self._port = None if transport is None else transport.port
        self._firmware_version: str | None = None
        self._hardware_version: str | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="xlc",
            model=self.MODEL,
            address=self._port,
            firmware_version=self._firmware_version,
        )

    @property
    def hardware_version(self) -> str | None:
        """The board's hardware version, as reported when it was opened."""
        return self._hardware_version

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._port or '(no port)'}>"

    def _open(self) -> None:
        if self._transport is None:
            raise DeviceNotFound(f"give {type(self).__name__} a port")
        transport = self._transport
        transport.open()
        try:
            identity = handshake(
                transport, "<?>", self._parse_identity, HANDSHAKE_TIMEOUT_S
            )
            if identity is None:
                raise DeviceNotFound(
                    f"no {self.MODEL} answered on {self._port!r}; check the port, "
                    "or that the board isn't held by another program"
                )
            hardware, firmware = identity
            if _version(firmware) < MIN_FIRMWARE:
                minimum = ".".join(map(str, MIN_FIRMWARE))
                raise DeviceError(
                    f"{self!r} has firmware {firmware}, but this driver needs "
                    f"{minimum} or newer. Reflash the board with the firmware in "
                    f"firmware/{self.MODEL.lower()}/ of the educelab-devices "
                    "repository; docs/hardware/xlc.md#flashing has the steps."
                )
            self._hardware_version = hardware
            self._firmware_version = firmware
            self._all_off()
            for channel in self.channels:
                off = (0.0, 0.0, 0.0) if channel.kind is ChannelKind.RGB else 0.0
                self._remember(channel, off)
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

    def _parse_identity(self, line: str) -> tuple[str | None, str] | None:
        """(hardware, firmware) if ``line`` is this model's handshake reply.

        Recognizes the pre-2.0.0 reply too, so old boards are refused as old
        rather than not found.
        """
        name = f"EduceLab {self.MODEL}"
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 3 and parts[0].casefold() == name.casefold():
            return parts[1], parts[2]
        old = re.fullmatch(rf"{name} \(FW ([^)]*)\)", line.strip(), re.IGNORECASE)
        return (None, old[1]) if old else None

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


class XLC1(_XLCLight):
    """The XLC1 controller: one RGB LED strip, channel ``"rgb"``.

    Levels are PWM duties, so a level is a fraction of full drive, not of
    light output. Opening and closing turn the strip off. Needs firmware
    2.0.0 or newer.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.
    """

    MODEL = "XLC1"
    CHANNELS = (Channel("rgb", ChannelKind.RGB),)

    @property
    def channels(self) -> tuple[Channel, ...]:
        return self.CHANNELS

    def _set_level(self, channel: Channel, level: float) -> None:
        raise NotImplementedError("the XLC1 channel is RGB")

    def _set_color(self, channel: Channel, color: RGB) -> None:
        self._command("C", *(_duty(value) for value in color))

    def _all_off(self) -> None:
        self._command("C", 0, 0, 0)


class XLC2(_XLCLight):
    """The XLC2 controller: a white LED strip and a 940 nm array.

    Channel ``"white"`` sets every pixel of the strip to the same level, and
    ``"940nm"`` drives the XL4 infrared array. Levels are PWM duties, so a
    level is a fraction of full drive, not of light output. Opening and
    closing turn both off. Needs firmware 2.0.0 or newer.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.
    """

    MODEL = "XLC2"
    CHANNELS = (Channel("white"), Channel("940nm", wavelength_nm=940.0))
    # The command letter that sets each channel's duty.
    COMMANDS: ClassVar[dict[str, str]] = {"white": "B", "940nm": "X"}

    @property
    def channels(self) -> tuple[Channel, ...]:
        return self.CHANNELS

    def _set_level(self, channel: Channel, level: float) -> None:
        self._command(self.COMMANDS[channel.id], _duty(level))

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise NotImplementedError("XLC2 channels are monochromatic")


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(
        port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S, reset_settle=RESET_SETTLE_S
    )


def _duty(level: float) -> int:
    return round(level * MAX_DUTY)


def _version(text: str) -> tuple[int, ...]:
    """Parse ``"2.0.0"``; anything unparseable sorts below every release."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()
