"""MISHA 16-band LED panels, driven over USB serial.

Protocol, timing, and firmware history: docs/hardware/misha.md.
"""

import logging

from educelab.devices._serial import SerialTransport, handshake
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import CommandRejected, DeviceError, DeviceNotFound
from educelab.devices.lights.base import RGB, Channel, Light

__all__ = ["MishaLight"]

logger = logging.getLogger(__name__)

BAUD_RATE = 9600
# Opening the port resets the board (DTR); it ignores commands while it boots.
# See docs/hardware/misha.md#timing.
RESET_SETTLE_S = 2.0
HANDSHAKE_TIMEOUT_S = 2.0
REPLY_TIMEOUT_S = 1.0

# First field of the ID reply, compared ignoring case.
ACCEPTED_NAMES = frozenset({"misha led panels"})
# 2.0.0 is the first firmware that acknowledges every command (ADR 0004).
MIN_FIRMWARE = (2, 0, 0)

WAVELENGTHS_NM = (
    365, 385, 395, 420, 450, 470, 500, 530,
    560, 590, 615, 630, 660, 730, 850, 940,
)  # fmt: skip
CHANNELS = tuple(Channel(f"{nm}nm", wavelength_nm=float(nm)) for nm in WAVELENGTHS_NM)

REFLASH_HINT = (
    "Reflash the board with the firmware in firmware/misha/ of the "
    "educelab-devices repository; its README has the steps."
)


class MishaLight(Light):
    """The MISHA LED panels: 16 monochromatic bands from 365 nm to 940 nm.

    Only one band is lit at a time, so setting a level above zero on one
    channel turns the others off. Needs firmware 2.0.0 or newer.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.
    """

    def __init__(self, port: str, *, transport: SerialTransport | None = None) -> None:
        super().__init__()
        self._port = port
        self._transport = transport or SerialTransport(
            port,
            baudrate=BAUD_RATE,
            timeout=REPLY_TIMEOUT_S,
            reset_settle=RESET_SETTLE_S,
        )
        self._firmware_version: str | None = None
        self._hardware_version: str | None = None
        # The lit channel and its level; None when everything is off.
        self._lit: tuple[str, float] | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="misha",
            model="MISHA",
            address=self._port,
            firmware_version=self._firmware_version,
        )

    @property
    def hardware_version(self) -> str | None:
        """The board's hardware revision, as reported when it was opened."""
        return self._hardware_version

    @property
    def channels(self) -> tuple[Channel, ...]:
        return CHANNELS

    def __repr__(self) -> str:
        return f"<MishaLight {self._port}>"

    def _open(self) -> None:
        self._transport.open()
        try:
            identity = handshake(
                self._transport, "ID", _parse_identity, HANDSHAKE_TIMEOUT_S
            )
            if identity is None:
                raise DeviceNotFound(
                    f"no MISHA answered on {self._port!r}; check the port, "
                    "or that the board isn't held by another program"
                )
            _, hardware, firmware = identity
            if _version(firmware) < MIN_FIRMWARE:
                minimum = ".".join(map(str, MIN_FIRMWARE))
                raise DeviceError(
                    f"{self!r} has firmware {firmware}, but this driver needs "
                    f"{minimum} or newer. {REFLASH_HINT}"
                )
            self._hardware_version = hardware
            self._firmware_version = firmware
            self._all_off()
        except BaseException:
            self._transport.close()
            raise

    def _close(self) -> None:
        try:
            self._command("0,0")
        except DeviceError as exc:
            logger.warning("%r could not turn off before closing: %s", self, exc)
        finally:
            self._transport.close()

    def _set_level(self, channel: Channel, level: float) -> None:
        percent = round(level * 100)
        if percent > 0:
            self._command(f"{channel.wavelength_nm:.0f},{percent}")
            self._lit = (channel.id, percent / 100)
        elif self._lit is not None and self._lit[0] == channel.id:
            self._all_off()
        # Otherwise the band is already off. Sending it would turn off the lit one.

    def _get_level(self, channel: Channel) -> float | None:
        if self._lit is not None and self._lit[0] == channel.id:
            return self._lit[1]
        return 0.0

    def _all_off(self) -> None:
        self._command("0,0")
        self._lit = None

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise NotImplementedError("MISHA channels are monochromatic")

    def _command(self, command: str) -> None:
        """Send a command and wait for its OK."""
        self._transport.reset_input()
        self._transport.write_line(command)
        reply = self._transport.read_line()
        if reply == "OK":
            return
        if reply.startswith("ERR"):
            reason = reply[3:].strip() or "no reason given"
            raise CommandRejected(f"{self!r} rejected {command!r}: {reason}")
        raise DeviceError(f"{self!r} answered {command!r} with {reply!r}")


def _parse_identity(line: str) -> tuple[str, str, str] | None:
    """Split an ID reply into (name, hardware, firmware) if it's from a MISHA."""
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 3 or parts[0].casefold() not in ACCEPTED_NAMES:
        return None
    return parts[0], parts[1], parts[2]


def _version(text: str) -> tuple[int, ...]:
    """Parse ``"2.0.0"``; anything unparseable sorts below every release."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()
