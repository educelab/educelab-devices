"""GenTL tests against real cameras: ``pytest -m hardware tests/test_gentl_hardware.py -s``.

Needs the ``[gentl]`` extra and a producer (e.g. IDS peak's) in a folder listed in
``GENICAM_GENTL64_PATH`` (docs/hardware/usb3-vision.md). ``GENTL_SERIAL`` picks
the camera; see `GenICamHardwareTests` for the rest.
"""

import pytest

from contracts.genicam_hardware import GenICamHardwareTests
from educelab.devices.cameras.gentl import GenTLCamera, find_producers


@pytest.mark.hardware
class TestGenTLHardware(GenICamHardwareTests):
    driver = GenTLCamera
    serial_variable = "GENTL_SERIAL"

    def test_producers_found(self, camera):
        # Records which producers were searched, for the hardware doc.
        print(f"producers: {find_producers()}")
        assert find_producers()
