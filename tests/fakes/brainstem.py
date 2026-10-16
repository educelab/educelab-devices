"""A stand-in for the slice of the BrainStem SDK that `AcronameHub` uses.

Pass a `FakeBrainstem` wherever the driver takes ``sdk``. It mirrors the SDK's
call shapes: commands return an integer error code, and getters return an
object with ``error`` and ``value``. Hubs are `FakeUnit`s on a shared bus, so
tests can unplug one, make it busy, or make a command fail with a given code.

Devices plugged into a unit's ports (``FakeUnit.devices``) show up in the port
state as attached, at the fastest speed both they and the port's enabled data
lines allow. The fake renegotiates instantly; whether the real hub does is in
the hardware doc.
"""

from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace

# Error codes from brainstem.result.Result.
NO_ERROR = 0
PARAMETER_ERROR = 2
NOT_FOUND = 3
BUSY = 5
IO_ERROR = 6
PERMISSION_ERROR = 12
RANGE_ERROR = 13
TIMEOUT = 18
CONNECTION_ERROR = 25
INDEX_RANGE_ERROR = 26

USB = 1
MODEL_USBHUB_3P = 19
OTHER_MODEL = 4

VBUS_ENABLED = 1 << 0
USB2_DATA_ENABLED = 1 << 1
USB3_DATA_ENABLED = 1 << 3
SPEED_USB2 = 1 << 11
SPEED_USB3 = 1 << 12
DEVICE_ATTACHED = 1 << 23
DATA_LINES = USB2_DATA_ENABLED | USB3_DATA_ENABLED
ALL_LINES = VBUS_ENABLED | DATA_LINES


@dataclass
class FakeResult:
    error: int
    value: int = 0


@dataclass
class FakeSpec:
    transport: int
    serial_number: int
    module: int
    model: int


@dataclass
class FakeUnit:
    """One hub on the fake bus.

    Attributes:
        failures: Method name (e.g. ``"setPortDisable"``) to the error code
            that method returns every time it's called.
        busy: Whether connecting fails as if another process holds the hub.
        present: Whether the hub is plugged in. Unplugging a connected hub
            makes every later call fail with a connection error.
        port_state: Each port's enabled lines (VBUS and data bits).
        devices: Port number to the attached device's fastest speed,
            ``"usb2"`` or ``"usb3"``.
    """

    serial_number: int
    model: int = MODEL_USBHUB_3P
    firmware: tuple[int, int, int] = (2, 12, 0)
    ports: int = 8
    enumeration_delay_ms: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    busy: bool = False
    present: bool = True
    calls: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    connections: int = 0
    devices: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.port_state = [ALL_LINES] * self.ports

    def state(self, port: int) -> int:
        """The port state as ``getPortState`` reports it."""
        lines = self.port_state[port]
        device = self.devices.get(port)
        if device is None or not lines & VBUS_ENABLED:
            return lines
        state = lines | DEVICE_ATTACHED
        if device == "usb3" and lines & USB3_DATA_ENABLED:
            return state | SPEED_USB3
        if lines & USB2_DATA_ENABLED:
            return state | SPEED_USB2
        return state


class _Hub:
    """Plays ``brainstem.stem.USBHub3p``."""

    def __init__(self, bus: "FakeBrainstem") -> None:
        self._bus = bus
        self._unit: FakeUnit | None = None
        self.system = SimpleNamespace(getVersion=self._get_version)
        switches = {
            "PortEnable": (ALL_LINES, True),
            "PortDisable": (ALL_LINES, False),
            "PowerEnable": (VBUS_ENABLED, True),
            "PowerDisable": (VBUS_ENABLED, False),
            "DataEnable": (DATA_LINES, True),
            "DataDisable": (DATA_LINES, False),
            "HiSpeedDataEnable": (USB2_DATA_ENABLED, True),
            "HiSpeedDataDisable": (USB2_DATA_ENABLED, False),
            "SuperSpeedDataEnable": (USB3_DATA_ENABLED, True),
            "SuperSpeedDataDisable": (USB3_DATA_ENABLED, False),
        }
        self.usb = SimpleNamespace(
            **{
                f"set{name}": partial(self._switch, f"set{name}", bits, on)
                for name, (bits, on) in switches.items()
            },
            getPortState=self._get_port_state,
            getPortCurrent=self._get_port_current,
            getEnumerationDelay=self._get_enumeration_delay,
            setEnumerationDelay=self._set_enumeration_delay,
        )

    def discoverAndConnect(self, transport: int, serial_number: int) -> int:
        if transport != USB:
            return PARAMETER_ERROR
        # Like the SDK, serial number 0 means the first hub found.
        unit = self._bus.unit(serial_number) if serial_number else self._bus.first()
        if unit is None or not unit.present:
            return NOT_FOUND
        if unit.busy:
            return BUSY
        if code := unit.failures.get("discoverAndConnect"):
            return code
        unit.connections += 1
        self._unit = unit
        return NO_ERROR

    def disconnect(self) -> int:
        unit, self._unit = self._unit, None
        if unit is None:
            return NO_ERROR  # as the SDK does
        unit.connections -= 1
        return unit.failures.get("disconnect", NO_ERROR)

    def _call(self, name: str, *args: int) -> int:
        """Error code for a command, before it takes effect."""
        unit = self._unit
        if unit is None or not unit.present:
            return CONNECTION_ERROR
        unit.calls.append((name, args))
        return unit.failures.get(name, NO_ERROR)

    def _port(self, name: str, channel: int) -> int:
        code = self._call(name, channel)
        if code == NO_ERROR and not 0 <= channel < self._connected.ports:
            return INDEX_RANGE_ERROR
        return code

    @property
    def _connected(self) -> FakeUnit:
        assert self._unit is not None
        return self._unit

    def _get_version(self) -> FakeResult:
        if code := self._call("getVersion"):
            return FakeResult(code)
        major, minor, patch = self._connected.firmware
        return FakeResult(NO_ERROR, (major << 24) | (minor << 16) | patch)

    def _switch(self, name: str, bits: int, on: bool, channel: int) -> int:
        if code := self._port(name, channel):
            return code
        if on:
            self._connected.port_state[channel] |= bits
        else:
            self._connected.port_state[channel] &= ~bits
        return NO_ERROR

    def _get_port_state(self, channel: int) -> FakeResult:
        if code := self._port("getPortState", channel):
            return FakeResult(code)
        return FakeResult(NO_ERROR, self._connected.state(channel))

    def _get_port_current(self, channel: int) -> FakeResult:
        if code := self._port("getPortCurrent", channel):
            return FakeResult(code)
        attached = self._connected.state(channel) & DEVICE_ATTACHED
        return FakeResult(NO_ERROR, 100_000 if attached else 0)

    def _get_enumeration_delay(self) -> FakeResult:
        if code := self._call("getEnumerationDelay"):
            return FakeResult(code)
        return FakeResult(NO_ERROR, self._connected.enumeration_delay_ms)

    def _set_enumeration_delay(self, ms: int) -> int:
        if code := self._call("setEnumerationDelay", ms):
            return code
        self._connected.enumeration_delay_ms = ms
        return NO_ERROR


class FakeBrainstem:
    """Plays the ``brainstem`` package for the hubs in ``units``."""

    def __init__(self, *units: FakeUnit) -> None:
        self.units = list(units)
        self.link = SimpleNamespace(Spec=SimpleNamespace(USB=USB))
        self.defs = SimpleNamespace(MODEL_USBHUB_3P=MODEL_USBHUB_3P)
        self.discover = SimpleNamespace(findAllModules=self._find_all_modules)
        self.stem = SimpleNamespace(USBHub3p=lambda: _Hub(self))
        self.version = SimpleNamespace(unpack_version=_unpack_version)

    def unit(self, serial_number: int) -> FakeUnit | None:
        return next((u for u in self.units if u.serial_number == serial_number), None)

    def first(self) -> FakeUnit | None:
        return next((u for u in self.units if u.present), None)

    def _find_all_modules(self, transports: int) -> list[FakeSpec]:
        if transports != USB:
            return []
        return [
            FakeSpec(USB, u.serial_number, 6, u.model) for u in self.units if u.present
        ]


def _unpack_version(packed: int) -> tuple[int, int, int]:
    return (packed >> 24) & 0xFF, (packed >> 16) & 0xFF, packed & 0xFFFF
