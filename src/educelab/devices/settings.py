"""Adjustable device parameters."""

import math
import numbers
from dataclasses import dataclass
from typing import Literal

__all__ = ["Rounding", "Setting", "SettingValue"]

SettingValue = bool | int | float | str
Rounding = Literal["nearest", "down", "up"]


@dataclass(frozen=True)
class Setting:
    """An adjustable parameter: its current value and what it accepts.

    A setting accepts either one of ``choices`` or a value of the same type as
    ``value``, within ``minimum`` and ``maximum`` when those are set. ``step``
    describes hardware that only takes ``minimum + k * step``; `validate()`
    doesn't enforce it, and the hardware rounds off-grid values itself. Use
    `nearest()` to get a value the hardware will keep exactly.

    A setting is a snapshot. What a setting accepts can depend on other
    settings (e.g. maximum exposure on frame rate), so read it again after
    changing anything else.
    """

    name: str
    value: SettingValue
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[SettingValue, ...] | None = None
    unit: str | None = None
    read_only: bool = False

    def validate(self, value: SettingValue) -> SettingValue:
        """Check that ``value`` could be written to this setting.

        Returns:
            ``value`` as the setting's own type: an int for a float setting
            becomes a float, and numpy scalars become Python numbers. Write
            this rather than ``value``.

        Raises:
            TypeError: ``value`` has the wrong type.
            ValueError: The setting is read-only, or ``value`` is out of range
                or not one of the choices.
        """
        if self.read_only:
            raise ValueError(f"setting {self.name!r} is read-only")
        if self.choices is not None:
            for choice in self.choices:
                if choice == value:
                    return choice
            raise ValueError(
                f"{value!r} is not a choice for {self.name!r}: {self.choices}"
            )
        converted = _as_kind(value, self.value)
        if converted is None:
            raise TypeError(
                f"setting {self.name!r} takes {type(self.value).__name__}, "
                f"not {type(value).__name__}"
            )
        value = converted
        if isinstance(value, bool | str):
            return value
        if math.isnan(value):
            raise ValueError(f"setting {self.name!r} can't be NaN")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}={value} is below {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name}={value} is above {self.maximum}")
        return value

    def nearest(self, value: float, direction: Rounding = "nearest") -> float:
        """The allowed value closest to ``value``.

        Allowed values are the ``choices`` if there are any, otherwise
        ``minimum + k * step`` if there is a step, otherwise anything in range.

        Args:
            value: The value wanted.
            direction: ``"nearest"`` picks the closest allowed value, breaking
                ties toward the lower one. ``"down"`` picks the largest allowed
                value not above ``value``, and ``"up"`` the smallest not below
                it. In every direction a value outside the allowed range gives
                the closest end of the range, so the result is always allowed.

        Raises:
            TypeError: The setting isn't numeric.
            ValueError: ``direction`` is unknown.
        """
        if direction not in ("nearest", "down", "up"):
            raise ValueError(
                f"direction must be nearest, down, or up, not {direction!r}"
            )
        if isinstance(self.value, bool | str):
            raise TypeError(f"setting {self.name!r} isn't numeric")
        if self.choices is not None:
            allowed = sorted(
                float(c) for c in self.choices if not isinstance(c, bool | str)
            )
            return _pick(allowed, value, direction)
        low, high = self.minimum, self.maximum
        if self.step:
            base = low if low is not None else 0.0
            if high is not None:
                # The top of the range may be off the grid.
                high = base + math.floor((high - base) / self.step + 1e-9) * self.step
            # Tolerance so a value already on the grid isn't pushed off it by
            # floating-point error in the division.
            k = (value - base) / self.step
            below = base + math.floor(k + 1e-9) * self.step
            above = base + math.ceil(k - 1e-9) * self.step
            value = _pick([below, above], value, direction)
        if low is not None:
            value = max(value, low)
        if high is not None:
            value = min(value, high)
        return value


def _pick(allowed: list[float], value: float, direction: Rounding) -> float:
    if direction == "down":
        return max((a for a in allowed if a <= value), default=allowed[0])
    if direction == "up":
        return min((a for a in allowed if a >= value), default=allowed[-1])
    best = min(allowed, key=lambda a: (abs(a - value), a))
    # Halfway values are ties even when float error makes one side look closer.
    tied = [a for a in allowed if math.isclose(abs(a - value), abs(best - value))]
    return min(tied)


def _as_kind(value: SettingValue, current: SettingValue) -> SettingValue | None:
    """``value`` converted to the type of ``current``, or None if it can't be."""
    if isinstance(current, bool) or isinstance(value, bool):
        return value if isinstance(current, bool) and isinstance(value, bool) else None
    if isinstance(current, str):
        return value if isinstance(value, str) else None
    if isinstance(current, float):
        return float(value) if isinstance(value, numbers.Real) else None
    return int(value) if isinstance(value, numbers.Integral) else None
