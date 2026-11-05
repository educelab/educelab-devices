"""`SpinnakerCamera`, tested against the fake PySpin in `fakes.pyspin`.

The behaviour every GenICam camera shares is tested once, in
``test_genicam_core.py``. These tests cover what PySpin adds: the shared
``System`` and its release, error codes and timeouts, image statuses, the
stream node map, and discovery.
"""

import math
import re
import sys
from datetime import UTC, datetime

import pytest

from contracts import FrameCameraContract
from contracts.genicam import BackendContract
from educelab.devices.app.cli import main
from educelab.devices.cameras import GAIN, spinnaker
from educelab.devices.cameras._genicam import BufferStatus
from educelab.devices.cameras.spinnaker import TRIGGER, SpinnakerCamera
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from fakes.genicam import FakeCameraError
from fakes.pyspin import (
    CameraPtr,
    FakePySpin,
    FrameSpec,
    SpinnakerException,
    flir_camera,
)

MONO = "20000001"
COLOR = "20000002"


@pytest.fixture
def pyspin():
    return FakePySpin(
        [
            flir_camera(MONO, "Blackfly S BFS-U3-51S5M"),
            flir_camera(COLOR, "Blackfly S BFS-U3-200S6C", color=True),
        ]
    )


@pytest.fixture
def fake(pyspin):
    """The fake hardware behind the mono camera."""
    return pyspin.system.cameras[0]


@pytest.fixture
def camera(pyspin):
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        yield camera


def assert_released(pyspin):
    assert pyspin.system.instances == 0
    assert pyspin.system.live_pointers == 0
    assert all(not c.opened for c in pyspin.system.cameras)


def stale(fake, age=1.0):
    """A frame already waiting in a buffer, exposed ``age`` seconds ago."""
    return FrameSpec(timestamp_ns=fake.device_time_ns() - round(age * 1e9))


# --- The shared System --------------------------------------------------------


def test_constructor_does_not_touch_the_sdk(pyspin):
    SpinnakerCamera(MONO, pyspin=pyspin)
    assert pyspin.system.get_instance_calls == 0


def test_close_releases_the_camera_and_the_system(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    camera.open()
    camera.close()
    assert_released(pyspin)


def test_reopening_initializes_again(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with camera:
        pass
    with camera:
        assert fake.opened
    assert fake.open_calls == 2
    assert_released(pyspin)


def test_cameras_share_the_system(pyspin):
    mono = SpinnakerCamera(MONO, pyspin=pyspin)
    color = SpinnakerCamera(COLOR, pyspin=pyspin)
    with mono:
        with color:
            assert pyspin.system.get_instance_calls == 1
        assert mono.is_open
        assert pyspin.system.instances == 1
    assert_released(pyspin)


def test_unknown_serial_raises_device_not_found_naming_what_is_attached(pyspin):
    with pytest.raises(DeviceNotFound, match=f"{MONO}.*{COLOR}"):
        SpinnakerCamera("99999999", pyspin=pyspin).open()
    assert_released(pyspin)


def test_unplugged_camera_fails_to_open_cleanly(pyspin, fake):
    pyspin.detach(MONO)
    with pytest.raises(DeviceNotFound):
        SpinnakerCamera(MONO, pyspin=pyspin).open()
    assert pyspin.system.instances == 0


def test_close_after_unplugging_still_releases_the_system(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    camera.open()
    pyspin.detach(MONO)
    camera.close()
    assert not camera.is_open
    assert pyspin.system.instances == 0


def test_close_ends_acquisition_before_deinit(pyspin, fake):
    # DeInit() refuses a camera that is still acquiring.
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    camera.open()
    camera.close()
    assert not fake.streaming
    assert_released(pyspin)


def test_close_raises_if_ending_acquisition_fails(pyspin, monkeypatch):
    def refuse(self):
        raise SpinnakerException("stream stuck")

    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    camera.open()
    monkeypatch.setattr(CameraPtr, "EndAcquisition", refuse)
    with pytest.raises(DeviceError, match="stream stuck"):
        camera.close()
    assert not camera.is_open


def test_close_after_captures_releases_the_system(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with camera:
        fake.queue_frames(stale(fake))
        camera.capture_frame(not_before=datetime.now(UTC))
        fake.queue_frames(*(stale(fake) for _ in range(100_000)))
        with pytest.raises(DeviceTimeout):
            camera.capture_frame(not_before=datetime.now(UTC), timeout=0.02)
    assert_released(pyspin)


# --- Errors -------------------------------------------------------------------


def test_camera_held_elsewhere_raises_device_busy(pyspin, fake):
    fake.busy = True
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with pytest.raises(DeviceBusy, match=MONO) as info:
        camera.open()
    assert not camera.is_open
    assert_released(pyspin)
    # A chained SpinnakerException would keep the camera pointer alive.
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_failed_configuration_releases_everything(pyspin, fake):
    pyspin.transport(fake).stream.nodes["StreamBufferHandlingMode"].access = pyspin.RO
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with pytest.raises(DeviceError, match="StreamBufferHandlingMode"):
        camera.open()
    assert not camera.is_open
    assert_released(pyspin)


def test_a_write_the_camera_refuses_raises_command_rejected(camera, fake):
    camera.set_setting("GainAuto", "Off")
    fake.failures["write Gain"] = FakeCameraError("range", "refused")
    with pytest.raises(CommandRejected, match="refused") as info:
        camera.set_setting(GAIN, 3.0)
    assert info.value.__context__ is None


def test_no_frame_in_time_raises_device_timeout(camera, monkeypatch):
    def no_frame(self, timeout=0):
        raise SpinnakerException("timed out", -1011)

    monkeypatch.setattr(CameraPtr, "GetNextImage", no_frame)
    with pytest.raises(DeviceTimeout) as info:
        camera.capture_frame()
    assert info.value.__context__ is None


def test_timeout_before_any_frame_hints_at_another_program(camera, monkeypatch):
    # On macOS, SpinView streaming from the camera doesn't stop Init(); grabs
    # just time out (-1011).
    def no_frame(self, timeout=0):
        raise SpinnakerException("timed out", -1011)

    monkeypatch.setattr(CameraPtr, "GetNextImage", no_frame)
    with pytest.raises(DeviceTimeout, match=r"timed out.*\(another program") as info:
        camera.capture_frame()
    assert info.value.__context__ is None


def test_grab_timeout_is_in_milliseconds(camera, monkeypatch, pyspin):
    seen = []
    original = CameraPtr.GetNextImage

    def spy(self, timeout=0):
        seen.append(timeout)
        return original(self, timeout)

    monkeypatch.setattr(CameraPtr, "GetNextImage", spy)
    camera.capture_frame(timeout=2.0)
    assert 1900 <= seen[-1] <= 2000
    camera.capture_frame(timeout=math.inf)
    assert seen[-1] == pyspin.EVENT_TIMEOUT_INFINITE
    camera.capture_frame()
    assert seen[-1] <= camera.capture_timeout * 1000


# --- Images and the stream ----------------------------------------------------


def test_open_keeps_only_the_newest_frame(camera, fake, pyspin):
    stream = pyspin.transport(fake).stream
    assert stream.nodes["StreamBufferHandlingMode"].value == "NewestOnly"


@pytest.mark.parametrize(
    "status",
    [
        "SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE",
        "SPINNAKER_IMAGE_STATUS_MISSING_PACKETS",
        "SPINNAKER_IMAGE_STATUS_CRC_CHECK_FAILED",
    ],
)
def test_an_incomplete_image_is_rejected_whatever_its_status(
    camera, fake, pyspin, status
):
    pyspin.image_status[BufferStatus.INCOMPLETE] = getattr(pyspin, status)
    fake.queue_frames(FrameSpec(status=BufferStatus.INCOMPLETE))
    frame = camera.capture_frame()
    assert frame.metadata["incomplete_frames"] == 1
    assert frame.metadata["missing_trailer"] is False


def test_an_image_missing_its_trailer_is_accepted_and_flagged(camera, fake):
    fake.queue_frames(FrameSpec(status=BufferStatus.MISSING_TRAILER))
    frame = camera.capture_frame()
    assert frame.metadata["missing_trailer"] is True
    assert frame.metadata["incomplete_frames"] == 0


# --- Discovery ----------------------------------------------------------------


def test_discover_lists_attached_cameras(pyspin, monkeypatch):
    monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
    serials = [info.serial_number for info in SpinnakerCamera.discover()]
    assert serials == [MONO, COLOR]
    assert pyspin.system.instances == 0


def test_el_devices_discover_lists_spinnaker_cameras(
    pyspin, monkeypatch, capsys, discover_only
):
    discover_only("spinnaker")
    monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "spinnaker: 2 found" in out
    assert re.search(rf"Blackfly S BFS-U3-51S5M +{MONO}", out)


def test_el_devices_discover_without_pyspin_says_how_to_install(
    monkeypatch, capsys, discover_only
):
    discover_only("spinnaker")
    monkeypatch.setitem(sys.modules, "PySpin", None)
    main(["discover"])
    assert "spinnaker: unavailable" in capsys.readouterr().out


# --- Contracts ----------------------------------------------------------------


class TestPySpinBackend(BackendContract):
    @pytest.fixture
    def make_backend(self):
        return lambda cameras: spinnaker._PySpinBackend(FakePySpin(cameras))


class TestSpinnakerCameraContract(FrameCameraContract):
    @pytest.fixture(autouse=True)
    def pyspin(self, monkeypatch):
        pyspin = FakePySpin([flir_camera(MONO, color=True)])
        # discover() is a classmethod, so it reaches the fake through the loader.
        monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
        return pyspin

    @pytest.fixture
    def make_device(self, pyspin):
        return lambda: SpinnakerCamera(MONO, pyspin=pyspin)

    def test_closing_releases_the_sdk(self, make_device, pyspin):
        with make_device() as camera:
            camera.capture_frame()
            camera.preview_frame()
        assert_released(pyspin)


class TestSpinnakerCameraContractSoftwareTrigger(TestSpinnakerCameraContract):
    """The same rules with every capture software-triggered."""

    @pytest.fixture
    def device(self, make_device):
        with make_device() as camera:
            camera.set_setting(TRIGGER, "Software")
            yield camera
