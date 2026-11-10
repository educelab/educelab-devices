import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from contracts.device import DeviceContract
from educelab.devices.cameras import (
    CANONICAL_UNITS,
    HEIGHT,
    PIXEL_FORMAT,
    WIDTH,
    CameraFile,
    Frame,
    PreviewGeometry,
    PreviewRegion,
)
from educelab.devices.errors import DeviceTimeout, NotConnected, PreviewError

CENTER = PreviewRegion(0.25, 0.25, 0.5, 0.5)


def assert_preview_frame_matches(camera):
    frame = camera.preview_frame()
    assert isinstance(frame, np.ndarray)
    assert frame.dtype == np.uint8
    assert frame.ndim in (2, 3)
    geometry = camera.preview
    assert frame.shape[:2] == (geometry.height, geometry.width)


class CameraContract(DeviceContract):
    """Rules every `Camera` follows."""

    def test_open_camera_reports_its_vendor(self, device):
        assert device.info.vendor

    def test_settings_are_keyed_by_name(self, device):
        settings = device.settings()
        assert settings
        assert all(name == setting.name for name, setting in settings.items())

    def test_canonical_settings_use_canonical_units(self, device):
        for name, setting in device.settings().items():
            if name in CANONICAL_UNITS:
                assert setting.unit == CANONICAL_UNITS[name], name

    def test_writing_current_values_succeeds(self, device):
        for name, setting in device.settings().items():
            if not setting.read_only and setting.value is not None:
                device.set_setting(name, setting.value)
                assert device.setting(name).value == setting.value

    def test_every_choice_is_accepted(self, device):
        for name, setting in device.settings().items():
            if setting.read_only or setting.choices is None:
                continue
            for choice in setting.choices:
                device.set_setting(name, choice)
                assert device.setting(name).value == choice
            if setting.value is not None:
                device.set_setting(name, setting.value)

    def test_nearest_values_are_accepted(self, device):
        for name, setting in device.settings().items():
            if setting.read_only or isinstance(setting.value, bool | str | None):
                continue
            for direction in ("nearest", "down", "up"):
                value = setting.nearest(setting.value, direction)
                device.set_setting(name, type(setting.value)(value))
            device.set_setting(name, setting.value)

    def test_value_above_maximum_raises(self, device):
        for name, setting in device.settings().items():
            if setting.read_only or setting.maximum is None:
                continue
            with pytest.raises(ValueError):
                device.set_setting(name, setting.maximum + abs(setting.maximum) + 1)
            assert device.setting(name).value == setting.value

    def test_unknown_setting_raises_key_error(self, device):
        with pytest.raises(KeyError):
            device.set_setting("no_such_setting", 1)

    # Preview (ADR 0007)

    def test_open_camera_is_not_previewing(self, device):
        assert device.preview is None

    def test_start_preview_reports_what_it_applied(self, device):
        geometry = device.start_preview()
        assert isinstance(geometry, PreviewGeometry)
        assert device.preview == geometry

    def test_preview_frame_is_displayable_at_the_reported_size(self, device):
        device.start_preview()
        assert_preview_frame_matches(device)

    def test_preview_output_fits_max_size(self, device):
        geometry = device.start_preview(max_size=200)
        assert max(geometry.width, geometry.height) <= 200
        assert_preview_frame_matches(device)

    def test_uncapped_preview_is_at_least_as_large_as_a_capped_one(self, device):
        small = device.start_preview(max_size=64)
        full = device.start_preview(max_size=None)
        assert full.width >= small.width and full.height >= small.height
        assert_preview_frame_matches(device)

    def test_preview_region_lies_within_the_requested_one(self, device):
        geometry = device.start_preview(CENTER, max_size=None)
        assert CENTER.contains(geometry.region)
        assert_preview_frame_matches(device)

    def test_preview_takes_a_region_as_a_tuple(self, device):
        as_tuple = device.start_preview((0.25, 0.25, 0.5, 0.5))
        assert as_tuple == device.start_preview(CENTER)

    def test_start_preview_while_previewing_reconfigures(self, device):
        whole = device.start_preview(max_size=None)
        center = device.start_preview(CENTER, max_size=None)
        assert device.preview == center
        assert center.region != whole.region
        assert_preview_frame_matches(device)

    def test_invalid_preview_request_leaves_preview_as_it_was(self, device):
        geometry = device.start_preview(max_size=100)
        with pytest.raises(ValueError):
            device.start_preview(max_size=0)
        with pytest.raises(TypeError):
            device.start_preview((0.0, 0.0, 1.0))
        assert device.preview == geometry
        assert_preview_frame_matches(device)

    def test_stop_preview_ends_preview(self, device):
        device.start_preview()
        device.stop_preview()
        assert device.preview is None
        with pytest.raises(PreviewError):
            device.preview_frame()

    def test_stop_preview_when_idle_does_nothing(self, device):
        device.stop_preview()
        device.stop_preview()
        assert device.preview is None

    def test_preview_frame_outside_preview_raises(self, device):
        with pytest.raises(PreviewError, match="start_preview"):
            device.preview_frame()

    def test_close_ends_preview(self, make_device):
        camera = make_device()
        with camera:
            camera.start_preview()
        with camera:
            assert camera.preview is None
            with pytest.raises(PreviewError):
                camera.preview_frame()

    def test_preview_frame_waits_for_not_before(self, device):
        device.start_preview()
        device.preview_frame()
        started = time.monotonic()
        device.preview_frame(not_before=datetime.now(UTC) + timedelta(seconds=0.1))
        # A frame taken after not_before can't arrive before it.
        assert time.monotonic() - started >= 0.09

    def test_preview_frame_rejects_naive_not_before(self, device):
        device.start_preview()
        with pytest.raises(ValueError, match="timezone-aware"):
            device.preview_frame(not_before=datetime.now())

    def test_settings_can_change_during_preview(self, device):
        device.start_preview()
        for name, setting in device.settings().items():
            if setting.read_only or setting.choices is None:
                continue
            for choice in setting.choices:
                device.set_setting(name, choice)
                assert_preview_frame_matches(device)
            if setting.value is not None:
                device.set_setting(name, setting.value)
        assert device.preview is not None

    def test_closed_camera_raises_not_connected(self, make_device):
        camera = make_device()
        with pytest.raises(NotConnected):
            camera.settings()


class FrameCameraContract(CameraContract):
    """Rules every `FrameCamera` follows."""

    def test_capture_frame_returns_frame(self, device):
        frame = device.capture_frame()
        assert isinstance(frame, Frame)
        assert isinstance(frame.pixels, np.ndarray)
        assert frame.pixels.ndim in (2, 3)
        assert frame.timestamp.tzinfo is not None

    def test_capture_frame_honors_not_before(self, device):
        ready = datetime.now(UTC)
        assert device.capture_frame(not_before=ready).timestamp >= ready

    def test_capture_frame_times_out_waiting_for_not_before(self, device):
        later = datetime.now(UTC) + timedelta(seconds=10)
        with pytest.raises(DeviceTimeout):
            device.capture_frame(not_before=later, timeout=0.1)

    @pytest.mark.parametrize("timeout", [0.0, -1.0])
    def test_capture_frame_rejects_non_positive_timeout(self, device, timeout):
        with pytest.raises(ValueError, match="timeout"):
            device.capture_frame(timeout=timeout)

    def test_frame_pixel_format_matches_setting(self, device):
        settings = device.settings()
        if PIXEL_FORMAT not in settings:
            pytest.skip("camera has no pixel_format setting")
        assert device.capture_frame().pixel_format == settings[PIXEL_FORMAT].value

    def test_capture_frame_during_preview_raises(self, device):
        device.start_preview()
        with pytest.raises(PreviewError, match="stop_preview"):
            device.capture_frame()

    def test_capture_after_stopping_preview_has_the_capture_size(self, device):
        settings = device.settings()
        device.start_preview(CENTER, max_size=64)
        device.stop_preview()
        frame = device.capture_frame()
        if WIDTH in settings and HEIGHT in settings:
            size = (settings[HEIGHT].value, settings[WIDTH].value)
            assert frame.pixels.shape[:2] == size

    def test_size_settings_describe_captures_during_preview(self, device):
        settings = device.settings()
        if WIDTH not in settings or HEIGHT not in settings:
            pytest.skip("camera reports no frame size")
        device.start_preview(CENTER, max_size=64)
        during = device.settings()
        assert during[WIDTH].value == settings[WIDTH].value
        assert during[HEIGHT].value == settings[HEIGHT].value

    def test_frame_size_matches_setting(self, device):
        settings = device.settings()
        if WIDTH not in settings or HEIGHT not in settings:
            pytest.skip("camera reports no frame size")
        size = (settings[HEIGHT].value, settings[WIDTH].value)
        assert device.capture_frame().pixels.shape[:2] == size


class FileCameraContract(CameraContract):
    """Rules every `FileCamera` follows."""

    def test_capture_files_returns_named_files(self, device):
        files = device.capture_files()
        assert files
        for file in files:
            assert isinstance(file, CameraFile)
            assert file.data
            assert file.suffix

    def test_capture_files_during_preview_raises(self, device):
        device.start_preview()
        with pytest.raises(PreviewError, match="stop_preview"):
            device.capture_files()

    def test_capture_after_stopping_preview_succeeds(self, device):
        device.start_preview()
        device.stop_preview()
        assert device.capture_files()

    def test_captured_file_saves_once(self, device, tmp_path):
        file = device.capture_files()[0]
        path = file.save(tmp_path, "capture")
        assert path.read_bytes() == file.data
        with pytest.raises(FileExistsError):
            file.save(tmp_path, "capture")
