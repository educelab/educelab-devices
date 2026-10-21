import dataclasses

import pytest

from educelab.devices.app.cli import main
from educelab.devices.cameras import gphoto2
from fakes.gphoto2 import NIKON_WIDGETS, FakeCamera, FakeLibrary


@pytest.fixture
def attach(monkeypatch):
    monkeypatch.setattr(gphoto2, "EVENT_POLL_S", 0)
    monkeypatch.setattr(gphoto2, "_wait", lambda seconds: None)

    def use(*cameras):
        library = FakeLibrary(*cameras)
        monkeypatch.setattr(gphoto2, "_default_library", lambda: library)
        return cameras

    return use


def test_lists_the_camera_and_its_settings(attach, capsys):
    attach(FakeCamera())
    assert main(["dslr"]) == 0
    out = capsys.readouterr().out
    assert "Nikon Z7_2 at usb:020,007" in out
    assert "exposure_time = 0.01667 s" in out
    assert "imagequality = 'NEF (Raw)'" in out
    assert "focus drive: yes" in out


def test_reports_a_camera_without_a_focus_drive(attach, capsys):
    widgets = [w for w in NIKON_WIDGETS if w.name != "manualfocusdrive"]
    attach(FakeCamera(widgets=widgets))
    assert main(["dslr"]) == 0
    assert "focus drive: no" in capsys.readouterr().out


def test_sets_the_nearest_exposure_and_captures_to_a_directory(
    attach, capsys, tmp_path
):
    (camera,) = attach(FakeCamera(suffixes=(".NEF", ".JPG")))
    assert main(["dslr", "--exposure", "0.4", "--capture", str(tmp_path)]) == 0
    assert camera.widgets["shutterspeed"].value == "1/3"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "DSC_0001.JPG",
        "DSC_0001.NEF",
    ]
    assert (tmp_path / "DSC_0001.NEF").read_bytes() == b"DSC_0001.NEF contents"
    out = capsys.readouterr().out
    assert "exposure_time set to 0.3333 s" in out
    assert "DSC_0001.NEF" in out


def test_creates_the_capture_directory(attach, tmp_path):
    attach(FakeCamera())
    directory = tmp_path / "captures" / "run-1"
    assert main(["dslr", "--capture", str(directory)]) == 0
    assert [p.name for p in directory.iterdir()] == ["DSC_0001.NEF"]


def test_refuses_to_overwrite_a_capture(attach, capsys, tmp_path):
    attach(FakeCamera())
    (tmp_path / "DSC_0001.NEF").write_bytes(b"earlier")
    assert main(["dslr", "--capture", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "DSC_0001.NEF").read_bytes() == b"earlier"


def test_grabs_a_preview(attach, capsys):
    attach(FakeCamera())
    assert main(["dslr", "--preview"]) == 0
    assert "preview: 64x48" in capsys.readouterr().out


def test_steps_the_focus_drive(attach, capsys):
    (camera,) = attach(FakeCamera(focus_position=2500))
    assert main(["dslr", "--focus-steps", "-300"]) == 0
    assert camera.focus_position == 2200
    assert "focus moved -300 steps" in capsys.readouterr().out


def test_reports_a_focus_limit(attach, capsys):
    attach(FakeCamera(focus_position=4900, focus_travel=5000))
    assert main(["dslr", "--focus-steps", "300"]) == 0
    assert "reached the far limit" in capsys.readouterr().out


def test_focus_steps_without_a_focus_drive_fail(attach, capsys):
    widgets = [w for w in NIKON_WIDGETS if w.name != "manualfocusdrive"]
    attach(FakeCamera(widgets=widgets))
    assert main(["dslr", "--focus-steps", "10"]) == 1
    assert "no focus drive" in capsys.readouterr().err


def test_uses_the_given_port(attach, capsys):
    attach(FakeCamera(port="usb:001,004"), FakeCamera(port="usb:001,005"))
    assert main(["dslr", "--port", "usb:001,005"]) == 0
    assert "at usb:001,005" in capsys.readouterr().out


def test_no_camera_is_an_error(attach, monkeypatch, capsys):
    monkeypatch.setattr(gphoto2, "OPEN_RETRY_DELAY_S", 0)
    attach()
    assert main(["dslr"]) == 1
    assert "no camera" in capsys.readouterr().err


def test_missing_sdk_says_how_to_install_it(monkeypatch, capsys):
    def missing():
        raise ImportError("Could not import 'gphoto2'. Install it with: pip install x")

    monkeypatch.setattr(gphoto2, "_default_library", missing)
    assert main(["dslr"]) == 1
    assert "pip install" in capsys.readouterr().err


def test_lists_a_setting_with_no_value(attach, capsys):
    widgets = [
        dataclasses.replace(w, value="Bulb") if w.name == "shutterspeed" else w
        for w in NIKON_WIDGETS
    ]
    attach(FakeCamera(widgets=widgets))
    assert main(["dslr"]) == 0
    assert "exposure_time = no number (8 choices)" in capsys.readouterr().out
