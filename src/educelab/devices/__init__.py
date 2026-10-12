from importlib.metadata import PackageNotFoundError, version

from educelab.devices.device import (
    Device,
    DeviceEvent,
    DeviceInfo,
    Disconnected,
    Listener,
    synchronized,
)

__all__ = [
    "Device",
    "DeviceEvent",
    "DeviceInfo",
    "Disconnected",
    "Listener",
    "__version__",
    "synchronized",
]

try:
    __version__ = version("educelab-devices")
except PackageNotFoundError:
    __version__ = "0.0.0"
