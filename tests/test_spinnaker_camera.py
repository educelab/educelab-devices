"""`SpinnakerCamera`, tested against the fake PySpin in `fakes.pyspin`."""

import math
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import numpy as np
import pytest

from contracts import FrameCameraContract
from educelab.devices import DeviceInfo
from educelab.devices.app.cli import main
from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT, spinnaker
from educelab.devices.cameras.spinnaker import (
    THROUGHPUT_LIMIT,
    TRIGGER,
    SpinnakerCamera,
)
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from fakes.pyspin import (
    SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE,
    SPINNAKER_IMAGE_STATUS_MISSING_PACKETS,
    SPINNAKER_IMAGE_STATUS_MISSING_TRAILER,
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


def test_settings_cover_exposure_gain_format_auto_modes_and_throughput(camera):
    assert set(camera.settings()) == {
        EXPOSURE_TIME,
        "ExposureAuto",
        GAIN,
        "GainAuto",
        PIXEL_FORMAT,
        THROUGHPUT_LIMIT,
        TRIGGER,
        "current_throughput",
        "resulting_frame_rate",
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


def test_frame_timestamp_is_the_start_of_exposure(camera, fake):
    # The camera stamps the end of exposure; the driver reports the start.
    end_ns = fake.device_time_ns()
    fake.queue_frames(FrameSpec(timestamp_ns=end_ns))
    frame = camera.capture_frame()
    start = camera.timestamp_offset + end_ns / 1e9 - 0.01
    assert frame.timestamp.timestamp() == pytest.approx(start, abs=1e-6)
    assert frame.metadata["device_timestamp_ns"] == end_ns


def test_a_frame_exposed_partly_before_not_before_is_stale(camera, fake):
    # Its exposure ends 5 ms after the request but began 5 ms before it.
    fake.queue_frames(FrameSpec(timestamp_ns=fake.device_time_ns() + 5_000_000))
    request = datetime.now(UTC)
    frame = camera.capture_frame(not_before=request)
    assert frame.metadata["stale_frames"] == 1
    assert frame.timestamp >= request


def test_capture_rejects_frames_older_than_not_before(camera, fake):
    fake.queue_frames(stale(fake), stale(fake, 0.5))
    request = datetime.now(UTC)
    frame = camera.capture_frame(not_before=request)
    assert frame.timestamp >= request
    assert frame.metadata["stale_frames"] == 2
    assert frame.metadata["retries"] == 2
    assert fake.frames_grabbed == 3


def test_a_future_not_before_waits_for_a_later_frame(camera, fake):
    later = datetime.now(UTC) + timedelta(seconds=0.05)
    frame = camera.capture_frame(not_before=later, timeout=1.0)
    assert frame.timestamp >= later
    assert fake.frames_grabbed == 1


def test_a_not_before_past_the_timeout_fails_without_waiting(camera, fake):
    started = time.monotonic()
    with pytest.raises(DeviceTimeout, match="not_before"):
        camera.capture_frame(
            not_before=datetime.now(UTC) + timedelta(seconds=60), timeout=1.0
        )
    assert time.monotonic() - started < 0.5
    assert fake.frames_grabbed == 0


def test_without_not_before_the_newest_frame_is_taken(camera, fake):
    fake.queue_frames(stale(fake))
    assert camera.capture_frame().metadata["stale_frames"] == 0


def test_incomplete_frames_are_rejected_even_when_first(camera, fake):
    # pgs-scan #83: the check was skipped on the frame normally returned.
    fake.queue_frames(
        FrameSpec(status=SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE),
        FrameSpec(status=SPINNAKER_IMAGE_STATUS_MISSING_PACKETS),
    )
    frame = camera.capture_frame()
    assert frame.metadata["incomplete_frames"] == 2
    assert frame.metadata["frame_id"] == 3


def test_missing_trailer_frames_are_accepted_and_flagged(camera, fake):
    fake.queue_frames(FrameSpec(status=SPINNAKER_IMAGE_STATUS_MISSING_TRAILER))
    frame = camera.capture_frame()
    assert frame.metadata["missing_trailer"] is True
    assert frame.metadata["incomplete_frames"] == 0


def test_every_grabbed_buffer_is_released(camera, fake):
    # pgs-scan #83: discarded frames leaked their buffers.
    for _ in range(3):
        fake.queue_frames(
            stale(fake), FrameSpec(status=SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE)
        )
        camera.capture_frame(not_before=datetime.now(UTC))
    assert fake.frames_grabbed == 9
    assert fake.held == []


def test_many_captures_never_exhaust_the_buffer_pool(camera, fake):
    for _ in range(fake.buffer_count * 3):
        fake.queue_frames(stale(fake))
        camera.capture_frame(not_before=datetime.now(UTC))
    assert fake.held == []


def test_retries_are_bounded_by_the_timeout(camera, fake):
    # pgs-scan #83: its stale-frame loop could run forever.
    fake.queue_frames(*(stale(fake) for _ in range(100_000)))
    with pytest.raises(DeviceTimeout, match="stale") as info:
        camera.capture_frame(not_before=datetime.now(UTC), timeout=0.05)
    assert 0 < fake.frames_grabbed < 100_000
    assert fake.held == []
    assert info.value.__context__ is None


def test_offset_is_remeasured_when_a_round_of_retries_runs_out(camera, fake):
    # The camera's counter jumped, so every frame looks 5 s old until the
    # offset is measured again.
    fake.device_epoch += 5.0
    frame = camera.capture_frame(not_before=datetime.now(UTC))
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


def test_timeout_before_any_frame_hints_at_another_program(camera, monkeypatch):
    # On macOS, SpinView streaming from the camera doesn't stop Init(); grabs
    # just time out (-1011).
    def no_frame(self, timeout=0):
        raise SpinnakerException("timed out", -1011)

    monkeypatch.setattr(CameraPtr, "GetNextImage", no_frame)
    with pytest.raises(DeviceTimeout, match=r"timed out.*\(another program") as info:
        camera.capture_frame()
    assert info.value.__context__ is None


def test_timeout_after_a_frame_has_no_hint(camera, fake):
    camera.capture_frame()
    fake.buffer_count = 0
    with pytest.raises(DeviceTimeout) as info:
        camera.capture_frame()
    assert "another program" not in str(info.value)


def test_grab_timeout_is_the_time_left(camera, monkeypatch, pyspin):
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
        camera.capture_frame(not_before=datetime.now(UTC))
        fake.queue_frames(*(stale(fake) for _ in range(100_000)))
        with pytest.raises(DeviceTimeout):
            camera.capture_frame(not_before=datetime.now(UTC), timeout=0.02)
    assert_released(pyspin)


# --- Preview ------------------------------------------------------------------


def test_preview_is_eight_bit(camera):
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    preview = camera.preview_frame()
    assert preview.dtype == np.uint8
    assert preview.shape == (6, 8)


def test_preview_scales_sixteen_bit_frames_down(camera, fake):
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    full = camera.capture_frame().pixels
    preview = camera.preview_frame()
    # Consecutive frames differ only by frame number, here at most 1 count.
    assert abs(int(preview[0, 0]) - int(full[0, 0] >> 8)) <= 1


def test_preview_of_a_large_sensor_is_reduced():
    pyspin = FakePySpin([FakeCamera(MONO, width=4000, height=3000)])
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        preview = camera.preview_frame()
    assert max(preview.shape) <= spinnaker.PREVIEW_MAX_SIZE
    assert preview.shape == (750, 1000)


def test_preview_releases_its_buffer(camera, fake):
    camera.preview_frame()
    assert fake.held == []


# --- Contracts ----------------------------------------------------------------


class TestSpinnakerCameraContract(FrameCameraContract):
    auto = True
    camera_options: ClassVar[dict[str, bool]] = {}

    @pytest.fixture(autouse=True)
    def pyspin(self, monkeypatch):
        camera = FakeCamera(MONO, color=True, auto=self.auto, **self.camera_options)
        pyspin = FakePySpin([camera])
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


class TestSpinnakerCameraContractManualExposure(TestSpinnakerCameraContract):
    """The same rules with auto exposure and gain off, so their ranges apply."""

    auto = False


class TestSpinnakerCameraContractThroughputMode(TestSpinnakerCameraContract):
    """The same rules on a camera whose throughput limit has a mode and only takes
    writes while stopped."""

    camera_options: ClassVar[dict[str, bool]] = {
        "throughput_mode": True,
        "throughput_while_streaming": False,
    }


# --- el-devices discover ------------------------------------------------------


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


# --- Throughput limit ---------------------------------------------------------

MB = 1_000_000


def test_throughput_limit_is_in_megabytes_per_second(camera, fake):
    limit = camera.setting(THROUGHPUT_LIMIT)
    assert limit.value == fake.THROUGHPUT_MAX / MB
    assert limit.minimum == fake.THROUGHPUT_MIN / MB
    assert limit.maximum == fake.THROUGHPUT_MAX / MB
    assert limit.step == fake.THROUGHPUT_INC / MB
    assert limit.unit == "MB/s"
    assert not limit.read_only


def test_throughput_limit_is_written_in_bytes_per_second(camera, fake):
    camera.set_setting(THROUGHPUT_LIMIT, 80)
    assert fake.throughput_limit._value == 80 * MB
    assert camera.setting(THROUGHPUT_LIMIT).value == 80.0


def test_throughput_limit_snaps_to_the_nodes_increment(camera, fake):
    # 80.0041 MB/s is 4100 B/s past 80 MB/s; the nearest step of 8000 B/s is up.
    camera.set_setting(THROUGHPUT_LIMIT, 80.0041)
    assert fake.throughput_limit._value == 80 * MB + 8_000
    limit = camera.setting(THROUGHPUT_LIMIT)
    assert limit.nearest(80.0041) == pytest.approx(limit.value)


def test_throughput_limit_outside_its_range_raises(camera, fake):
    with pytest.raises(ValueError):
        camera.set_setting(THROUGHPUT_LIMIT, fake.THROUGHPUT_MAX / MB + 1)
    with pytest.raises(ValueError):
        camera.set_setting(THROUGHPUT_LIMIT, 1.0)


def test_open_leaves_the_throughput_limit_alone(pyspin, fake):
    fake.throughput_limit._value = 123 * MB
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        assert camera.setting(THROUGHPUT_LIMIT).value == 123.0
    assert fake.throughput_limit._value == 123 * MB


def test_throughput_limit_mode_off_reads_as_none_and_writing_turns_it_on():
    pyspin = FakePySpin([FakeCamera(MONO, throughput_mode=True)])
    fake = pyspin.system.cameras[0]
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        assert camera.setting(THROUGHPUT_LIMIT).value is None
        camera.set_setting(THROUGHPUT_LIMIT, 80)
        assert fake.throughput_limit_mode.symbolic == "On"
        assert camera.setting(THROUGHPUT_LIMIT).value == 80.0


def test_throughput_limit_pauses_acquisition_if_it_must():
    pyspin = FakePySpin([FakeCamera(MONO, throughput_while_streaming=False)])
    fake = pyspin.system.cameras[0]
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        camera.set_setting(THROUGHPUT_LIMIT, 80)
        assert fake.throughput_limit._value == 80 * MB
        assert fake.streaming
        camera.capture_frame()


def test_a_camera_without_a_throughput_limit_lists_none_of_it():
    pyspin = FakePySpin([FakeCamera(MONO, throughput=False)])
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        names = set(camera.settings())
    assert not names & {THROUGHPUT_LIMIT, "current_throughput", "resulting_frame_rate"}


def test_current_throughput_and_frame_rate_are_read_only(camera, fake):
    current = camera.setting("current_throughput")
    rate = camera.setting("resulting_frame_rate")
    assert (current.unit, rate.unit) == ("MB/s", "Hz")
    assert current.read_only and rate.read_only
    assert rate.value == pytest.approx(fake.MAX_FRAME_RATE)
    with pytest.raises(ValueError, match="read-only"):
        camera.set_setting("resulting_frame_rate", 10.0)


# --- Software trigger ---------------------------------------------------------


@pytest.fixture
def triggered(camera, fake):
    camera.set_setting(TRIGGER, "Software")
    # Leave out the frame and trigger used to prime the camera.
    fake.trigger_calls = 0
    fake.frames_grabbed = 0
    fake.grab_timeouts.clear()
    return camera


def test_trigger_is_off_after_open(camera):
    setting = camera.setting(TRIGGER)
    assert setting.value == "Off"
    assert setting.choices == ("Off", "Software")


def test_software_trigger_sets_the_trigger_nodes(triggered, fake):
    assert fake.trigger_mode.symbolic == "On"
    assert fake.trigger_source.symbolic == "Software"
    assert fake.trigger_selector.symbolic == "FrameStart"
    assert fake.streaming
    assert triggered.setting(TRIGGER).value == "Software"


def test_a_trigger_left_on_a_hardware_line_reads_none(camera, fake, pyspin):
    with camera._acquisition_paused():
        fake.trigger_source.SetValue(pyspin.TriggerSource_Line0)
        fake.trigger_mode.SetValue(pyspin.TriggerMode_On)
    assert camera.setting(TRIGGER).value is None


def test_trigger_off_free_runs_again(triggered, fake):
    triggered.set_setting(TRIGGER, "Off")
    assert fake.trigger_mode.symbolic == "Off"
    triggered.capture_frame()
    assert fake.trigger_calls == 0


def test_reopening_turns_the_trigger_off(pyspin, fake):
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        camera.set_setting(TRIGGER, "Software")
    fake.trigger_calls = 0
    with SpinnakerCamera(MONO, pyspin=pyspin) as camera:
        assert camera.setting(TRIGGER).value == "Off"
        camera.capture_frame()
    assert fake.trigger_calls == 0


def test_a_triggered_capture_fires_once_and_returns_that_frame(triggered, fake):
    request = datetime.now(UTC)
    frame = triggered.capture_frame(not_before=request)
    assert fake.trigger_calls == 1
    assert frame.timestamp >= request
    assert frame.metadata["retries"] == 0


def test_frames_waiting_before_a_trigger_are_discarded(triggered, fake):
    fake.queue_frames(stale(fake), stale(fake, 0.5))
    frame = triggered.capture_frame()
    assert fake.frames_grabbed == 3
    assert frame.metadata["frame_id"] == 3
    assert frame.metadata["stale_frames"] == 0
    assert fake.held == []


def test_a_stale_frame_arriving_after_the_trigger_is_not_returned(
    triggered, fake, monkeypatch
):
    # E.g. a frame from an earlier, timed-out capture, still in flight.
    discard = SpinnakerCamera._discard_waiting_frames
    monkeypatch.setattr(
        SpinnakerCamera,
        "_discard_waiting_frames",
        lambda self: (discard(self), fake.queue_frames(stale(fake))),
    )
    request = datetime.now(UTC)
    frame = triggered.capture_frame()
    assert frame.timestamp >= request
    assert frame.metadata["stale_frames"] == 1


def test_an_incomplete_triggered_frame_is_triggered_again(triggered, fake):
    fake.queue_frames(FrameSpec(status=SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE))
    frame = triggered.capture_frame()
    assert frame.metadata["incomplete_frames"] == 1
    assert fake.trigger_calls == 2
    assert fake.held == []


def test_trigger_then_capture_collects_without_firing_again(triggered, fake):
    triggered.trigger()
    triggered.capture_frame()
    assert fake.trigger_calls == 1


def test_a_trigger_fired_before_not_before_is_fired_again(triggered, fake):
    triggered.trigger()
    later = datetime.now(UTC) + timedelta(seconds=0.02)
    frame = triggered.capture_frame(not_before=later, timeout=1.0)
    assert fake.trigger_calls == 2
    assert frame.timestamp >= later


def test_a_trigger_in_the_same_clock_tick_as_not_before_counts(
    triggered, fake, monkeypatch
):
    # Windows' clock on Python 3.12 ticks every 15.6 ms, so not_before and the
    # trigger can read the same tick, with not_before rounded just after it.
    tick = time.time()
    monkeypatch.setattr(fake, "clock", lambda: tick)
    monkeypatch.setattr("educelab.devices.cameras.spinnaker.time.time", lambda: tick)
    triggered.trigger()
    triggered.capture_frame(
        not_before=datetime.fromtimestamp(tick, UTC) + timedelta(microseconds=1)
    )
    assert fake.trigger_calls == 1


def test_trigger_outside_software_trigger_mode_raises(camera):
    with pytest.raises(DeviceError, match="software-trigger"):
        camera.trigger()


def test_an_unanswered_trigger_times_out(triggered, fake):
    fake.respond_to_triggers = False
    with pytest.raises(DeviceTimeout):
        triggered.capture_frame(timeout=0.05)
    assert fake.held == []


def test_a_missed_trigger_is_fired_again(triggered, fake):
    fake.ignore_triggers = 1
    frame = triggered.capture_frame(timeout=5.0)
    assert fake.trigger_calls == 2
    assert frame.metadata["retriggers"] == 1
    # Each wait is the exposure plus a few trigger turnarounds, not the timeout.
    exposure = triggered.setting(EXPOSURE_TIME).value
    turnaround = triggered._trigger_turnaround
    wait = exposure + spinnaker._RETRIGGER_TURNAROUNDS * turnaround
    assert fake.grab_timeouts[-2:] == [math.ceil(wait * 1000)] * 2


def test_switching_to_software_trigger_fires_until_the_camera_answers(camera, fake):
    fake.ignore_triggers = 2
    camera.set_setting(TRIGGER, "Software")
    assert fake.trigger_calls == 3
    assert fake.held == []
    frame = camera.capture_frame(timeout=5.0)
    assert frame.metadata["retriggers"] == 0


def test_a_camera_that_never_answers_a_trigger_fails_the_switch(camera, fake):
    fake.respond_to_triggers = False
    camera.capture_timeout = 0.05
    with pytest.raises(DeviceTimeout, match="answered none"):
        camera.set_setting(TRIGGER, "Software")


def test_a_pause_in_trigger_mode_primes_the_trigger_again(triggered, fake):
    fake.ignore_triggers = 1
    triggered.set_setting(PIXEL_FORMAT, "Mono16")
    assert fake.trigger_calls == 2
    assert triggered.capture_frame(timeout=5.0).metadata["retriggers"] == 0


def test_preview_in_software_trigger_mode_fires_a_trigger(triggered, fake):
    triggered.preview_frame()
    assert fake.trigger_calls == 1


class TestSpinnakerCameraContractSoftwareTrigger(TestSpinnakerCameraContract):
    """The same rules with every capture software-triggered."""

    @pytest.fixture
    def device(self, make_device):
        with make_device() as camera:
            camera.set_setting(TRIGGER, "Software")
            yield camera
