"""Adjustable device parameters."""

import math
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

    def validate(self, value: SettingValue) -> None:
        """Check that ``value`` could be written to this setting.

        Raises:
            TypeError: ``value`` has the wrong type.
            ValueError: The setting is read-only, or ``value`` is out of range
                or not one of the choices.
        """
        if self.read_only:
            raise ValueError(f"setting {self.name!r} is read-only")
        if self.choices is not None:
            if value not in self.choices:
                raise ValueError(
                    f"{value!r} is not a choice for {self.name!r}: {self.choices}"
                )
            return
        if not _same_kind(value, self.value):
            raise TypeError(
                f"setting {self.name!r} takes {type(self.value).__name__}, "
                f"not {type(value).__name__}"
            )
        if isinstance(value, bool | str):
            return
        if math.isnan(value):
            raise ValueError(f"setting {self.name!r} can't be NaN")
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}={value} is below {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name}={value} is above {self.maximum}")

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
        result = value
        if self.step:
            base = self.minimum if self.minimum is not None else 0.0
            # Tolerance so a value already on the grid isn't pushed off it by
            # floating-point error in the division.
            k = (value - base) / self.step
            below = base + math.floor(k + 1e-9) * self.step
            above = base + math.ceil(k - 1e-9) * self.step
            result = _pick([below, above], value, direction)
        if self.minimum is not None:
            result = max(result, self.minimum)
        if self.maximum is not None:
            result = min(result, self.maximum)
        return result


def _pick(allowed: list[float], value: float, direction: Rounding) -> float:
    if direction == "down":
        return max((a for a in allowed if a <= value), default=allowed[0])
    if direction == "up":
        return min((a for a in allowed if a >= value), default=allowed[-1])
    best = min(allowed, key=lambda a: (abs(a - value), a))
    # Halfway values are ties even when float error makes one side look closer.
    tied = [a for a in allowed if math.isclose(abs(a - value), abs(best - value))]
    return min(tied)


def _same_kind(value: SettingValue, current: SettingValue) -> bool:
    if isinstance(current, bool) or isinstance(value, bool):
        return isinstance(current, bool) and isinstance(value, bool)
    if isinstance(current, float):
        return isinstance(value, int | float)
    return type(value) is type(current)
