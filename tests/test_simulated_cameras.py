import functools
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from contracts import FileCameraContract, FrameCameraContract
from educelab.devices.cameras import (
    EXPOSURE_TIME,
    GAIN,
    HEIGHT,
    ISO,
    PIXEL_FORMAT,
    SENSOR_HEIGHT,
    SENSOR_WIDTH,
    WIDTH,
    SimulatedCamera,
    SimulatedFileCamera,
)
from educelab.devices.errors import DeviceTimeout


class TestSimulatedCamera(FrameCameraContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedCamera


class TestSimulatedColorCamera(FrameCameraContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedCamera, color=True)


class TestSimulatedAutoCamera(FrameCameraContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedCamera, auto=True)


class TestSimulatedFileCamera(FileCameraContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedFileCamera


class TestSimulatedFileCameraWithoutValues(FileCameraContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedFileCamera, bulb=True, auto_iso=True)


def mean_level(camera: SimulatedCamera) -> float:
    return float(camera.capture_frame().pixels.mean())


def test_longer_exposure_gives_brighter_frame():
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 0.001)
        dim = mean_level(camera)
        camera.set_setting(EXPOSURE_TIME, 0.005)
        assert mean_level(camera) > dim


def test_gain_gives_brighter_frame():
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 0.001)
        dim = mean_level(camera)
        camera.set_setting(GAIN, 12.0)
        assert mean_level(camera) > dim


def test_pixel_format_sets_dtype():
    with SimulatedCamera(width=32, height=24) as camera:
        frame = camera.capture_frame()
        assert frame.pixels.dtype == np.uint8
        assert frame.pixels.shape == (24, 32)
        camera.set_setting(PIXEL_FORMAT, "Mono16")
        assert camera.capture_frame().pixels.dtype == np.uint16


def test_mono_camera_offers_no_rgb():
    with SimulatedCamera() as camera:
        assert "RGB8" not in camera.setting(PIXEL_FORMAT).choices


def test_color_camera_starts_in_rgb8_with_distinct_channels():
    with SimulatedCamera(width=32, height=24, color=True) as camera:
        frame = camera.capture_frame()
        assert frame.pixel_format == "RGB8"
        assert frame.pixels.dtype == np.uint8
        assert frame.pixels.shape == (24, 32, 3)
        red, green, blue = frame.pixels.reshape(-1, 3).mean(axis=0)
        assert red > green > blue
        assert camera.preview_frame().ndim == 3
        camera.set_setting(PIXEL_FORMAT, "Mono8")
        assert camera.capture_frame().pixels.shape == (24, 32)


def test_frame_and_sensor_size_are_reported():
    with SimulatedCamera(width=32, height=24) as camera:
        for name, size in (
            (WIDTH, 32),
            (HEIGHT, 24),
            (SENSOR_WIDTH, 32),
            (SENSOR_HEIGHT, 24),
        ):
            assert camera.setting(name).value == size
            assert camera.setting(name).read_only


def test_auto_modes_are_offered_only_on_request():
    with SimulatedCamera() as camera:
        assert "ExposureAuto" not in camera.settings()
    with SimulatedCamera(auto=True) as camera:
        assert camera.setting("ExposureAuto").choices == ("Off", "Continuous")
        assert camera.setting("GainAuto").value == "Continuous"


def test_auto_exposure_locks_exposure_time_and_varies_it_per_frame():
    with SimulatedCamera(auto=True) as camera:
        assert camera.setting(EXPOSURE_TIME).read_only
        exposures = {camera.capture_frame().metadata[EXPOSURE_TIME] for _ in range(3)}
        assert len(exposures) == 3
        last = camera.setting(EXPOSURE_TIME).value
        camera.set_setting("ExposureAuto", "Off")
        assert camera.setting(EXPOSURE_TIME).value == last
        camera.set_setting(EXPOSURE_TIME, 0.002)
        assert camera.capture_frame().metadata[EXPOSURE_TIME] == 0.002


def test_auto_gain_makes_up_for_a_short_manual_exposure():
    with SimulatedCamera(auto=True) as camera:
        camera.set_setting("ExposureAuto", "Off")
        camera.set_setting(EXPOSURE_TIME, 0.001)
        gains = {camera.capture_frame().metadata[GAIN] for _ in range(3)}
        assert len(gains) == 3
        assert all(gain > 15 for gain in gains)
        assert camera.setting(GAIN).read_only
        camera.set_setting("GainAuto", "Off")
        camera.set_setting(GAIN, 0.0)
        assert camera.capture_frame().metadata[GAIN] == 0.0


def test_capture_takes_the_exposure_time():
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 0.1)
        start = time.monotonic()
        camera.capture_frame()
        assert time.monotonic() - start >= 0.1


def test_capture_waits_for_a_future_not_before():
    with SimulatedCamera() as camera:
        ready = datetime.now(UTC) + timedelta(seconds=0.1)
        assert camera.capture_frame(not_before=ready).timestamp >= ready


def test_capture_times_out_when_exposure_exceeds_timeout():
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 1.0)
        with pytest.raises(DeviceTimeout):
            camera.capture_frame(timeout=0.1)


def test_frame_metadata_records_settings():
    with SimulatedCamera() as camera:
        camera.set_setting(GAIN, 6.0)
        assert camera.capture_frame().metadata[GAIN] == 6.0


def test_file_camera_produces_numbered_pgm_files():
    with SimulatedFileCamera(width=8, height=4) as camera:
        first = camera.capture_files()[0]
        second = camera.capture_files()[0]
    assert (first.name, second.name) == ("SIM_0001.pgm", "SIM_0002.pgm")
    header, pixels = first.data.split(b"\n255\n", 1)
    assert header == b"P5\n8 4"
    assert len(pixels) == 8 * 4


def test_file_camera_offers_iso_choices():
    with SimulatedFileCamera() as camera:
        assert 100 in camera.setting(ISO).choices


def test_file_camera_at_bulb_and_auto_iso_reports_none_until_written():
    with SimulatedFileCamera(bulb=True, auto_iso=True) as camera:
        assert camera.setting(EXPOSURE_TIME).value is None
        assert camera.setting(ISO).value is None
        assert camera.capture_files()
        camera.set_setting(EXPOSURE_TIME, 0.5)
        camera.set_setting(ISO, 400)
        assert camera.setting(EXPOSURE_TIME).value == 0.5
        assert camera.setting(ISO).value == 400
