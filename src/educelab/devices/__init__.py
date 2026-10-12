from importlib.metadata import PackageNotFoundError, version

from educelab.devices.device import Device, DeviceInfo, synchronized

__all__ = ["Device", "DeviceInfo", "__version__", "synchronized"]

try:
    __version__ = version("educelab-devices")
except PackageNotFoundError:
    __version__ = "0.0.0"
