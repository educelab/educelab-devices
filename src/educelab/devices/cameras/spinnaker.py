"""FLIR/Teledyne machine-vision cameras, through the Spinnaker SDK (PySpin).

PySpin is imported on first use, so this module imports without it. See
``docs/hardware/spinnaker.md`` for installing the SDK.
"""

import logging
from types import ModuleType, TracebackType
from typing import Any, Self

from educelab.devices.errors import import_optional

__all__ = ["PYSPIN_INSTALL_HINT", "SpinnakerSystem", "load_pyspin"]

logger = logging.getLogger(__name__)

PYSPIN_INSTALL_HINT = (
    "PySpin is not on PyPI. Install the Spinnaker SDK, then the PySpin wheel from "
    "the same SDK release (Python 3.12 and numpy<2 only). See docs/hardware/"
    "spinnaker.md: https://github.com/educelab/educelab-devices/blob/main/docs/"
    "hardware/spinnaker.md"
)


def load_pyspin() -> ModuleType:
    """Import PySpin, or raise an `ImportError` that explains how to install it."""
    return import_optional("PySpin", hint=PYSPIN_INSTALL_HINT)


class SpinnakerSystem:
    """A handle on PySpin's ``System``, which enumerates cameras.

    Args:
        pyspin: A module to use in place of PySpin, for tests.
    """

    def __init__(self, pyspin: ModuleType | None = None) -> None:
        self._pyspin_override = pyspin
        self._system: Any = None

    @property
    def is_open(self) -> bool:
        """Whether this handle holds the system."""
        return self._system is not None

    def open(self) -> None:
        """Take the PySpin ``System``. Does nothing if already open.

        Raises:
            ImportError: PySpin isn't installed.
        """
        if self._system is None:
            pyspin = self._pyspin_override or load_pyspin()
            self._system = pyspin.System.GetInstance()

    def close(self) -> None:
        """Let go of the system. Does nothing if already closed."""
        system, self._system = self._system, None
        if system is not None:
            system.ReleaseInstance()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
