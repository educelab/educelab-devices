"""The thin layer between `GPhoto2Camera` and python-gphoto2.

The driver talks to libgphoto2 only through `Library` and `Session`, so its
tests can swap in a fake. `LibGPhoto2` is the real implementation; it is the
only code that imports the SDK.
"""

import logging
import sys
import threading
from types import ModuleType
from typing import Any

from educelab.devices.errors import import_optional

__all__ = ["LIBGPHOTO2_LOGGER", "LogBridge", "import_sdk"]

LIBGPHOTO2_LOGGER = "educelab.devices.cameras.gphoto2.libgphoto2"
"""Logger that receives libgphoto2's own log messages."""


def import_sdk() -> ModuleType:
    """Import python-gphoto2 and bridge libgphoto2's logging into Python's.

    Raises:
        ImportError: python-gphoto2 isn't installed (or can't be on Windows).
    """
    hint = None
    if sys.platform == "win32":
        hint = "libgphoto2 isn't available on Windows; use macOS or Linux."
    gp = import_optional("gphoto2", extra="gphoto2", hint=hint)
    _log_bridge.install(gp)
    return gp


class LogBridge:
    """Forwards libgphoto2's log messages to `LIBGPHOTO2_LOGGER`.

    libgphoto2 often reports a failure to Python as a bare ``[-1] Unspecified
    error`` and logs the real reason, so these messages matter. Its errors
    become warnings; its verbose and debug output become info and debug.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Dropping python-gphoto2's handle would uninstall the callback.
        self._handle: Any = None

    def install(self, gp: Any) -> None:
        """Register the callback with libgphoto2. Later calls do nothing."""
        with self._lock:
            if self._handle is not None:
                return
            levels = {
                gp.GP_LOG_ERROR: logging.WARNING,
                gp.GP_LOG_VERBOSE: logging.INFO,
                gp.GP_LOG_DEBUG: logging.DEBUG,
            }
            logger = logging.getLogger(LIBGPHOTO2_LOGGER)

            def forward(level: int, domain: str, message: str, data: object) -> None:
                python_level = levels.get(level, logging.DEBUG)
                if logger.isEnabledFor(python_level):
                    logger.log(python_level, "%s: %s", domain, message)

            # GP_LOG_DATA (raw USB bytes) is left out; it floods the log.
            self._handle = gp.gp_log_add_func(gp.GP_LOG_DEBUG, forward, None)


_log_bridge = LogBridge()
