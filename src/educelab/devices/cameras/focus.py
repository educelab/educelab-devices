"""The focus drive role."""

from abc import abstractmethod
from typing import Literal, get_args

from educelab.devices.device import Device, synchronized
from educelab.devices.errors import FocusLimitReached

__all__ = ["FocusDrive", "FocusLimit"]

FocusLimit = Literal["near", "far"]


class FocusDrive(Device):
    """Moves a lens's focus in signed, relative steps.

    Positive steps move toward far, negative toward near. The drive can't
    report an absolute position, only how many steps it has moved since it
    last reached a limit on this connection.

    Drivers implement `_step()` and `_drive_to_limit()`. A `_step()` that hits
    a limit raises `FocusLimitReached`.
    """

    _focus_steps: int | None = None
    _last_limit: FocusLimit | None = None

    @property
    def focus_steps(self) -> int | None:
        """Signed steps moved since `last_limit`, or ``None`` if no limit has
        been reached on this connection."""
        return self._focus_steps

    @property
    def last_limit(self) -> FocusLimit | None:
        """The limit `focus_steps` counts from, or ``None``."""
        return self._last_limit

    @synchronized
    def step(self, steps: int) -> None:
        """Move focus by ``steps``: positive toward far, negative toward near.

        Raises:
            TypeError: ``steps`` is not an integer.
            FocusLimitReached: A limit stopped the move early. `focus_steps`
                is then 0 relative to that limit.
        """
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise TypeError(f"steps must be an int, not {type(steps).__name__}")
        if steps == 0:
            return
        try:
            self._step(steps)
        except FocusLimitReached as exc:
            self._at_limit(exc.limit)
            raise
        if self._focus_steps is not None:
            self._focus_steps += steps

    @synchronized
    def drive_to_limit(self, limit: FocusLimit) -> None:
        """Drive focus until it reaches the ``"near"`` or ``"far"`` limit.

        Raises:
            ValueError: ``limit`` is not ``"near"`` or ``"far"``.
        """
        if limit not in get_args(FocusLimit):
            raise ValueError(f"limit must be 'near' or 'far', not {limit!r}")
        self._drive_to_limit(limit)
        self._at_limit(limit)

    def _at_limit(self, limit: FocusLimit) -> None:
        self._last_limit = limit
        self._focus_steps = 0

    def _reset_state(self) -> None:
        super()._reset_state()
        self._focus_steps = None
        self._last_limit = None

    @abstractmethod
    def _step(self, steps: int) -> None: ...

    @abstractmethod
    def _drive_to_limit(self, limit: FocusLimit) -> None: ...
