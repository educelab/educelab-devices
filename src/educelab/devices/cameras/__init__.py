"""Cameras: devices that capture frames or camera files."""

from educelab.devices.cameras._genicam import (
    THROUGHPUT_LIMIT,
    TRIGGER,
    GenICamCamera,
    host_time,
)
from educelab.devices.cameras.aravis import AravisCamera
from educelab.devices.cameras.base import (
    APERTURE,
    CANONICAL_UNITS,
    EXPOSURE_TIME,
    GAIN,
    HEIGHT,
    ISO,
    PIXEL_FORMAT,
    SENSOR_HEIGHT,
    SENSOR_WIDTH,
    WIDTH,
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
from educelab.devices.cameras.gentl import GenTLCamera
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
    "HEIGHT",
    "ISO",
    "PIXEL_FORMAT",
    "SENSOR_HEIGHT",
    "SENSOR_WIDTH",
    "THROUGHPUT_LIMIT",
    "TRIGGER",
    "WIDTH",
    "AravisCamera",
    "Camera",
    "CameraFile",
    "FileCamera",
    "FocusDrive",
    "FocusLimit",
    "Frame",
    "FrameCamera",
    "GenICamCamera",
    "GenTLCamera",
    "Rounding",
    "Setting",
    "SettingValue",
    "SimulatedCamera",
    "SimulatedFileCamera",
    "SimulatedFocusDrive",
    "SpinnakerCamera",
    "SpinnakerSystem",
    "host_time",
]
