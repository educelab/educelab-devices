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

    ``value`` is ``None`` while the device is in a state the setting can't
    express, such as a DSLR's shutter at Bulb. The setting still says what can
    be written, and writing a value leaves that state.

    A setting is a snapshot. What a setting accepts can depend on other
    settings (e.g. maximum exposure on frame rate), so read it again after
    changing anything else.
    """

    name: str
    value: SettingValue | None
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
            kind = "float" if self.value is None else type(self.value).__name__
            raise TypeError(
                f"setting {self.name!r} takes {kind}, not {type(value).__name__}"
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

    def nearest(
        self, value: float, direction: Rounding = "nearest", *, strict: bool = False
    ) -> float:
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
            strict: Refuse a value more than half a step past either end of
                the allowed values (any value past a range without a step),
                rather than giving that end.

        Raises:
            TypeError: The setting isn't numeric.
            ValueError: ``direction`` is unknown, or ``strict`` refused
                ``value``.
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
            if not allowed:
                raise TypeError(f"setting {self.name!r} has no numeric choices")
            if strict:
                below = (allowed[1] - allowed[0]) / 2 if len(allowed) > 1 else 0.0
                above = (allowed[-1] - allowed[-2]) / 2 if len(allowed) > 1 else 0.0
                self._check_reach(value, allowed[0], allowed[-1], below, above)
            return _pick(allowed, value, direction)
        low, high = self.minimum, self.maximum
        base = low if low is not None else 0.0
        if self.step and high is not None:
            # The top of the range may be off the grid.
            high = base + math.floor((high - base) / self.step + 1e-9) * self.step
        if strict:
            half = (self.step or 0.0) / 2
            self._check_reach(value, low, high, half, half)
        if self.step:
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

    def _check_reach(
        self,
        value: float,
        low: float | None,
        high: float | None,
        below: float,
        above: float,
    ) -> None:
        """Raise if ``value`` is more than ``below`` under ``low`` or ``above``
        over ``high``."""

        def past(limit: float) -> bool:
            return not math.isclose(value, limit, rel_tol=1e-9, abs_tol=1e-12)

        too_low = low is not None and value < low - below and past(low - below)
        too_high = high is not None and value > high + above and past(high + above)
        if not (too_low or too_high):
            return
        unit = f" {self.unit}" if self.unit else ""
        if low is not None and high is not None:
            where = f"outside {low:.4g} to {high:.4g}"
        elif too_low:
            where = f"below {low:.4g}"
        else:
            where = f"above {high:.4g}"
        raise ValueError(f"{self.name} {value:.4g}{unit} is {where}{unit}")


def _pick(allowed: list[float], value: float, direction: Rounding) -> float:
    if direction == "down":
        return max((a for a in allowed if a <= value), default=allowed[0])
    if direction == "up":
        return min((a for a in allowed if a >= value), default=allowed[-1])
    best = min(allowed, key=lambda a: (abs(a - value), a))
    # Halfway values are ties even when float error makes one side look closer.
    tied = [a for a in allowed if math.isclose(abs(a - value), abs(best - value))]
    return min(tied)


def _as_kind(value: SettingValue, current: SettingValue | None) -> SettingValue | None:
    """``value`` converted to the type of ``current``, or None if it can't be.

    With no current value to go by, only numbers are accepted, as floats.
    """
    if current is None:
        current = 0.0
    if isinstance(current, bool) or isinstance(value, bool):
        return value if isinstance(current, bool) and isinstance(value, bool) else None
    if isinstance(current, str):
        return value if isinstance(value, str) else None
    if isinstance(current, float):
        return float(value) if isinstance(value, numbers.Real) else None
    return int(value) if isinstance(value, numbers.Integral) else None
