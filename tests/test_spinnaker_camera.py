"""`SpinnakerCamera`, tested against the fake PySpin in `fakes.pyspin`."""

import pytest

from educelab.devices import DeviceInfo
from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT, spinnaker
from educelab.devices.cameras.spinnaker import SpinnakerCamera
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
)
from fakes.pyspin import FakeCamera, FakePySpin, SpinnakerException

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


def test_open_keeps_only_the_newest_buffer(camera, fake):
    mode = fake.tl_stream_nodes["StreamBufferHandlingMode"]
    assert mode.symbolic == "NewestOnly"
    assert fake.nodes["AcquisitionMode"].symbolic == "Continuous"


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
