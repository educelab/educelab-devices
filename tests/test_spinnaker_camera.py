"""`SpinnakerCamera`, tested against the fake PySpin in `fakes.pyspin`."""

import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from educelab.devices import DeviceInfo
from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT, spinnaker
from educelab.devices.cameras.spinnaker import SpinnakerCamera
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from fakes.pyspin import (
    IMAGE_DATA_INCOMPLETE,
    IMAGE_MISSING_PACKETS,
    IMAGE_MISSING_TRAILER,
    CameraPtr,
    FakeCamera,
    FakeImage,
    FakePySpin,
    FrameSpec,
    SpinnakerException,
)

MONO = "20000001"
COLOR = "20000002"


@pytest.fixture
def pyspin():
    return FakePySpin(
        [
            FakeCamera(MONO, "Blackfly S BFS-U3-51S5M"),
            FakeCamera(COLOR, "Blackfly S BFS-U3-200S6C", color=True),
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
    assert all(not c.initialized for c in pyspin.system.cameras)


# --- Open and close -----------------------------------------------------------


def test_constructor_does_not_touch_the_sdk(pyspin):
    SpinnakerCamera(MONO, pyspin=pyspin)
    assert pyspin.system.get_instance_calls == 0


def test_open_initializes_the_camera(camera, fake):
    assert fake.initialized
    assert fake.init_calls == 1


def test_close_releases_the_camera_and_the_system(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    camera.open()
    camera.close()
    assert fake.deinit_calls == 1
    assert_released(pyspin)


def test_reopening_initializes_again(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with camera:
        pass
    with camera:
        assert fake.initialized
    assert fake.init_calls == 2
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
    fake.tl_stream_nodes["StreamBufferHandlingMode"]._access = pyspin.RO
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with pytest.raises(DeviceError, match="StreamBufferHandlingMode"):
        camera.open()
    assert not camera.is_open
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


# --- Timestamp offset ---------------------------------------------------------


def test_open_measures_the_timestamp_offset(camera, fake):
    assert fake.latch_calls == spinnaker.TIMESTAMP_SAMPLES
    assert camera.timestamp_offset == pytest.approx(fake.device_epoch, abs=0.01)


def test_timestamp_offset_can_be_remeasured(camera, fake):
    fake.device_epoch += 2.0
    camera.measure_timestamp_offset()
    assert camera.timestamp_offset == pytest.approx(fake.device_epoch, abs=0.01)


def test_older_timestamp_node_is_used_as_a_fallback():
    pyspin = FakePySpin([FakeCamera(MONO, timestamp_node="TimestampLatchValue")])
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        expected = pyspin.system.cameras[0].device_epoch
        assert camera.timestamp_offset == pytest.approx(expected, abs=0.01)


# --- Identity and discovery ---------------------------------------------------


def test_info_before_open_names_the_serial(pyspin):
    info = SpinnakerCamera(MONO, pyspin=pyspin).info
    assert info.backend == "spinnaker"
    assert info.serial_number == MONO


def test_info_after_open_has_model_and_firmware(camera):
    assert camera.info == DeviceInfo(
        "spinnaker",
        "Blackfly S BFS-U3-51S5M",
        serial_number=MONO,
        firmware_version="1.0.0.0",
    )


def test_discover_lists_attached_cameras(pyspin, monkeypatch):
    monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
    serials = [info.serial_number for info in SpinnakerCamera.discover()]
    assert serials == [MONO, COLOR]
    assert pyspin.system.instances == 0


def test_repr_names_the_serial(pyspin):
    assert MONO in repr(SpinnakerCamera(MONO, pyspin=pyspin))


# --- Settings -----------------------------------------------------------------


def test_settings_cover_exposure_gain_format_and_auto_modes(camera):
    assert set(camera.settings()) == {
        EXPOSURE_TIME,
        "ExposureAuto",
        GAIN,
        "GainAuto",
        PIXEL_FORMAT,
    }


def test_exposure_time_is_in_seconds(camera):
    exposure = camera.setting(EXPOSURE_TIME)
    assert exposure.value == pytest.approx(0.01)
    assert exposure.minimum == pytest.approx(6e-6)
    assert exposure.maximum == pytest.approx(30.0)
    assert exposure.unit == "s"


def test_exposure_time_is_read_only_while_auto_exposure_runs(camera):
    assert camera.setting("ExposureAuto").value == "Continuous"
    assert camera.setting(EXPOSURE_TIME).read_only
    with pytest.raises(ValueError, match="read-only"):
        camera.set_setting(EXPOSURE_TIME, 0.02)


def test_exposure_time_is_written_in_microseconds(camera, fake):
    camera.set_setting("ExposureAuto", "Off")
    camera.set_setting(EXPOSURE_TIME, 0.02)
    assert fake.nodes["ExposureTime"]._value == pytest.approx(20_000.0)
    assert camera.setting(EXPOSURE_TIME).value == pytest.approx(0.02)


def test_exposure_time_limits_survive_the_unit_conversion(camera, fake):
    camera.set_setting("ExposureAuto", "Off")
    for limit in ("minimum", "maximum"):
        camera.set_setting(EXPOSURE_TIME, getattr(camera.setting(EXPOSURE_TIME), limit))
    assert fake.nodes["ExposureTime"]._value == fake.nodes["ExposureTime"].maximum


def test_gain_is_in_decibels(camera, fake):
    gain = camera.setting(GAIN)
    assert (gain.minimum, gain.maximum, gain.unit) == (0.0, 47.99, "dB")
    camera.set_setting("GainAuto", "Off")
    camera.set_setting(GAIN, 12)
    assert fake.nodes["Gain"]._value == 12.0
    assert camera.setting(GAIN).value == 12.0


def test_auto_modes_offer_off_and_continuous(camera):
    # "Once" reverts to "Off" on its own, so it would never read back.
    assert camera.setting("ExposureAuto").choices == ("Off", "Continuous")
    assert camera.setting("GainAuto").choices == ("Off", "Continuous")


def test_pixel_formats_are_the_ones_the_camera_offers(pyspin):
    with SpinnakerCamera(MONO, pyspin=pyspin) as mono:
        assert mono.setting(PIXEL_FORMAT).choices == ("Mono8", "Mono16")
    with SpinnakerCamera(COLOR, pyspin=pyspin) as color:
        assert color.setting(PIXEL_FORMAT).choices == (
            "RGB8",
            "BayerRG8",
            "BayerRG16",
            "Mono8",
            "Mono16",
        )


def test_set_pixel_format(camera, fake):
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    assert fake.pixel_format.symbolic == "Mono16"
    assert camera.setting(PIXEL_FORMAT).value == "Mono16"


def test_open_replaces_a_pixel_format_the_driver_cannot_decode():
    pyspin = FakePySpin([FakeCamera(MONO, formats=["Mono12Packed", "Mono8"])])
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        assert camera.setting(PIXEL_FORMAT).choices == ("Mono8",)
        assert camera.setting(PIXEL_FORMAT).value == "Mono8"


def test_a_feature_the_camera_lacks_is_not_a_setting(pyspin, fake):
    del fake.nodes["Gain"]
    del fake.nodes["GainAuto"]
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        assert GAIN not in camera.settings()
        assert "GainAuto" not in camera.settings()


def test_a_write_the_camera_refuses_raises_command_rejected(camera, fake, monkeypatch):
    def refuse(value):
        raise SpinnakerException("refused")

    camera.set_setting("GainAuto", "Off")
    monkeypatch.setattr(fake.nodes["Gain"], "SetValue", refuse)
    with pytest.raises(CommandRejected, match="refused") as info:
        camera.set_setting(GAIN, 3.0)
    assert info.value.__context__ is None


# --- Acquisition --------------------------------------------------------------


def test_open_starts_free_running_acquisition(camera, fake):
    assert fake.streaming
    assert fake.nodes["AcquisitionMode"].symbolic == "Continuous"
    assert fake.tl_stream_nodes["StreamBufferHandlingMode"].symbolic == "NewestOnly"
    assert fake.trigger_mode.symbolic == "Off"


def test_open_turns_off_a_trigger_mode_left_on(pyspin, fake):
    fake.initialized = True
    fake.trigger_mode.SetValue(pyspin.TriggerMode_On)
    fake.initialized = False
    with SpinnakerCamera(MONO, pyspin=pyspin):
        assert fake.trigger_mode.symbolic == "Off"


def test_close_ends_acquisition_before_deinit(pyspin, fake):
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


def test_pixel_format_change_pauses_acquisition(camera, fake):
    assert camera.setting(PIXEL_FORMAT).read_only is False
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    assert fake.pixel_format.symbolic == "Mono16"
    assert fake.streaming


def test_failed_pixel_format_change_still_resumes_acquisition(
    camera, fake, monkeypatch
):
    def refuse(value):
        raise SpinnakerException("refused")

    monkeypatch.setattr(fake.pixel_format, "SetIntValue", refuse)
    with pytest.raises(CommandRejected):
        camera.set_setting(PIXEL_FORMAT, "Mono16")
    assert fake.streaming


# --- Capture ------------------------------------------------------------------


def stale(fake, age=1.0):
    """A frame already waiting in a buffer, exposed ``age`` seconds ago."""
    return FrameSpec(timestamp_ns=fake.device_time_ns() - round(age * 1e9))


def test_capture_returns_a_copy_of_the_pixels(camera, fake):
    frame = camera.capture_frame()
    assert frame.pixels.shape == (fake.height, fake.width)
    assert frame.pixels.dtype == np.uint8
    assert frame.pixel_format == "Mono8"
    assert not (frame.pixels == FakeImage.RELEASED_FILL).all()


def test_capture_timestamp_is_host_time(camera):
    before = datetime.now(UTC)
    frame = camera.capture_frame()
    assert before - timedelta(seconds=0.05) <= frame.timestamp
    assert frame.timestamp <= datetime.now(UTC) + timedelta(seconds=0.05)


def test_capture_metadata(camera, fake):
    metadata = camera.capture_frame().metadata
    assert metadata[EXPOSURE_TIME] == pytest.approx(0.01)
    assert metadata[GAIN] == 0.0
    assert metadata["device_timestamp_ns"] > 0
    assert metadata["frame_id"] == fake.frames_grabbed
    assert metadata["retries"] == 0
    assert metadata["stale_frames"] == 0
    assert metadata["incomplete_frames"] == 0
    assert metadata["missing_trailer"] is False


def test_capture_rejects_frames_older_than_not_before(camera, fake):
    fake.queue_frames(stale(fake), stale(fake, 0.5))
    request = time.time()
    frame = camera.capture_frame(not_before=request)
    assert frame.timestamp.timestamp() >= request - 0.01
    assert frame.metadata["stale_frames"] == 2
    assert frame.metadata["retries"] == 2
    assert fake.frames_grabbed == 3


def test_not_before_accepts_a_datetime(camera, fake):
    fake.queue_frames(stale(fake))
    frame = camera.capture_frame(not_before=datetime.now(UTC))
    assert frame.metadata["stale_frames"] == 1


def test_without_not_before_the_newest_frame_is_taken(camera, fake):
    fake.queue_frames(stale(fake))
    assert camera.capture_frame().metadata["stale_frames"] == 0


def test_incomplete_frames_are_rejected_even_when_first(camera, fake):
    # pgs-scan #83: the check was skipped on the frame normally returned.
    fake.queue_frames(
        FrameSpec(status=IMAGE_DATA_INCOMPLETE), FrameSpec(status=IMAGE_MISSING_PACKETS)
    )
    frame = camera.capture_frame()
    assert frame.metadata["incomplete_frames"] == 2
    assert frame.metadata["frame_id"] == 3


def test_missing_trailer_frames_are_accepted_and_flagged(camera, fake):
    fake.queue_frames(FrameSpec(status=IMAGE_MISSING_TRAILER))
    frame = camera.capture_frame()
    assert frame.metadata["missing_trailer"] is True
    assert frame.metadata["incomplete_frames"] == 0


def test_every_grabbed_buffer_is_released(camera, fake):
    # pgs-scan #83: discarded frames leaked their buffers.
    for _ in range(3):
        fake.queue_frames(stale(fake), FrameSpec(status=IMAGE_DATA_INCOMPLETE))
        camera.capture_frame(not_before=time.time())
    assert fake.frames_grabbed == 9
    assert fake.held == []


def test_many_captures_never_exhaust_the_buffer_pool(camera, fake):
    for _ in range(fake.buffer_count * 3):
        fake.queue_frames(stale(fake))
        camera.capture_frame(not_before=time.time())
    assert fake.held == []


def test_retries_are_bounded(camera, fake):
    # Every frame is older than a request in the future, so none will do.
    with pytest.raises(DeviceError, match="no usable frame") as info:
        camera.capture_frame(not_before=time.time() + 60)
    assert fake.frames_grabbed == spinnaker.MAX_ATTEMPTS
    assert fake.held == []
    assert info.value.__context__ is None


def test_offset_is_remeasured_when_a_round_of_retries_runs_out(camera, fake):
    # The camera's counter jumped, so every frame looks 5 s old until the
    # offset is measured again.
    fake.device_epoch += 5.0
    frame = camera.capture_frame(not_before=time.time())
    assert frame.metadata["retries"] == spinnaker.RETRIES_BEFORE_REMEASURE
    assert frame.metadata["timestamp_remeasured"] is True
    assert fake.latch_calls == 2 * spinnaker.TIMESTAMP_SAMPLES
    assert camera.timestamp_offset == pytest.approx(fake.device_epoch, abs=0.01)


def test_no_frame_in_time_raises_device_timeout(camera, fake, monkeypatch):
    def no_frame(self, timeout=0):
        raise SpinnakerException("timed out", -1011)

    monkeypatch.setattr(CameraPtr, "GetNextImage", no_frame)
    with pytest.raises(DeviceTimeout) as info:
        camera.capture_frame()
    assert info.value.__context__ is None


def test_an_expired_timeout_raises_device_timeout(camera, fake):
    fake.queue_frames(stale(fake))
    with pytest.raises(DeviceTimeout):
        camera.capture_frame(not_before=time.time(), timeout=0)
    assert fake.held == []


def test_grab_timeout_covers_the_exposure(camera, fake, monkeypatch):
    seen = []
    original = CameraPtr.GetNextImage

    def spy(self, timeout=0):
        seen.append(timeout)
        return original(self, timeout)

    monkeypatch.setattr(CameraPtr, "GetNextImage", spy)
    camera.set_setting("ExposureAuto", "Off")
    camera.set_setting(EXPOSURE_TIME, 5.0)
    camera.capture_frame()
    assert seen[0] >= 5000


def test_capture_in_each_pixel_format(pyspin):
    with SpinnakerCamera(COLOR, pyspin=pyspin) as camera:
        for fmt, shape, dtype in (
            ("RGB8", (6, 8, 3), np.uint8),
            ("Mono16", (6, 8), np.uint16),
            ("BayerRG8", (6, 8), np.uint8),
        ):
            camera.set_setting(PIXEL_FORMAT, fmt)
            frame = camera.capture_frame()
            assert (frame.pixel_format, frame.pixels.shape) == (fmt, shape)
            assert frame.pixels.dtype == dtype


def test_close_after_captures_releases_the_system(pyspin, fake):
    camera = SpinnakerCamera(MONO, pyspin=pyspin)
    with camera:
        fake.queue_frames(stale(fake))
        camera.capture_frame(not_before=time.time())
        with pytest.raises(DeviceError):
            camera.capture_frame(not_before=time.time() + 60)
    assert_released(pyspin)
