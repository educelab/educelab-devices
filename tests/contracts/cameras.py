import numpy as np
import pytest

from contracts.device import DeviceContract
from educelab.devices.cameras import CANONICAL_UNITS, PIXEL_FORMAT, CameraFile, Frame
from educelab.devices.errors import NotConnected


class CameraContract(DeviceContract):
    """Rules every `Camera` follows."""

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
            if not setting.read_only:
                device.set_setting(name, setting.value)
                assert device.setting(name).value == setting.value

    def test_every_choice_is_accepted(self, device):
        for name, setting in device.settings().items():
            if setting.read_only or setting.choices is None:
                continue
            for choice in setting.choices:
                device.set_setting(name, choice)
                assert device.setting(name).value == choice
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

    def test_preview_frame_is_displayable(self, device):
        preview = device.preview_frame()
        assert isinstance(preview, np.ndarray)
        assert preview.dtype == np.uint8
        assert preview.ndim in (2, 3)

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

    def test_frame_pixel_format_matches_setting(self, device):
        settings = device.settings()
        if PIXEL_FORMAT not in settings:
            pytest.skip("camera has no pixel_format setting")
        assert device.capture_frame().pixel_format == settings[PIXEL_FORMAT].value


class FileCameraContract(CameraContract):
    """Rules every `FileCamera` follows."""

    def test_capture_files_returns_named_files(self, device):
        files = device.capture_files()
        assert files
        for file in files:
            assert isinstance(file, CameraFile)
            assert file.data
            assert file.suffix

    def test_captured_file_saves_once(self, device, tmp_path):
        file = device.capture_files()[0]
        path = file.save(tmp_path, "capture")
        assert path.read_bytes() == file.data
        with pytest.raises(FileExistsError):
            file.save(tmp_path, "capture")
