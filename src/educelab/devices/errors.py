"""Errors raised by devices.

Every error a device raises for a hardware or connection problem is a
`DeviceError`. Invalid arguments raise the usual built-ins: `KeyError` for an
unknown channel, axis, port, or setting name, and `ValueError` for an
out-of-range value.
"""

import importlib
from types import ModuleType
from typing import Literal

__all__ = [
    "CommandRejected",
    "DeviceBusy",
    "DeviceError",
    "DeviceNotFound",
    "DeviceTimeout",
    "FocusLimitReached",
    "NotConnected",
    "PositionError",
    "import_optional",
]


class DeviceError(Exception):
    """Base class for errors raised by devices."""


class DeviceNotFound(DeviceError):
    """The device could not be found or opened at the given address."""


class NotConnected(DeviceError):
    """The device is not open, or its connection was lost."""


class DeviceTimeout(DeviceError, TimeoutError):
    """The device did not respond or finish within the allowed time."""


class CommandRejected(DeviceError):
    """The device received a command and refused it."""


class DeviceBusy(DeviceError):
    """The device is held by another process or is busy with another operation."""


class PositionError(DeviceError):
    """A stage cannot reach or trust a position.

    Raised for targets outside an axis's limits, moves on a stage that needs
    homing first, and moves interrupted by `stop()`.
    """


class FocusLimitReached(DeviceError):
    """A focus drive hit its near or far limit before finishing a move."""

    def __init__(self, limit: Literal["near", "far"], message: str | None = None):
        self.limit = limit
        super().__init__(message or f"focus drive reached its {limit} limit")


def import_optional(
    name: str, *, extra: str | None = None, hint: str | None = None
) -> ModuleType:
    """Import a vendor SDK, or explain how to install it.

    Backends call this at the point of use so importing a subpackage never
    needs the SDK.

    Args:
        name: Module to import, e.g. ``"serial"``.
        extra: The ``educelab-devices`` extra that provides it.
        hint: Install instructions for SDKs that no extra provides (PySpin).

    Raises:
        ImportError: The module could not be imported. The original error is
            chained, since some SDKs fail at import for reasons other than
            being absent.
    """
    if extra is None and hint is None:
        raise ValueError("import_optional() needs an extra or a hint")
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        remedy = hint or f"Install it with: pip install 'educelab-devices[{extra}]'"
        raise ImportError(f"Could not import {name!r}. {remedy}", name=name) from exc
