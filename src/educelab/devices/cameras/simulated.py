"""Cameras and a focus drive with no hardware behind them."""

import logging

from educelab.devices.cameras.focus import FocusDrive, FocusLimit
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import FocusLimitReached

__all__ = ["SimulatedFocusDrive"]

logger = logging.getLogger(__name__)


class SimulatedFocusDrive(FocusDrive):
    """A focus drive over a fixed number of steps between its limits.

    Its hidden position survives close and reopen, like a real lens, even
    though `focus_steps` does not.

    Args:
        travel_steps: Steps from the near limit to the far limit.
        start: Hidden starting position, in steps from the near limit.
    """

    def __init__(self, travel_steps: int = 1000, start: int = 500) -> None:
        super().__init__()
        if not 0 <= start <= travel_steps:
            raise ValueError(f"start must be within 0–{travel_steps}, not {start}")
        self._travel = travel_steps
        self._lens_position = start

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="simulated", model="SimulatedFocusDrive")

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _step(self, steps: int) -> None:
        target = self._lens_position + steps
        self._lens_position = min(max(target, 0), self._travel)
        logger.debug("focus at %d of %d", self._lens_position, self._travel)
        if target < 0:
            raise FocusLimitReached("near")
        if target > self._travel:
            raise FocusLimitReached("far")

    def _drive_to_limit(self, limit: FocusLimit) -> None:
        self._lens_position = 0 if limit == "near" else self._travel
