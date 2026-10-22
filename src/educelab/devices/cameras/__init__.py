"""Cameras: devices that capture frames or camera files."""

from educelab.devices.cameras.aravis import AravisCamera
from educelab.devices.cameras.base import (
    APERTURE,
    CANONICAL_UNITS,
    EXPOSURE_TIME,
    GAIN,
    ISO,
    PIXEL_FORMAT,
    Camera,
    CameraFile,
    FileCamera,
    Frame,
    FrameCamera,
    Rounding,
    Setting,
    SettingValue,
)
from educelab.devices.cameras.focus import FocusDrive, FocusLimit
from educelab.devices.cameras.simulated import (
    SimulatedCamera,
    SimulatedFileCamera,
    SimulatedFocusDrive,
)
from educelab.devices.cameras.spinnaker import SpinnakerCamera, SpinnakerSystem

__all__ = [
    "APERTURE",
    "CANONICAL_UNITS",
    "EXPOSURE_TIME",
    "GAIN",
    "ISO",
    "PIXEL_FORMAT",
    "AravisCamera",
    "Camera",
    "CameraFile",
    "FileCamera",
    "FocusDrive",
    "FocusLimit",
    "Frame",
    "FrameCamera",
    "Rounding",
    "Setting",
    "SettingValue",
    "SimulatedCamera",
    "SimulatedFileCamera",
    "SimulatedFocusDrive",
    "SpinnakerCamera",
    "SpinnakerSystem",
]
