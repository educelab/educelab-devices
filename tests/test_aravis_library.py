"""`AravisCamera` against real Aravis, through the simulated camera Aravis ships.

Needs no hardware, only Aravis 0.8 and PyGObject; skips without them (as in
CI). Aravis's "Fake" camera is a GigE device with the older ``ExposureTimeAbs``
node and no timestamp latch, so it checks the driver's use of the real library
rather than USB3 Vision behaviour.
"""

from datetime import UTC, datetime

import numpy as np
import pytest

from educelab.devices.cameras import EXPOSURE_TIME, PIXEL_FORMAT, aravis
from educelab.devices.cameras._genicam import TRIGGER
from educelab.devices.cameras.aravis import AravisCamera
from educelab.devices.errors import DeviceError

SERIAL = "1"


@pytest.fixture(scope="module")
def arv():
    try:
        arv = aravis.load_aravis().Aravis
    except ImportError as exc:
        pytest.skip(str(exc))
    arv.enable_interface("Fake")
    yield arv
    arv.disable_interface("Fake")


@pytest.fixture
def camera(arv):
    with AravisCamera(SERIAL) as camera:
        yield camera


def test_discover_finds_the_simulated_camera(arv):
    found = {i.serial_number: i for i in AravisCamera.discover()}
    assert found[SERIAL].model == "Fake"
    assert found[SERIAL].backend == "aravis"


def test_exposure_time_uses_the_older_node(camera):
    camera.set_setting(EXPOSURE_TIME, 0.005)
    assert camera.setting(EXPOSURE_TIME).value == pytest.approx(0.005)
    assert camera.capture_frame().metadata[EXPOSURE_TIME] == pytest.approx(0.005)


def test_every_pixel_format_captures(camera):
    shapes = {}
    for fmt in camera.setting(PIXEL_FORMAT).choices or ():
        camera.set_setting(PIXEL_FORMAT, fmt)
        frame = camera.capture_frame()
        assert frame.pixel_format == fmt
        assert frame.pixels.dtype == (np.uint16 if fmt.endswith("16") else np.uint8)
        shapes[fmt] = frame.pixels.shape
    assert shapes["RGB8"][2] == 3
    assert len(shapes["Mono16"]) == 2


def test_frames_carry_device_timestamps_and_ids(camera):
    first, second = camera.capture_frame(), camera.capture_frame()
    assert second.metadata["frame_id"] > first.metadata["frame_id"]
    assert (
        second.metadata["device_timestamp_ns"] > first.metadata["device_timestamp_ns"]
    )


def test_without_a_timestamp_latch_not_before_is_refused(camera):
    assert camera.timestamp_offset is None
    with pytest.raises(DeviceError, match="latch"):
        camera.capture_frame(not_before=datetime.now(UTC))


def test_software_trigger(camera):
    camera.set_setting(TRIGGER, "Software")
    try:
        frame = camera.capture_frame(timeout=5.0)
        assert frame.metadata["retriggers"] == 0
    finally:
        camera.set_setting(TRIGGER, "Off")


def test_preview(camera):
    preview = camera.preview_frame()
    assert preview.dtype == np.uint8


def test_reopening(arv):
    camera = AravisCamera(SERIAL)
    for _ in range(3):
        with camera:
            camera.capture_frame()
