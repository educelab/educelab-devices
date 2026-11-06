"""Spinnaker tests against real cameras: ``pytest -m hardware tests/test_spinnaker_hardware.py -s``.

Needs PySpin (Python 3.12; docs/hardware/spinnaker.md). ``SPINNAKER_SERIAL``
picks the camera; see `GenICamHardwareTests` for the rest.
"""

import sys
from datetime import UTC, datetime

import pytest

from contracts.genicam_hardware import GenICamHardwareTests
from educelab.devices.cameras.spinnaker import SpinnakerCamera, SpinnakerSystem


@pytest.mark.hardware
class TestSpinnakerHardware(GenICamHardwareTests):
    driver = SpinnakerCamera
    serial_variable = "SPINNAKER_SERIAL"

    def test_frames_keep_their_dtype_referenced(self, camera):
        # PySpin dropping a dtype reference per GetNDArray() would show up as a
        # drift of about -300.
        dtype = camera.capture_frame().pixels.dtype
        refs = sys.getrefcount(dtype)
        for _ in range(300):
            frame = camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
        del frame
        drift = sys.getrefcount(dtype) - refs
        print(f"300 captures: {dtype} refcount drift {drift}")
        assert drift > -10

    def test_reopening_releases_the_system_each_time(self, serial, camera):
        # Needs the class's camera closed: holding it would keep the system open.
        camera.close()
        try:
            for _ in range(3):
                with SpinnakerCamera(serial) as again:
                    again.capture_frame()
            with SpinnakerSystem() as system:
                assert system.discover()
        finally:
            camera.open()
