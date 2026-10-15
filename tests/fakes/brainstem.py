"""A stand-in for the slice of the BrainStem SDK that `AcronameHub` uses.

Pass a `FakeBrainstem` wherever the driver takes ``sdk``. It mirrors the SDK's
call shapes: commands return an integer error code, and getters return an
object with ``error`` and ``value``. Hubs are `FakeUnit`s on a shared bus, so
tests can unplug one, make it busy, or make a command fail with a given code.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace

# Error codes from brainstem.result.Result.
NO_ERROR = 0
PARAMETER_ERROR = 2
NOT_FOUND = 3
BUSY = 5
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
DEVICE_ATTACHED = 1 << 23


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

    def __post_init__(self) -> None:
        enabled = VBUS_ENABLED | USB2_DATA_ENABLED | USB3_DATA_ENABLED
        self.port_state = [enabled] * self.ports


class _Hub:
    """Plays ``brainstem.stem.USBHub3p``."""

    def __init__(self, bus: "FakeBrainstem") -> None:
        self._bus = bus
        self._unit: FakeUnit | None = None
        self.system = SimpleNamespace(getVersion=self._get_version)
        self.usb = SimpleNamespace(
            setPortEnable=self._set_port_enable,
            setPortDisable=self._set_port_disable,
            getPortState=self._get_port_state,
            getEnumerationDelay=self._get_enumeration_delay,
            setEnumerationDelay=self._set_enumeration_delay,
        )

    def connect(self, transport: int, serial_number: int) -> int:
        if transport != USB:
            return PARAMETER_ERROR
        unit = self._bus.unit(serial_number)
        if unit is None or not unit.present:
            return NOT_FOUND
        if unit.busy:
            return BUSY
        if code := unit.failures.get("connect"):
            return code
        unit.connections += 1
        self._unit = unit
        return NO_ERROR

    def disconnect(self) -> int:
        unit, self._unit = self._unit, None
        if unit is None:
            return CONNECTION_ERROR
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

    def _set_port_enable(self, channel: int) -> int:
        if code := self._port("setPortEnable", channel):
            return code
        self._connected.port_state[channel] |= (
            VBUS_ENABLED | USB2_DATA_ENABLED | USB3_DATA_ENABLED
        )
        return NO_ERROR

    def _set_port_disable(self, channel: int) -> int:
        if code := self._port("setPortDisable", channel):
            return code
        self._connected.port_state[channel] &= ~(
            VBUS_ENABLED | USB2_DATA_ENABLED | USB3_DATA_ENABLED
        )
        return NO_ERROR

    def _get_port_state(self, channel: int) -> FakeResult:
        if code := self._port("getPortState", channel):
            return FakeResult(code)
        return FakeResult(NO_ERROR, self._connected.port_state[channel])

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

    def _find_all_modules(self, transports: int) -> list[FakeSpec]:
        if transports != USB:
            return []
        return [
            FakeSpec(USB, u.serial_number, 6, u.model) for u in self.units if u.present
        ]


def _unpack_version(packed: int) -> tuple[int, int, int]:
    return (packed >> 24) & 0xFF, (packed >> 16) & 0xFF, packed & 0xFFFF
