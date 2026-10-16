"""The Acroname USBHub3p, through Acroname's BrainStem SDK.

See ``docs/hardware/acroname-usbhub3p.md``.
"""

import logging
import math
import numbers
from enum import StrEnum
from types import ModuleType
from typing import Any

from educelab.devices.device import DeviceInfo, synchronized
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    import_optional,
)
from educelab.devices.hubs.base import Hub
from educelab.devices.settings import Setting

__all__ = ["AcronameHub", "PortSpeed"]

logger = logging.getLogger(__name__)

HARDWARE_DOC = "docs/hardware/acroname-usbhub3p.md"
MODEL = "USBHub3p"

# brainstem.stem.USBHub3p.NUMBER_OF_DOWNSTREAM_USB, numbered from 0.
DOWNSTREAM_PORTS = 8
# Port state bits, from the aUSBHUB3P_* constants on brainstem.stem.USBHub3p.
VBUS_ENABLED_BIT = 0
USB2_DATA_ENABLED_BIT = 1
USB3_DATA_ENABLED_BIT = 3
SPEED_USB2_BIT = 11
SPEED_USB3_BIT = 12
# The SDK documents the enumeration delay in 100 ms increments (assumed; see
# HARDWARE_DOC).
ENUMERATION_DELAY_STEP_S = 0.1

# brainstem.result.Result codes the driver treats specially.
_NO_ERROR = 0
_NOT_FOUND = 3
_BUSY = 5
_PERMISSION = 12
_TIMEOUT = 18
_CONNECTION = 25
# Parameter, mode, range, unimplemented, and invalid option: the hub understood
# the command and refused it.
_REJECTED = frozenset({2, 7, 13, 21, 29})

# Serial numbers and millisecond delays are C unsigned ints in the SDK.
_MAX_UINT32 = 0xFFFFFFFF

# brainstem.result.Result descriptions, indexed by code.
_DESCRIPTIONS = (
    "No error occurred.",
    "Memory allocation/de-allocation error.",
    "Invalid parameters given.",
    "Entity, module or information not found.",
    "File name is too long.",
    "Module or resource is currently busy.",
    "An Input/Output error occurred.",
    "Invalid mode or mode not accessible for current state.",
    "Write error occurred.",
    "Read error occurred.",
    "Unexpected end of file encountered.",
    "Resource not ready.",
    "Insufficient permissions.",
    "Request is outside of valid range.",
    "Size is incorrect for resource.",
    "Buffer was overrun or will be.",
    "Unable to parse command.",
    "Configuration is invalid.",
    "Timeout occurred.",
    "Could not initialize resource.",
    "Version mismatch.",
    "Functionality unavailable or unimplemented.",
    "Duplicate request received.",
    "Request was canceled.",
    "Packet was invalid or had invalid contents.",
    "Connection is no longer valid, or was closed.",
    "Requested entity does not exist.",
    "Command too short, not enough data to parse.",
    "Entity is not available, or does not exist.",
    "Option for given entity is invalid.",
    "Error allocating or acquiring a resource.",
    "Media not found or not available.",
    "Asynchronous return.",
    "Stream value is stale.",
    "Unknown error encountered.",
)


class PortSpeed(StrEnum):
    """The fastest USB data speed a port allows."""

    USB3 = "usb3"
    """SuperSpeed and Hi-Speed data lines enabled. The hub's default."""
    USB2 = "usb2"
    """Hi-Speed data lines only, so a USB 3 device connects at USB 2."""
    DATA_OFF = "data-off"
    """No data lines: the port supplies power only, e.g. for charging."""


def load_sdk() -> ModuleType:
    """Import the BrainStem SDK.

    Raises:
        ImportError: ``brainstem`` isn't installed, or is installed but can't
            load its native library on this platform.
    """
    try:
        return import_optional("brainstem", extra="acroname")
    except OSError as exc:
        # brainstem raises OSError at import when its native library won't load,
        # e.g. on Windows without the MSVC runtime.
        raise ImportError(
            f"brainstem is installed but could not load its native library: {exc} "
            f"See {HARDWARE_DOC}.",
            name="brainstem",
        ) from exc


class AcronameHub(Hub):
    """An Acroname USBHub3p, which switches power and data on 8 downstream ports.

    Ports are numbered 0 to 7. `power_off()` disables both power and data on a
    port, and `power_on()` enables both; `is_powered()` reports whether the port
    has power.

    Args:
        serial_number: The hub's serial number, as an int or a hex string
            (``"0x1234ABCD"`` or ``"1234ABCD"``), as `discover()` reports it.
            ``None`` opens the only hub attached and refuses to guess when
            there are several; once found, that hub is the one reopened.
        sdk: The ``brainstem`` package, or a stand-in with the same calls for
            testing. ``None`` imports ``brainstem`` on `open()`.

    Raises:
        TypeError: ``serial_number`` is neither an int nor a string.
        ValueError: ``serial_number`` isn't a 32-bit hex number.
    """

    def __init__(
        self, serial_number: int | str | None = None, *, sdk: Any = None
    ) -> None:
        super().__init__()
        self._serial_number = (
            None if serial_number is None else parse_serial_number(serial_number)
        )
        self._sdk = sdk
        self._stem: Any = None
        self._firmware_version: str | None = None
        self._speeds: dict[int, PortSpeed] = {}

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List the USBHub3p hubs attached over USB, without connecting to them.

        Raises:
            ImportError: The BrainStem SDK can't be imported.
        """
        return [
            DeviceInfo(backend="acroname", model=MODEL, serial_number=_format(serial))
            for serial in _find_hubs(load_sdk())
        ]

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="acroname",
            model=MODEL,
            serial_number=_format_or_none(self._serial_number),
            firmware_version=self._firmware_version,
        )

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(range(DOWNSTREAM_PORTS))

    @synchronized
    def enumeration_delay(self) -> Setting:
        """How long the hub waits between enumerating its ports, in seconds."""
        delay_ms = self._get(
            "read the enumeration delay", self._usb.getEnumerationDelay
        )
        return _enumeration_delay(delay_ms / 1000)

    @synchronized
    def set_enumeration_delay(self, seconds: float) -> None:
        """Set how long the hub waits between enumerating its ports.

        The hub takes whole steps of 100 ms, so ``seconds`` is rounded to the
        nearest step. This changes the running hub only; it isn't saved to the
        hub's flash.

        Raises:
            TypeError: ``seconds`` isn't a number.
            ValueError: ``seconds`` is negative or not finite.
        """
        setting = _enumeration_delay(0.0)
        seconds = float(setting.validate(seconds))
        if not math.isfinite(seconds) or seconds * 1000 > _MAX_UINT32:
            raise ValueError(f"enumeration_delay={seconds} is too large")
        delay_ms = round(setting.nearest(seconds) * 1000)
        self._check(
            "set the enumeration delay", self._usb.setEnumerationDelay(delay_ms)
        )

    @synchronized
    def set_port_speed(self, port: int, speed: PortSpeed | str) -> None:
        """Limit ``port`` to ``speed``, switching only its data lines.

        A device already attached may keep its current connection until it
        re-enumerates; call `power_cycle()` on the port to make it reconnect at
        the new speed. The limit lasts while the hub is open, including across
        `power_off()` and `power_on()`, and isn't saved to the hub's flash. On
        an unpowered port it applies at the next `power_on()`.

        Raises:
            KeyError: The hub has no such port.
            ValueError: ``speed`` isn't a `PortSpeed`.
        """
        port = self._port(port)
        speed = PortSpeed(speed)
        if self._is_powered(port):
            self._apply_speed(port, speed)
        self._speeds[port] = speed

    @synchronized
    def port_speed(self, port: int) -> PortSpeed:
        """The fastest speed ``port`` allows.

        This is the limit set since `open()`, or what the hub reported at
        `open()`. A port that was unpowered at `open()` reports `PortSpeed.USB3`,
        since its data lines say nothing about its limit (see the hardware doc).

        Raises:
            KeyError: The hub has no such port.
        """
        return self._speeds[self._port(port)]

    @synchronized
    def connected_speed(self, port: int) -> PortSpeed | None:
        """The speed the device on ``port`` connected at, or ``None`` if none has.

        Low- and full-speed devices report `PortSpeed.USB2`.

        Raises:
            KeyError: The hub has no such port.
        """
        port = self._port(port)
        state = self._get(f"read port {port}", self._usb.getPortState, port)
        if state & (1 << SPEED_USB3_BIT):
            return PortSpeed.USB3
        if state & (1 << SPEED_USB2_BIT):
            return PortSpeed.USB2
        return None

    def __repr__(self) -> str:
        serial = _format_or_none(self._serial_number)
        return f"<AcronameHub {serial}>" if serial else "<AcronameHub>"

    def _open(self) -> None:
        sdk = self._sdk if self._sdk is not None else load_sdk()
        serial = self._serial_number
        if serial is None:
            serial = self._only_hub(sdk)
        stem = sdk.stem.USBHub3p()
        code = stem.discoverAndConnect(sdk.link.Spec.USB, serial)
        if code != _NO_ERROR:
            if code == _NOT_FOUND:
                raise DeviceNotFound(
                    f"no {MODEL} with serial number {_format(serial)} is attached"
                )
            raise self._error(f"connect to {_format(serial)}", code)
        self._serial_number = serial
        self._stem = stem
        try:
            packed = self._get("read the firmware version", stem.system.getVersion)
            self._firmware_version = ".".join(
                str(n) for n in sdk.version.unpack_version(packed)
            )
            self._speeds = {port: self._read_speed(port) for port in self.ports}
        except BaseException:
            try:
                self._disconnect()
            except DeviceError:
                # The original failure is the one worth reporting.
                logger.debug("%r disconnect after failed open also failed", self)
            raise
        logger.debug("connected to %r, firmware %s", self, self._firmware_version)

    def _close(self) -> None:
        self._disconnect()

    def _disconnect(self) -> None:
        stem, self._stem = self._stem, None
        self._firmware_version = None
        self._speeds = {}
        if stem is None:
            return
        code = stem.disconnect()
        if code == _CONNECTION:
            # Already gone, e.g. unplugged; there's nothing left to release.
            logger.debug("%r was already disconnected", self)
            return
        self._check("disconnect", code)

    def _set_power(self, port: int, on: bool) -> None:
        logger.debug("%r port %d %s", self, port, "on" if on else "off")
        if not on:
            self._check(f"disable port {port}", self._usb.setPortDisable(port))
            return
        speed = self._speeds[port]
        if speed is PortSpeed.USB3:
            self._check(f"enable port {port}", self._usb.setPortEnable(port))
            return
        # A full enable would turn on every data line and lift the limit.
        self._check(f"power port {port}", self._usb.setPowerEnable(port))
        self._apply_speed(port, speed)

    def _apply_speed(self, port: int, speed: PortSpeed) -> None:
        logger.debug("%r port %d data lines for %s", self, port, speed)
        usb = self._usb
        action = f"set port {port} to {speed}"
        # Disable before enabling, so the port never briefly allows more.
        if speed is not PortSpeed.USB3:
            self._check(action, usb.setSuperSpeedDataDisable(port))
        if speed is PortSpeed.DATA_OFF:
            self._check(action, usb.setHiSpeedDataDisable(port))
        else:
            self._check(action, usb.setHiSpeedDataEnable(port))
        if speed is PortSpeed.USB3:
            self._check(action, usb.setSuperSpeedDataEnable(port))

    def _read_speed(self, port: int) -> PortSpeed:
        state = self._get(f"read port {port}", self._usb.getPortState, port)
        if not state & (1 << VBUS_ENABLED_BIT):
            # Powering a port off clears its data lines too.
            return PortSpeed.USB3
        usb2 = state & (1 << USB2_DATA_ENABLED_BIT)
        usb3 = state & (1 << USB3_DATA_ENABLED_BIT)
        if usb2 and not usb3:
            return PortSpeed.USB2
        if not usb2 and not usb3:
            return PortSpeed.DATA_OFF
        if not usb2:
            logger.debug("%r port %d has SuperSpeed lines only", self, port)
        return PortSpeed.USB3

    def _is_powered(self, port: int) -> bool:
        state = self._get(f"read port {port}", self._usb.getPortState, port)
        return bool(state & (1 << VBUS_ENABLED_BIT))

    @property
    def _usb(self) -> Any:
        return self._stem.usb

    def _only_hub(self, sdk: Any) -> int:
        found = _find_hubs(sdk)
        if len(found) == 1:
            return found[0]
        if not found:
            raise DeviceNotFound(f"no {MODEL} is attached")
        listed = ", ".join(_format(serial) for serial in found)
        raise DeviceNotFound(
            f"several {MODEL} hubs are attached ({listed}); pass serial_number"
        )

    def _get(self, action: str, call: Any, *args: int) -> int:
        """Run an SDK getter and return its value, raising on an error code."""
        result = call(*args)
        self._check(action, result.error)
        return int(result.value)

    def _check(self, action: str, code: int) -> None:
        if code != _NO_ERROR:
            raise self._error(action, code)

    def _error(self, action: str, code: int) -> DeviceError:
        description = (
            _DESCRIPTIONS[code] if 0 <= code < len(_DESCRIPTIONS) else "Unknown error."
        )
        message = f"{self!r} could not {action}: {description} (BrainStem error {code})"
        if code == _TIMEOUT:
            return DeviceTimeout(message)
        if code == _BUSY:
            return DeviceBusy(message)
        if code == _CONNECTION:
            return NotConnected(f"{message}. Check the hub's USB and power cables.")
        if code == _NOT_FOUND:
            return DeviceNotFound(message)
        if code in _REJECTED:
            return CommandRejected(message)
        if code == _PERMISSION:
            return DeviceError(
                f"{message}. On Linux, install Acroname's udev rules; see "
                f"{HARDWARE_DOC}."
            )
        return DeviceError(message)


def _find_hubs(sdk: Any) -> list[int]:
    specs = sdk.discover.findAllModules(sdk.link.Spec.USB)
    return [
        int(spec.serial_number)
        for spec in specs
        if spec.model == sdk.defs.MODEL_USBHUB_3P
    ]


def _enumeration_delay(seconds: float) -> Setting:
    return Setting(
        name="enumeration_delay",
        value=seconds,
        minimum=0.0,
        step=ENUMERATION_DELAY_STEP_S,
        unit="s",
    )


def parse_serial_number(serial_number: int | str) -> int:
    """A hub serial number from an int or a hex string, with or without ``0x``.

    Raises:
        TypeError: ``serial_number`` is neither an int nor a string.
        ValueError: ``serial_number`` isn't a nonzero 32-bit hex number.
    """
    if isinstance(serial_number, str):
        try:
            value = int(serial_number.strip(), 16)
        except ValueError:
            raise ValueError(
                f"serial_number must be a hex number, not {serial_number!r}"
            ) from None
    elif isinstance(serial_number, numbers.Integral) and not isinstance(
        serial_number, bool
    ):
        value = int(serial_number)
    else:
        raise TypeError(
            f"serial_number must be an int or hex string, not {serial_number!r}"
        )
    # The SDK reads serial number 0 as "any hub", so it's never a real one.
    if not 0 < value <= _MAX_UINT32:
        raise ValueError(f"{serial_number!r} is not a valid serial number")
    return value


def _format(serial_number: int) -> str:
    return f"0x{serial_number:08X}"


def _format_or_none(serial_number: int | None) -> str | None:
    return None if serial_number is None else _format(serial_number)
