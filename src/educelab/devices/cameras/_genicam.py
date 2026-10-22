"""The seam between GenICam cameras and the libraries that reach them.

Every GenICam camera driver shares one core (ADR 0006), which talks to the
camera only through a `Backend` and the `Connection` it opens. Each library
(PySpin, Aravis, GenTL) has one backend. Tests swap in a fake.

Backends translate their library's errors into `DeviceError` subclasses: a
grab or other wait that runs out raises `DeviceTimeout`, a camera held by
another process raises `DeviceBusy`, and anything else raises `DeviceError`
with the library's own message. The core adds which camera and what it was
doing.
"""

import enum
from typing import Any, Protocol

import numpy.typing as npt

from educelab.devices.device import DeviceInfo

__all__ = ["Backend", "Buffer", "BufferStatus", "Connection"]


class BufferStatus(enum.Enum):
    """Whether a buffer holds a whole frame."""

    COMPLETE = "complete"
    MISSING_TRAILER = "missing trailer"
    """All the image data arrived, but not the packet that closes it."""
    INCOMPLETE = "incomplete"


class Buffer(Protocol):
    """One frame's buffer, held until `release()`.

    Reading a released buffer is undefined; copy the pixels first.
    """

    def status(self) -> BufferStatus: ...

    def status_text(self) -> str:
        """The library's description of the status, for logs."""
        ...

    def device_timestamp_ns(self) -> int:
        """The camera's timestamp counter for this frame, in nanoseconds."""
        ...

    def frame_id(self) -> int: ...

    def pixel_format(self) -> str:
        """The SFNC name of the pixel format, e.g. ``"Mono8"``."""
        ...

    def pixels(self) -> npt.NDArray[Any]:
        """A copy of the image, shaped ``(height, width)`` or ``(height, width, 3)``."""
        ...

    def release(self) -> None:
        """Give the buffer back to the stream."""
        ...


class Connection(Protocol):
    """One open camera: its node map and its stream.

    Nodes are named as in SFNC (e.g. ``"ExposureTime"``) and keep their native
    units. A node the camera lacks is neither readable nor writable, and
    reading or writing it raises `DeviceError`.

    The stream keeps only the newest frame: a grab returns the most recent
    one and drops any older ones still waiting.
    """

    def has(self, name: str) -> bool:
        """Whether the camera has the node at all, whatever its access."""
        ...

    def is_readable(self, name: str) -> bool: ...

    def is_writable(self, name: str) -> bool: ...

    def get_float(self, name: str) -> float: ...

    def get_int(self, name: str) -> int: ...

    def get_string(self, name: str) -> str: ...

    def get_enum(self, name: str) -> str:
        """The symbolic name of the current entry."""
        ...

    def float_range(self, name: str) -> tuple[float, float]:
        """The node's ``(minimum, maximum)``."""
        ...

    def int_range(self, name: str) -> tuple[int, int, int]:
        """The node's ``(minimum, maximum, increment)``."""
        ...

    def enum_entries(self, name: str) -> tuple[str, ...]:
        """The symbolic names of the entries currently available."""
        ...

    def set_float(self, name: str, value: float) -> None: ...

    def set_int(self, name: str, value: int) -> None: ...

    def set_enum(self, name: str, symbol: str) -> None: ...

    def execute(self, name: str) -> None:
        """Run a command node, e.g. ``"TriggerSoftware"``."""
        ...

    def start_acquisition(self) -> None: ...

    def stop_acquisition(self) -> None:
        """Stop acquiring. Does nothing if already stopped."""
        ...

    def grab(self, timeout: float | None) -> Buffer:
        """The newest frame, waiting up to ``timeout`` seconds for one.

        ``None`` waits indefinitely, and ``0`` takes a frame only if one is
        already waiting.

        Raises:
            DeviceTimeout: No frame came in time.
        """
        ...

    def close(self) -> None:
        """Stop acquiring and let go of the camera. Does nothing if closed.

        Everything is released even if a step fails.

        Raises:
            DeviceError: A step failed; the message lists each failure.
        """
        ...


class Backend(Protocol):
    """One library's way of finding and opening GenICam cameras."""

    name: str
    """The short name reported in `DeviceInfo.backend`, e.g. ``"aravis"``."""

    def discover(self) -> list[DeviceInfo]:
        """List attached cameras without opening them.

        Raises:
            ImportError: The library isn't installed.
            DeviceError: The library couldn't enumerate cameras.
        """
        ...

    def open(self, serial: str) -> Connection | None:
        """Open the camera with this serial number, or return None if none is
        attached.

        Raises:
            ImportError: The library isn't installed.
            DeviceBusy: Another process holds the camera.
            DeviceError: The camera was found but couldn't be opened.
        """
        ...
