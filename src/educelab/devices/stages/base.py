"""The stage role and its axes."""

import math
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from educelab.devices.device import Device, synchronized
from educelab.devices.errors import PositionError
from educelab.devices.settings import Setting

__all__ = ["Axis", "AxisKind", "Stage"]


class AxisKind(StrEnum):
    LINEAR = "linear"
    ROTARY = "rotary"


@dataclass(frozen=True)
class Axis:
    """One degree of freedom of a stage, in machine coordinates.

    Linear axes are in millimetres and rotary axes in degrees. A wrapping
    rotary axis has no limits and reports positions in [0, 360).

    Attributes:
        name: The axis's name, unique within its stage (e.g. ``"x"``).
        kind: Linear or rotary.
        minimum: Lowest reachable position, or ``None`` if unbounded.
        maximum: Highest reachable position, or ``None`` if unbounded.
        wraps: Whether positions wrap at 360 degrees.
    """

    name: str
    kind: AxisKind
    minimum: float | None = None
    maximum: float | None = None
    wraps: bool = False

    def __post_init__(self) -> None:
        if self.wraps and self.kind is not AxisKind.ROTARY:
            raise ValueError(f"axis {self.name!r}: only rotary axes can wrap")
        if self.wraps and (self.minimum is not None or self.maximum is not None):
            raise ValueError(f"axis {self.name!r}: a wrapping axis has no limits")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"axis {self.name!r}: minimum is above maximum")

    @property
    def unit(self) -> str:
        """``"mm"`` for linear axes, ``"deg"`` for rotary axes."""
        return "mm" if self.kind is AxisKind.LINEAR else "deg"

    def check(self, position: float) -> float:
        """Return ``position`` as a target for this axis.

        Wrapping axes reduce it to [0, 360).

        Raises:
            PositionError: ``position`` is outside the axis's limits.
        """
        if self.wraps:
            return position % 360.0
        if (self.minimum is not None and position < self.minimum) or (
            self.maximum is not None and position > self.maximum
        ):
            raise PositionError(
                f"{self.name}={position:g} {self.unit} is outside "
                f"[{self.minimum}, {self.maximum}]"
            )
        return position


class Stage(Device):
    """A device that positions something along one or more axes.

    Moves are blocking: they return once the stage has arrived, and raise
    `DeviceTimeout` if it takes longer than ``timeout`` seconds (default
    `move_timeout`). `stop()` may be called from another thread during a move;
    the interrupted move then raises `PositionError`.

    Stages with a hardware reference (e.g. limit switches) set `can_home`.
    Those that also set `requires_homing` refuse to move until `home()` has run
    on the current connection. Homing is never a side effect of `open()`.

    On a wrapping axis, `move_to()` is absolute: 720 is the same position as
    0, and moving there again does nothing. Use `move_by()` for whole turns.

    Moves and jogs run at the stage's speed, set with `set_speed()`.

    Drivers declare `axes` and implement `_move_to()`, `_jog()`, `_stop()`,
    `_position()`, `_speed()`, and `_set_speed()`, plus `_home()` if they can
    home.
    """

    move_timeout: float = 60.0
    """Default limit, in seconds, for a move or homing to finish."""

    can_home: bool = False
    """Whether the stage has a hardware reference that `home()` can find."""

    requires_homing: bool = False
    """Whether the stage must be homed before it will move. Needs `can_home`."""

    _homed: bool = False

    @property
    @abstractmethod
    def axes(self) -> tuple[Axis, ...]:
        """The stage's axes."""

    @property
    def is_homed(self) -> bool:
        """Whether `home()` has completed on the current connection."""
        return self._homed

    def open(self) -> None:
        if self.requires_homing and not self.can_home:
            raise ValueError(f"{self!r} sets requires_homing without can_home")
        super().open()

    @synchronized
    def home(self, timeout: float | None = None) -> None:
        """Establish the stage's reference position from the hardware.

        Raises:
            NotImplementedError: The stage has no hardware reference.
            DeviceTimeout: Homing did not finish in time.
        """
        if not self.can_home:
            raise NotImplementedError(f"{self!r} has no hardware reference to home to")
        self._homed = False
        self._home(self._timeout(timeout))
        self._homed = True

    @synchronized
    def move_to(
        self, targets: Mapping[str, float], timeout: float | None = None
    ) -> None:
        """Move axes to absolute positions and wait until they arrive.

        Wrapping axes take the shorter way round.

        Args:
            targets: Position per axis name. Axes not named stay put.
            timeout: Seconds to wait, or ``None`` for `move_timeout`.

        Raises:
            KeyError: An axis name is unknown.
            PositionError: A target is outside its axis's limits, the stage
                needs homing, or `stop()` interrupted the move.
            DeviceTimeout: The move did not finish in time.
        """
        checked = {
            axis.name: axis.check(position)
            for axis, position in self._resolve(targets).items()
        }
        self._check_homed()
        if checked:
            self._move_to(checked, self._timeout(timeout))

    @synchronized
    def move_by(
        self, offsets: Mapping[str, float], timeout: float | None = None
    ) -> None:
        """Move axes by signed offsets and wait until they arrive.

        Raises the same errors as `move_to()`.
        """
        resolved = self._resolve(offsets)
        self._check_homed()
        if not resolved:
            return
        current = self._position()
        for axis, offset in resolved.items():
            axis.check(current[axis.name] + offset)
        self._move_by(
            {axis.name: offset for axis, offset in resolved.items()},
            self._timeout(timeout),
        )

    @synchronized
    def jog(self, axis: str, direction: int) -> None:
        """Start moving one axis continuously and return; `stop()` ends it.

        Args:
            axis: Axis name.
            direction: ``+1`` or ``-1``.

        Raises:
            KeyError: The axis name is unknown.
            ValueError: ``direction`` is not ``+1`` or ``-1``.
            PositionError: The stage needs homing.
        """
        if direction not in (1, -1) or isinstance(direction, bool):
            raise ValueError(f"direction must be +1 or -1, not {direction!r}")
        self._axis(axis)
        self._check_homed()
        self._jog(axis, direction)

    @synchronized
    def speed(self) -> Setting:
        """The speed used by moves and jogs, and the speeds the stage accepts.

        The setting is named ``"speed"``, in mm/s if the stage has any linear
        axis and deg/s if it is rotary only. Stages with fixed speeds list them as ``choices``;
        `Setting.nearest()` picks one.
        """
        return self._speed()

    @synchronized
    def set_speed(self, speed: float) -> None:
        """Set the speed for later moves and jogs.

        Raises:
            ValueError: The stage doesn't accept ``speed``.
        """
        self._speed().validate(speed)
        self._set_speed(speed)

    def stop(self) -> None:
        """Stop all motion as soon as possible. Safe when nothing is moving.

        Doesn't wait for the device lock, so it can interrupt a move running
        on another thread.
        """
        self._require_open()
        self._stop()

    def position(self) -> dict[str, float]:
        """Current position per axis name, in machine coordinates.

        Doesn't wait for a move in progress, so while moving the values may be
        slightly stale.
        """
        self._require_open()
        return dict(self._position())

    def _reset_state(self) -> None:
        super()._reset_state()
        self._homed = False

    def _axis(self, name: str) -> Axis:
        for axis in self.axes:
            if axis.name == name:
                return axis
        raise KeyError(f"{self!r} has no axis {name!r}")

    def _resolve(self, values: Mapping[str, float]) -> dict[Axis, float]:
        resolved = {}
        for name, value in values.items():
            axis = self._axis(name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be a number, not {type(value).__name__}")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, not {value}")
            resolved[axis] = float(value)
        return resolved

    def _check_homed(self) -> None:
        if self.requires_homing and not self._homed:
            raise PositionError(f"{self!r} must be homed first; call home()")

    def _timeout(self, timeout: float | None) -> float:
        if timeout is None:
            return self.move_timeout
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, not {timeout}")
        return timeout

    @abstractmethod
    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        """Move to checked absolute targets, blocking until arrival."""

    def _move_by(self, offsets: Mapping[str, float], timeout: float) -> None:
        """Move by checked offsets. Override when the hardware moves relatively."""
        current = self._position()
        self._move_to(
            {
                name: self._axis(name).check(current[name] + offset)
                for name, offset in offsets.items()
            },
            timeout,
        )

    @abstractmethod
    def _jog(self, axis: str, direction: int) -> None:
        """Start continuous motion and return."""

    @abstractmethod
    def _stop(self) -> None:
        """Stop motion. Called without the device lock, possibly mid-move."""

    @abstractmethod
    def _position(self) -> Mapping[str, float]:
        """Positions of every axis. Called without the device lock."""

    @abstractmethod
    def _speed(self) -> Setting:
        """The current speed and what the stage accepts, named ``"speed"``."""

    @abstractmethod
    def _set_speed(self, speed: float) -> None:
        """Set the speed for later moves and jogs."""

    def _home(self, timeout: float) -> None:
        """Home the stage, blocking until done."""
        raise NotImplementedError
