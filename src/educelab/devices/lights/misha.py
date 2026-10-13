"""MISHA 16-band LED panels, driven over USB serial.

Protocol, timing, and firmware history: docs/hardware/misha.md.
"""

import logging

from educelab.devices._serial import PortMatch, SerialTransport, handshake, list_ports
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

# USB-serial bridges a board may sit behind (CH340, CP210x, FTDI, Prolific,
# Arduino, SparkFun), from pgs-scan's spectral prototype. Discovery writes to
# these ports only; other devices' ports aren't ours to send commands to.
CANDIDATE_PORTS = PortMatch(vid={0x1A86, 0x10C4, 0x0403, 0x067B, 0x2341, 0x1B4F})

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
    channel turns the others off; setting an unlit band to zero changes
    nothing. Levels are read from the panel, and opening leaves it as it was.
    Closing turns everything off. Needs firmware 2.0.0 or newer.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/ttyUSB0"``.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.
    """

    def __init__(self, port: str, *, transport: SerialTransport | None = None) -> None:
        super().__init__()
        self._port = port
        self._transport = transport or _default_transport(port)
        self._firmware_version: str | None = None
        self._hardware_version: str | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="misha",
            model="MISHA",
            address=self._port,
            firmware_version=self._firmware_version,
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """Find MISHA panels by asking each USB-serial port to identify itself.

        Slow: opening a port resets the board behind it, so each candidate
        port takes a few seconds. Boards with firmware too old to open are
        still listed.

        Raises:
            ImportError: pyserial isn't installed.
        """
        found = []
        for port in CANDIDATE_PORTS.matching(list_ports()):
            transport = _default_transport(port.device)
            try:
                transport.open()
                identity = handshake(
                    transport, "ID", _parse_identity, HANDSHAKE_TIMEOUT_S
                )
            except DeviceError as exc:
                # Usually a port held by another program; it can't be ours to open.
                logger.debug("skipping %s: %s", port.device, exc)
                continue
            finally:
                transport.close()
            if identity is not None:
                found.append(
                    DeviceInfo(
                        backend="misha",
                        model="MISHA",
                        address=port.device,
                        serial_number=port.serial_number,
                        firmware_version=identity[2],
                    )
                )
        return found

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
            # Fails early on a board that can't report its state.
            self._read_state()
        except BaseException:
            self._transport.close()
            raise

    def _close(self) -> None:
        try:
            self._command("OFF")
        except DeviceError as exc:
            logger.warning("%r could not turn off before closing: %s", self, exc)
        finally:
            self._transport.close()

    def _set_level(self, channel: Channel, level: float) -> None:
        # At 0 the firmware turns this band off only if it's the lit one.
        self._command(f"{channel.wavelength_nm:.0f},{round(level * 100)}")

    def _get_level(self, channel: Channel) -> float | None:
        wavelength, percent = self._read_state()
        return percent / 100 if wavelength == channel.wavelength_nm else 0.0

    def _all_off(self) -> None:
        self._command("OFF")

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise NotImplementedError("MISHA channels are monochromatic")

    def _read_state(self) -> tuple[int, int]:
        """The lit wavelength and its percent, or ``(0, 0)`` if all are off."""
        answer = self._command("STATE")
        try:
            wavelength, percent = (int(part) for part in answer.split(","))
        except ValueError:
            wavelength, percent = -1, -1
        if wavelength not in (0, *WAVELENGTHS_NM) or not 0 <= percent <= 100:
            raise DeviceError(f"{self!r} reported a state it can't be in: {answer!r}")
        return wavelength, percent

    def _command(self, command: str) -> str:
        """Send a command, wait for its OK, and return any answer after it."""
        self._transport.reset_input()
        self._transport.write_line(command)
        reply = self._transport.read_line()
        if reply == "OK" or reply.startswith("OK "):
            return reply[3:].strip()
        if reply.startswith("ERR"):
            reason = reply[3:].strip() or "no reason given"
            raise CommandRejected(f"{self!r} rejected {command!r}: {reason}")
        raise DeviceError(f"{self!r} answered {command!r} with {reply!r}")


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(
        port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S, reset_settle=RESET_SETTLE_S
    )


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
