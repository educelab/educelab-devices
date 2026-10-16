import functools
import time

import numpy as np
import pytest

from contracts import FileCameraContract, FrameCameraContract
from educelab.devices.cameras import (
    EXPOSURE_TIME,
    GAIN,
    ISO,
    PIXEL_FORMAT,
    SimulatedCamera,
    SimulatedFileCamera,
)


class TestSimulatedCamera(FrameCameraContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedCamera


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


def test_capture_takes_the_exposure_time():
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 0.1)
        start = time.monotonic()
        camera.capture_frame()
        assert time.monotonic() - start >= 0.1


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
