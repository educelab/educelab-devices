"""The XLC2 light controller for the pgs-scan gantry rigs, over USB serial.

Protocol, firmware history and flashing: docs/hardware/xlc2.md.
"""

import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor

from educelab.devices import _serial
from educelab.devices._serial import (
    PortInfo,
    PortMatch,
    SerialTransport,
    command,
    handshake,
    parse_version,
)
from educelab.devices.device import DeviceInfo, synchronized
from educelab.devices.errors import DeviceError, DeviceNotFound
from educelab.devices.lights.base import RGB, Channel, ChannelConcurrency, Light

__all__ = ["XLC2"]

# (hardware version, firmware version, unit ID) from a handshake reply.
Identity = tuple[str | None, str, str | None]

logger = logging.getLogger(__name__)

BAUD_RATE = 9600
# Opening the port resets the board (DTR), and pgs-scan waits 2 s before its
# handshake. See docs/hardware/xlc2.md#transport.
RESET_SETTLE_S = 2.0
HANDSHAKE_TIMEOUT_S = 2.0
REPLY_TIMEOUT_S = 1.0

# Name in the handshake reply, compared ignoring case.
NAME = "EduceLab XLC2"
# 2.0.0 is the first firmware that acknowledges every command (ADR 0004).
MIN_FIRMWARE = (2, 0, 0)
# 2.1.0 adds the unit ID: stored in EEPROM, reported in the handshake.
UNIT_ID_FIRMWARE = (2, 1, 0)
UNIT_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")
# Both outputs take a duty from 0 to 255.
MAX_DUTY = 255

# USB-serial bridges found on XLC2s. Only these ports are probed, since
# probing resets the board behind a port; they must never include another
# driver's ports, such as the X-Controller's FTDI (docs/hardware/xlc2.md).
BRIDGES = (
    PortMatch(vid=0x04D9, pid=0xB534),  # Holtek HT42B534
    PortMatch(vid=0x1A86, pid=0x7523),  # WCH CH340
)

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
        serial_number: The board's unit ID, as set with `set_unit_id()`,
            matched exactly. `open()` finds it with `probe()` each time, so it
            survives the port being renamed after a replug.
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
        self._unit_id: str | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="xlc",
            model="XLC2",
            address=self._port,
            serial_number=self._unit_id or self._serial_number,
            firmware_version=self._firmware_version,
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List ports that could be an XLC2, without opening them.

        These are the ports with a USB-serial bridge seen on XLC2s, so other
        boards with the same bridge are listed too. Serial numbers are unknown
        until a board is opened or probed (docs/hardware/xlc2.md#identity).

        Raises:
            ImportError: pyserial isn't installed.
        """
        return [_info(port.device) for port in _candidates()]

    @classmethod
    def probe(cls) -> list[DeviceInfo]:
        """Find the XLC2s attached by sending each candidate port the handshake.

        Unlike `discover()`, this opens ports, which resets the boards behind
        them and turns their lights off. Only ports with an XLC2's USB bridge
        are opened. Ports that can't be opened are skipped: on Windows, any
        port another program has open; on Linux and macOS, only one held by a
        program that locks it (as this package does), so close other serial
        programs first. Boards are probed at once, so this takes about 4 s
        however many there are.

        Returns:
            One `DeviceInfo` per XLC2 that answered, with its unit ID as
            ``serial_number`` (``None`` if unset) and its firmware version.
            Boards with firmware too old for this driver are included.

        Raises:
            ImportError: pyserial isn't installed.
        """
        found = _probe_all()
        for _, _, transport in found:
            transport.close()
        return [_info(port, identity) for port, identity, _ in found]

    @property
    def hardware_version(self) -> str | None:
        """The board's hardware version, as reported when it was opened."""
        return self._hardware_version

    @property
    def channels(self) -> tuple[Channel, ...]:
        return CHANNELS

    @property
    def concurrency(self) -> ChannelConcurrency:
        return ChannelConcurrency.INDEPENDENT

    @synchronized
    def set_unit_id(self, unit_id: str) -> None:
        """Store a unit ID on the board, reported as its serial number from now on.

        The ID is kept in the board's EEPROM, so it survives power cycles and
        reflashing. Needs firmware 2.1.0 or newer.

        Args:
            unit_id: 1–16 characters from ``A–Z``, ``a–z``, ``0–9``, ``_``
                and ``-``, e.g. ``"XLC2-01"``.

        Raises:
            ValueError: ``unit_id`` isn't a valid ID.
            DeviceError: The firmware is too old to store one.
        """
        if not UNIT_ID.fullmatch(unit_id):
            raise ValueError(
                f"an XLC2 unit ID is 1-16 letters, digits, '_' or '-', not {unit_id!r}"
            )
        if parse_version(self._firmware_version or "") < UNIT_ID_FIRMWARE:
            raise DeviceError(
                f"{self!r} has firmware {self._firmware_version}, which can't store "
                "a unit ID; reflash it with 2.1.0 or newer "
                "(docs/hardware/xlc2.md#flashing)"
            )
        self._command("U", unit_id)
        self._unit_id = unit_id
        if self._serial_number is not None:
            # Opened by its ID, so reopening must look for the new one.
            self._serial_number = unit_id

    def __repr__(self) -> str:
        where = self._port or self._serial_number or "(no port)"
        return (
            f"<XLC2 {self._unit_id} at {where}>" if self._unit_id else f"<XLC2 {where}>"
        )

    def _open(self) -> None:
        if self._serial_number is not None:
            # Found again on each open, in case a replug renamed the port.
            transport, identity = self._find_unit()
        else:
            transport, identity = self._open_port()
        try:
            hardware, firmware, unit_id = identity
            if parse_version(firmware) < MIN_FIRMWARE:
                minimum = ".".join(map(str, MIN_FIRMWARE))
                raise DeviceError(
                    f"{self!r} has firmware {firmware}, but this driver needs "
                    f"{minimum} or newer. Reflash the board with the firmware in "
                    "firmware/xlc2/ of the educelab-devices repository; "
                    "docs/hardware/xlc2.md#flashing has the steps."
                )
            self._hardware_version = hardware
            self._firmware_version = firmware
            self._unit_id = unit_id
            self._all_off()
            for channel in CHANNELS:
                self._remember(channel, 0.0)
        except BaseException:
            transport.close()
            raise

    def _open_port(self) -> tuple[SerialTransport, Identity]:
        """Open the given port and handshake, leaving it open if an XLC2 answered."""
        if self._transport is None:
            raise DeviceNotFound(
                "XLC2 needs port= or serial_number=; XLC2.probe() lists the "
                "boards attached"
            )
        transport = self._transport
        transport.open()
        try:
            identity = handshake(transport, "<?>", _parse_identity, HANDSHAKE_TIMEOUT_S)
        except BaseException:
            transport.close()
            raise
        if identity is None:
            transport.close()
            raise DeviceNotFound(
                f"no XLC2 answered on {self._port!r}; check the port, "
                "or that the board isn't held by another program"
            )
        return transport, identity

    def _find_unit(self) -> tuple[SerialTransport, Identity]:
        """Probe for the board with our unit ID, keeping its port open."""
        found = _probe_all()
        matches = [f for f in found if f[1][2] == self._serial_number]
        keep = matches[0][2] if len(matches) == 1 else None
        for _, _, transport in found:
            if transport is not keep:
                transport.close()
        if not matches:
            seen = ", ".join(
                f"{ident[2] or '(no ID)'} at {port}" for port, ident, _ in found
            )
            raise DeviceNotFound(
                f"no XLC2 has unit ID {self._serial_number!r}; "
                + (f"found {seen}" if seen else "found no XLC2s")
            )
        if len(matches) > 1:
            where = ", ".join(port for port, _, _ in matches)
            raise DeviceError(
                f"{len(matches)} XLC2s have unit ID {self._serial_number!r} "
                f"({where}); give one a new ID with set_unit_id()"
            )
        port, identity, transport = matches[0]
        self._transport = transport
        self._port = port
        return transport, identity

    def _close(self) -> None:
        try:
            self._all_off()
        except DeviceError as exc:
            logger.warning("%r could not turn off before closing: %s", self, exc)
        finally:
            self._require_transport().close()

    def _set_level(self, channel: Channel, level: float) -> None:
        self._command(COMMANDS[channel.id], round(level * MAX_DUTY))

    def _all_off(self) -> None:
        # Each output separately, so one failing doesn't leave the other lit.
        failures = []
        for channel in CHANNELS:
            try:
                self._set_level(channel, 0.0)
            except DeviceError as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise NotImplementedError("XLC2 channels are monochromatic")

    def _command(self, *fields: object) -> None:
        """Send one framed command and wait for its OK."""
        line = "<" + ",".join(map(str, fields)) + ">"
        command(self._require_transport(), line, self)

    def _require_transport(self) -> SerialTransport:
        assert self._transport is not None, "no transport before open()"
        return self._transport


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(
        port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S, reset_settle=RESET_SETTLE_S
    )


def _parse_identity(line: str) -> Identity | None:
    """(hardware, firmware, unit ID) if ``line`` is an XLC2's handshake reply.

    From 2.1.0 a fourth field holds the unit ID, empty if none is set.
    Recognizes the pre-2.0.0 reply too, so old boards are refused as old
    rather than not found.
    """
    parts = [part.strip() for part in line.split(",")]
    if len(parts) in (3, 4) and parts[0].casefold() == NAME.casefold():
        unit_id = parts[3] if len(parts) == 4 and parts[3] else None
        return parts[1], parts[2], unit_id
    old = re.fullmatch(rf"{NAME} \(FW ([^)]*)\)", line.strip(), re.IGNORECASE)
    return (None, old[1], None) if old else None


def _candidates() -> list[PortInfo]:
    """Ports with an XLC2's bridge."""
    return [
        port for port in _serial.list_ports() if any(m.matches(port) for m in BRIDGES)
    ]


def _info(port: str, identity: Identity | None = None) -> DeviceInfo:
    _, firmware, unit_id = identity or (None, None, None)
    return DeviceInfo(
        backend="xlc",
        model="XLC2",
        address=port,
        serial_number=unit_id,
        firmware_version=firmware,
    )


def _probe_all() -> list[tuple[str, Identity, SerialTransport]]:
    """Handshake every candidate at once. The XLC2s' ports are left open."""
    ports = [port.device for port in _candidates()]
    if not ports:
        return []
    futures: list[Future[tuple[str, Identity, SerialTransport] | None]] = []
    try:
        with ThreadPoolExecutor(max_workers=len(ports)) as pool:
            futures = [pool.submit(_probe_one, port) for port in ports]
        results = [future.result() for future in futures]
    except BaseException:
        # Interrupted, or a probe failed: close every port that was opened,
        # now or, for probes still running, when they finish.
        for future in futures:
            future.add_done_callback(_close_probed)
        raise
    return [result for result in results if result is not None]


def _close_probed(future: Future[tuple[str, Identity, SerialTransport] | None]) -> None:
    if (
        not future.cancelled()
        and future.exception() is None
        and (result := future.result()) is not None
    ):
        result[2].close()


def _probe_one(port: str) -> tuple[str, Identity, SerialTransport] | None:
    transport = _default_transport(port)
    try:
        transport.open()
    except DeviceNotFound as exc:
        logger.debug("not probing %s: %s", port, exc)
        return None
    try:
        identity = handshake(transport, "<?>", _parse_identity, HANDSHAKE_TIMEOUT_S)
    except DeviceError as exc:
        logger.debug("probing %s failed: %s", port, exc)
        identity = None
    except BaseException:
        transport.close()
        raise
    if identity is None:
        logger.debug("no XLC2 answered on %s", port)
        transport.close()
        return None
    return port, identity, transport
