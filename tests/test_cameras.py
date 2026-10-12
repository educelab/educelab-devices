from collections.abc import Mapping
from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt
import pytest

from educelab.devices import DeviceInfo
from educelab.devices.cameras import (
    EXPOSURE_TIME,
    CameraFile,
    FileCamera,
    Frame,
    FrameCamera,
    Setting,
    SettingValue,
)
from educelab.devices.errors import NotConnected


class StubCamera(FrameCamera):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, SettingValue] = {EXPOSURE_TIME: 0.01, "mode": "a"}
        self.writes: list[tuple[str, SettingValue]] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="StubCamera")

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _settings(self) -> Mapping[str, Setting]:
        return {
            EXPOSURE_TIME: Setting(
                EXPOSURE_TIME, self.values[EXPOSURE_TIME], 1e-5, 1.0, unit="s"
            ),
            "mode": Setting("mode", self.values["mode"], choices=("a", "b")),
            "serial": Setting("serial", "123", read_only=True),
        }

    def _set_setting(self, name: str, value: SettingValue) -> None:
        self.writes.append((name, value))
        self.values[name] = value

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        return np.zeros((4, 4), dtype=np.uint8)

    def _capture_frame(self) -> Frame:
        return Frame(np.zeros((4, 4), np.uint16), "Mono16", datetime.now(UTC))


class StubFileCamera(FileCamera, StubCamera):
    def _capture_files(self) -> list[CameraFile]:
        return [CameraFile("DSC_0001.NEF", b"raw"), CameraFile("DSC_0001.JPG", b"jpg")]


@pytest.fixture
def camera():
    with StubCamera() as cam:
        yield cam


def test_set_setting_writes_valid_value(camera):
    camera.set_setting(EXPOSURE_TIME, 0.5)
    assert camera.setting(EXPOSURE_TIME).value == 0.5


def test_int_is_accepted_for_float_setting(camera):
    camera.set_setting(EXPOSURE_TIME, 1)
    assert camera.writes == [(EXPOSURE_TIME, 1)]


@pytest.mark.parametrize("value", [2.0, 0.0, float("nan")])
def test_out_of_range_setting_raises_without_writing(camera, value):
    with pytest.raises(ValueError):
        camera.set_setting(EXPOSURE_TIME, value)
    assert camera.writes == []


@pytest.mark.parametrize("value", ["fast", True])
def test_wrong_type_setting_raises(camera, value):
    with pytest.raises(TypeError):
        camera.set_setting(EXPOSURE_TIME, value)


def test_choice_setting_rejects_other_values(camera):
    camera.set_setting("mode", "b")
    with pytest.raises(ValueError, match="not a choice"):
        camera.set_setting("mode", "c")


def test_read_only_setting_raises(camera):
    with pytest.raises(ValueError, match="read-only"):
        camera.set_setting("serial", "456")


def test_unknown_setting_raises_key_error(camera):
    with pytest.raises(KeyError, match="iso"):
        camera.set_setting("iso", 100)
    with pytest.raises(KeyError):
        camera.setting("iso")


def test_settings_returns_every_setting(camera):
    assert set(camera.settings()) == {EXPOSURE_TIME, "mode", "serial"}


def test_camera_methods_need_open_device():
    cam = StubCamera()
    for call in (cam.settings, cam.preview_frame, cam.capture_frame):
        with pytest.raises(NotConnected):
            call()
    with pytest.raises(NotConnected):
        cam.set_setting(EXPOSURE_TIME, 0.1)


def test_capture_frame_returns_frame(camera):
    assert camera.capture_frame().pixels.shape == (4, 4)


def test_capture_files_returns_every_file():
    with StubFileCamera() as cam:
        assert [f.name for f in cam.capture_files()] == ["DSC_0001.NEF", "DSC_0001.JPG"]


def test_camera_file_save_keeps_camera_suffix(tmp_path):
    path = CameraFile("DSC_0001.NEF", b"raw").save(tmp_path, "scan_001")
    assert path == tmp_path / "scan_001.NEF"
    assert path.read_bytes() == b"raw"


def test_camera_file_save_refuses_to_overwrite(tmp_path):
    (tmp_path / "scan_001.NEF").write_bytes(b"original")
    with pytest.raises(FileExistsError, match="overwrite=True"):
        CameraFile("DSC_0001.NEF", b"new").save(tmp_path, "scan_001")
    assert (tmp_path / "scan_001.NEF").read_bytes() == b"original"


def test_camera_file_save_overwrites_when_asked(tmp_path):
    (tmp_path / "scan_001.NEF").write_bytes(b"original")
    CameraFile("DSC_0001.NEF", b"new").save(tmp_path, "scan_001", overwrite=True)
    assert (tmp_path / "scan_001.NEF").read_bytes() == b"new"


@pytest.mark.parametrize("stem", ["", "sub/scan", "../scan"])
def test_camera_file_save_rejects_path_stems(tmp_path, stem):
    with pytest.raises(ValueError):
        CameraFile("DSC_0001.NEF", b"raw").save(tmp_path, stem)
