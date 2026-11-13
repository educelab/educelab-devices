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
    PreviewRegion,
    SimulatedCamera,
    SimulatedFileCamera,
    SimulatedFocusDrive,
)
from educelab.devices.cameras.simulated import BLUR_STEPS_PER_PX
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


class TestSimulatedChartCamera(FrameCameraContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(
            SimulatedCamera, 320, 240, pattern="chart", focus=SimulatedFocusDrive()
        )


class TestSimulatedFileCamera(FileCameraContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedFileCamera


class TestSimulatedChartFileCamera(FileCameraContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedFileCamera, 320, 240, pattern="chart")


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
        camera.start_preview()
        assert camera.preview_frame().pixels.ndim == 3
        camera.stop_preview()
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


def test_auto_gain_varies_per_frame_when_exposure_is_manual():
    with SimulatedCamera(auto=True) as camera:
        camera.set_setting("ExposureAuto", "Off")
        assert camera.setting(EXPOSURE_TIME).value == 0.01
        gains = {camera.capture_frame().metadata[GAIN] for _ in range(5)}
        assert len(gains) == 5
        assert all(gain > 0 for gain in gains)
        assert camera.setting(GAIN).read_only
        camera.set_setting("GainAuto", "Off")
        camera.set_setting(GAIN, 0.0)
        assert camera.capture_frame().metadata[GAIN] == 0.0


def test_with_both_auto_modes_exposure_does_the_work():
    with SimulatedCamera(auto=True) as camera:
        assert {camera.capture_frame().metadata[GAIN] for _ in range(3)} == {0.0}


def test_auto_mode_leaves_the_noise_unchanged():
    with SimulatedCamera(seed=3) as manual, SimulatedCamera(seed=3, auto=True) as auto:
        manual.capture_frame()
        auto.capture_frame()
        auto.set_setting("ExposureAuto", "Off")
        auto.set_setting("GainAuto", "Off")
        auto.set_setting(EXPOSURE_TIME, 0.01)
        auto.set_setting(GAIN, 0.0)
        assert np.array_equal(
            manual.capture_frame().pixels, auto.capture_frame().pixels
        )


def test_sensor_can_be_larger_than_the_frame():
    with SimulatedCamera(width=32, height=24, sensor_size=(40, 30)) as camera:
        assert (camera.setting(WIDTH).value, camera.setting(HEIGHT).value) == (32, 24)
        assert camera.setting(SENSOR_WIDTH).value == 40
        assert camera.setting(SENSOR_HEIGHT).value == 30
        assert camera.capture_frame().pixels.shape == (24, 32)
    with pytest.raises(ValueError, match="sensor_size"):
        SimulatedCamera(width=32, height=24, sensor_size=(16, 24))


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


def test_preview_shows_the_requested_part_of_the_frame():
    # The pattern brightens to the bottom right, so the bottom-right quarter
    # is brighter than the whole frame.
    with SimulatedCamera(width=64, height=48) as camera:
        camera.start_preview(max_size=None)
        whole = camera.preview_frame().pixels.mean()
        geometry = camera.start_preview((0.5, 0.5, 0.5, 0.5), max_size=None)
        assert (geometry.width, geometry.height) == (32, 24)
        assert camera.preview_frame().pixels.mean() > whole


def test_preview_of_a_cropped_sensor_reports_the_frame_area():
    with SimulatedCamera(width=60, height=40, sensor_size=(80, 60)) as camera:
        geometry = camera.start_preview(max_size=None)
        assert geometry.region == PreviewRegion(10 / 80, 10 / 60, 60 / 80, 40 / 60)
        assert (geometry.width, geometry.height) == (60, 40)


def test_preview_follows_exposure_changes():
    with SimulatedCamera() as camera:
        camera.start_preview(max_size=64)
        camera.set_setting(EXPOSURE_TIME, 0.001)
        dim = camera.preview_frame().pixels.mean()
        camera.set_setting(EXPOSURE_TIME, 0.005)
        assert camera.preview_frame().pixels.mean() > dim


def detail(pixels) -> float:
    """Mean difference between neighbouring pixels: high where it's sharp."""
    return float(np.abs(np.diff(pixels.astype(np.float64), axis=1)).mean())


def test_the_chart_has_single_pixel_detail_at_full_resolution():
    with SimulatedCamera(640, 480, pattern="chart") as camera:
        whole = camera.capture_frame().pixels
        # The 1-px bars, top left: columns alternate dark and light.
        bars = whole[20:60, 20:60].astype(int)
        assert (np.abs(np.diff(bars, axis=1)).mean()) > 50
        # Previewed at 100%, the same pixels; reduced, the bars alias away.
        camera.start_preview(PreviewRegion(0.0, 0.0, 0.25, 0.25))
        assert detail(camera.preview_frame().pixels) > 20
        camera.start_preview(max_size=80)
        full = camera.preview
        assert full.scale < 0.2


def test_the_chart_saturates_its_patch_and_not_much_else():
    with SimulatedCamera(640, 480, pattern="chart") as camera:
        pixels = camera.capture_frame().pixels
        clipped = pixels == 255
        assert 0.002 < clipped.mean() < 0.05
        # The patch is near the bottom right corner.
        rows, cols = np.nonzero(clipped)
        assert np.median(rows) > 0.75 * 480 and np.median(cols) > 0.75 * 640


def test_chart_frames_differ_so_a_live_preview_shows():
    with SimulatedCamera(320, 240, pattern="chart") as camera:
        camera.start_preview()
        first = camera.preview_frame().pixels.astype(int)
        second = camera.preview_frame().pixels.astype(int)
        assert 0 < np.abs(first - second).mean() < 10


def test_a_focus_drive_blurs_the_image_away_from_sharp():
    drive = SimulatedFocusDrive(travel_steps=1000, start=500, sharp_at=500)
    with SimulatedCamera(320, 240, pattern="chart", focus=drive) as camera, drive:
        camera.start_preview()
        sharp = detail(camera.preview_frame().pixels)
        drive.step(4 * BLUR_STEPS_PER_PX)
        blurred = detail(camera.preview_frame().pixels)
        drive.step(-4 * BLUR_STEPS_PER_PX)
        again = detail(camera.preview_frame().pixels)
        assert blurred < sharp / 3
        assert again == pytest.approx(sharp, rel=0.1)


def test_a_focus_drive_blurs_the_file_camera_too():
    drive = SimulatedFocusDrive(start=0, sharp_at=1000)
    with SimulatedFileCamera(320, 240, pattern="chart", focus=drive) as camera:
        camera.start_preview()
        assert detail(camera.preview_frame().pixels) < 5


def test_an_unknown_pattern_is_refused():
    with pytest.raises(ValueError, match="pattern"):
        SimulatedCamera(pattern="stripes")
    with pytest.raises(ValueError, match="pattern"):
        SimulatedFileCamera(pattern="stripes")


def test_sharp_at_must_lie_within_the_travel():
    with pytest.raises(ValueError, match="sharp_at"):
        SimulatedFocusDrive(travel_steps=100, start=50, sharp_at=101)
