"""Contract tests shared by every implementation of a role.

A driver's test module subclasses the contract for each role the device
offers and overrides the ``make_device`` fixture to return a new, unopened
device (a real driver on a fake transport, or a simulated device)::

    class TestSimulatedLight(LightContract):
        @pytest.fixture
        def make_device(self):
            return SimulatedLight
"""

from contracts.cameras import CameraContract, FileCameraContract, FrameCameraContract
from contracts.device import DeviceContract
from contracts.focus import FocusDriveContract
from contracts.hubs import HubContract
from contracts.lights import LightContract
from contracts.stages import StageContract

__all__ = [
    "CameraContract",
    "DeviceContract",
    "FileCameraContract",
    "FocusDriveContract",
    "FrameCameraContract",
    "HubContract",
    "LightContract",
    "StageContract",
]
