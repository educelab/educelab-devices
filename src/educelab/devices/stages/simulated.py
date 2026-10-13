"""A stage with no hardware behind it."""

import logging
import threading
import time
from collections.abc import Mapping

from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceBusy, DeviceTimeout, PositionError
from educelab.devices.stages.base import Axis, AxisKind, Stage

__all__ = ["SimulatedStage"]

logger = logging.getLogger(__name__)

DEFAULT_AXES = (
    Axis("x", AxisKind.LINEAR, 0.0, 300.0),
    Axis("y", AxisKind.LINEAR, 0.0, 300.0),
    Axis("theta", AxisKind.ROTARY, wraps=True),
)

# How often a simulated motion updates its position and checks for stop().
_TICK_S = 0.005


class SimulatedStage(Stage):
    """A stage whose moves take time proportional to distance.

    All axes move at ``speed`` and arrive together. Position is kept across
    close and reopen. Homing drives every axis to its minimum (or 0).

    Args:
        axes: The axes to offer. Defaults to linear ``x`` and ``y``
            (0–300 mm) and a wrapping rotary ``theta``.
        speed: Speed of every axis, in mm/s or deg/s.
        can_home: Whether the stage can home.
        requires_homing: Whether the stage must be homed before moving.
    """

    def __init__(
        self,
        axes: tuple[Axis, ...] = DEFAULT_AXES,
        speed: float = 1000.0,
        can_home: bool = True,
        requires_homing: bool = False,
    ) -> None:
        super().__init__()
        if speed <= 0:
            raise ValueError(f"speed must be positive, not {speed}")
        if requires_homing and not can_home:
            raise ValueError("requires_homing needs can_home")
        self.can_home = can_home
        self._axes = tuple(axes)
        self._speed = speed
        self.requires_homing = requires_homing
        self._positions = {axis.name: _reference(axis) for axis in self._axes}
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._jog_thread: threading.Thread | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="simulated", model="SimulatedStage")

    @property
    def axes(self) -> tuple[Axis, ...]:
        return self._axes

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        self._stop()

    def _position(self) -> Mapping[str, float]:
        with self._state_lock:
            return dict(self._positions)

    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        current = self._position()
        offsets = {}
        for name, target in targets.items():
            offset = target - current[name]
            if self._axis(name).wraps:
                offset = (offset + 180.0) % 360.0 - 180.0
            offsets[name] = offset
        self._travel(offsets, timeout)

    def _move_by(self, offsets: Mapping[str, float], timeout: float) -> None:
        self._travel(offsets, timeout)

    def _home(self, timeout: float) -> None:
        current = self._position()
        reference = {axis.name: _reference(axis) for axis in self._axes}
        offsets = {name: reference[name] - current[name] for name in reference}
        self._travel(offsets, timeout)

    def _jog(self, axis: str, direction: int) -> None:
        self._check_not_jogging()
        self._stop_event.clear()
        self._jog_thread = threading.Thread(
            target=self._run_jog,
            args=(self._axis(axis), direction),
            name="simulated-stage-jog",
            daemon=True,
        )
        self._jog_thread.start()

    def _stop(self) -> None:
        self._stop_event.set()
        jog = self._jog_thread
        if jog is not None and jog is not threading.current_thread():
            jog.join()
        self._jog_thread = None

    def _travel(self, offsets: Mapping[str, float], timeout: float) -> None:
        self._check_not_jogging()
        self._stop_event.clear()
        start = self._position()
        duration = max(abs(offset) for offset in offsets.values()) / self._speed
        began = time.monotonic()
        while True:
            elapsed = time.monotonic() - began
            fraction = 1.0 if duration == 0 else min(elapsed / duration, 1.0)
            self._set_positions(
                {
                    name: start[name] + offset * fraction
                    for name, offset in offsets.items()
                }
            )
            if fraction >= 1.0:
                return
            if elapsed > timeout:
                raise DeviceTimeout(
                    f"{self!r} move did not finish within {timeout:g} s"
                )
            if self._stop_event.wait(_TICK_S):
                raise PositionError(f"{self!r} move was interrupted by stop()")

    def _run_jog(self, axis: Axis, direction: int) -> None:
        step = direction * self._speed * _TICK_S
        while not self._stop_event.wait(_TICK_S):
            position = self._position()[axis.name] + step
            at_limit = False
            if axis.minimum is not None and position <= axis.minimum:
                position, at_limit = axis.minimum, True
            if axis.maximum is not None and position >= axis.maximum:
                position, at_limit = axis.maximum, True
            self._set_positions({axis.name: position})
            if at_limit:
                return

    def _set_positions(self, positions: Mapping[str, float]) -> None:
        with self._state_lock:
            for name, position in positions.items():
                if self._axis(name).wraps:
                    position %= 360.0
                self._positions[name] = position

    def _check_not_jogging(self) -> None:
        jog = self._jog_thread
        if jog is not None and jog.is_alive():
            raise DeviceBusy(f"{self!r} is jogging; call stop() first")


def _reference(axis: Axis) -> float:
    return axis.minimum if axis.minimum is not None else 0.0
