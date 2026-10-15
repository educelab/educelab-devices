import math
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

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        self.capture_args = (not_before, timeout)
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
    assert isinstance(camera.writes[0][1], float)
    camera.set_setting(EXPOSURE_TIME, 0.5)


def test_numpy_scalars_are_written_as_python_numbers(camera):
    camera.set_setting(EXPOSURE_TIME, np.float32(0.5))
    assert type(camera.writes[-1][1]) is float
    assert type(Setting("iso", 100, 0, 6400).validate(np.int64(200))) is int


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
    assert camera.capture_args == (None, camera.capture_timeout)


def test_capture_frame_passes_timeout_through(camera):
    camera.capture_frame(timeout=2.0)
    assert camera.capture_args == (None, 2.0)


def test_capture_frame_accepts_infinite_timeout(camera):
    camera.capture_frame(timeout=math.inf)
    assert camera.capture_args == (None, math.inf)


def test_capture_frame_rejects_nan_timeout(camera):
    with pytest.raises(ValueError, match="timeout"):
        camera.capture_frame(timeout=math.nan)


def test_capture_frame_passes_aware_datetime_through(camera):
    when = datetime(2026, 10, 15, tzinfo=UTC)
    camera.capture_frame(not_before=when)
    assert camera.capture_args == (when, camera.capture_timeout)


def test_capture_frame_rejects_float_not_before(camera):
    with pytest.raises(TypeError, match="datetime"):
        camera.capture_frame(not_before=1_700_000_000.0)


def test_capture_frame_rejects_naive_not_before(camera):
    with pytest.raises(ValueError, match="timezone-aware"):
        camera.capture_frame(not_before=datetime(2026, 10, 15))


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


SHUTTER = Setting(EXPOSURE_TIME, 1 / 125, choices=(1 / 250, 1 / 125, 1 / 60, 1 / 30))
STEPPED = Setting(EXPOSURE_TIME, 0.001, 0.0002, 0.01, step=0.0002)
CONTINUOUS = Setting(EXPOSURE_TIME, 0.001, 0.0001, 0.01)


@pytest.mark.parametrize(
    ("value", "direction", "expected"),
    [
        (1 / 100, "nearest", 1 / 125),
        (1 / 70, "nearest", 1 / 60),
        (1 / 70, "down", 1 / 125),
        (1 / 100, "up", 1 / 60),
        (1 / 60, "down", 1 / 60),
        (1.0, "nearest", 1 / 30),
        (1e-6, "up", 1 / 250),
        (1e-6, "down", 1 / 250),
    ],
)
def test_nearest_choice(value, direction, expected):
    assert SHUTTER.nearest(value, direction) == expected


def test_nearest_breaks_ties_toward_the_lower_value():
    setting = Setting("iso", 100, choices=(100, 200))
    assert setting.nearest(150) == 100


@pytest.mark.parametrize(
    ("value", "direction", "expected"),
    [
        (0.00125, "nearest", 0.0012),
        (0.00131, "nearest", 0.0014),
        (0.0013, "nearest", 0.0012),
        (0.0013, "down", 0.0012),
        (0.00121, "up", 0.0014),
        (0.0012, "up", 0.0012),
        (0.5, "nearest", 0.01),
        (0.0, "nearest", 0.0002),
    ],
)
def test_nearest_step(value, direction, expected):
    assert STEPPED.nearest(value, direction) == pytest.approx(expected)


def test_nearest_continuous_clamps_to_range():
    assert CONTINUOUS.nearest(0.005) == 0.005
    assert CONTINUOUS.nearest(1.0) == 0.01
    assert CONTINUOUS.nearest(0.0, "down") == 0.0001


def test_nearest_result_is_accepted_by_validate():
    for value in (0.00031, 0.0099999, 0.5):
        STEPPED.validate(STEPPED.nearest(value))


def test_nearest_needs_a_numeric_setting():
    with pytest.raises(TypeError):
        Setting("mode", "a", choices=("a", "b")).nearest(1.0)


def test_nearest_rejects_unknown_direction():
    with pytest.raises(ValueError):
        CONTINUOUS.nearest(0.005, "sideways")  # type: ignore[arg-type]


def test_nearest_clamps_to_the_last_step_below_an_off_grid_maximum():
    setting = Setting("s", 0.0, minimum=0, maximum=10, step=3)
    assert setting.nearest(11) == 9
    assert setting.nearest(9.5, "up") == 9
