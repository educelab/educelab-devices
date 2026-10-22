"""Aravis tests against real cameras: ``pytest -m hardware tests/test_aravis_hardware.py -s``.

Needs Aravis 0.8 and PyGObject (docs/hardware/usb3-vision.md). ``ARAVIS_SERIAL``
picks the camera; see `GenICamHardwareTests` for the rest.
"""

import pytest

from contracts.genicam_hardware import GenICamHardwareTests
from educelab.devices.cameras import EXPOSURE_TIME
from educelab.devices.cameras.aravis import AravisCamera


@pytest.mark.hardware
class TestAravisHardware(GenICamHardwareTests):
    driver = AravisCamera
    serial_variable = "ARAVIS_SERIAL"

    def test_device_timestamp_against_buffer_arrival(self, camera):
        # What moment the device stamps, relative to when the host had the whole
        # frame. Settles the rolling-shutter question in usb3-vision.md, Timestamps.
        conn = camera._connection
        exposure = camera.setting(EXPOSURE_TIME).value
        gaps = []
        for _ in range(10):
            buffer = conn.grab(5.0)
            try:
                stamped = camera.timestamp_offset + buffer.device_timestamp_ns() / 1e9
                arrived = buffer._buffer.get_system_timestamp() / 1e9
            finally:
                buffer.release()
            gaps.append(arrived - stamped)
        print(
            f"exposure {exposure * 1000:.1f} ms; arrival minus device stamp: "
            f"{', '.join(f'{g * 1000:.1f}' for g in gaps)} ms"
        )
        assert all(-0.01 < g < 1.0 for g in gaps)
