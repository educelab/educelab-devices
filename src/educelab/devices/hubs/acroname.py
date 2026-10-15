"""The Acroname USBHub3p, through Acroname's BrainStem SDK.

See ``docs/hardware/acroname-usbhub3p.md``.
"""

import logging
from types import ModuleType

from educelab.devices.errors import import_optional

__all__: list[str] = []

logger = logging.getLogger(__name__)

HARDWARE_DOC = "docs/hardware/acroname-usbhub3p.md"


def load_sdk() -> ModuleType:
    """Import the BrainStem SDK.

    Raises:
        ImportError: ``brainstem`` isn't installed, or is installed but can't
            load its native library on this platform.
    """
    try:
        return import_optional("brainstem", extra="acroname")
    except OSError as exc:
        # brainstem raises OSError at import when its native library won't load,
        # e.g. on Windows without the MSVC runtime.
        raise ImportError(
            f"brainstem is installed but could not load its native library: {exc} "
            f"See {HARDWARE_DOC}.",
            name="brainstem",
        ) from exc
