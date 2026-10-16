"""``el-devices spinnaker``, against the fake PySpin."""

import sys
import threading

import pytest

from educelab.devices.app.cli import main
from educelab.devices.cameras import spinnaker
from fakes.pyspin import (
    SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE,
    SPINNAKER_IMAGE_STATUS_MISSING_TRAILER,
    FakeCamera,
    FakePySpin,
    FrameSpec,
)

MONO = "20000001"
COLOR = "20000002"


@pytest.fixture
def pyspin(monkeypatch):
    pyspin = FakePySpin(
        [
            FakeCamera(MONO, "Blackfly S BFS-U3-51S5M"),
            FakeCamera(COLOR, "Blackfly S BFS-U3-200S6C", color=True),
        ]
    )
    monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
    return pyspin


def test_lists_cameras_without_opening_them(pyspin, capsys):
    assert main(["spinnaker", "--frames", "0"]) == 0
    out = capsys.readouterr().out
    assert "Spinnaker SDK 4.4.0.0" in out
    assert f"{MONO}  Blackfly S BFS-U3-51S5M" in out
    assert f"{COLOR}  Blackfly S BFS-U3-200S6C" in out
    assert all(camera.init_calls == 0 for camera in pyspin.system.cameras)


def test_benchmarks_every_camera_at_once(pyspin, capsys):
    assert main(["spinnaker", "--frames", "3"]) == 0
    out = capsys.readouterr().out
    assert "Capturing 3 frame(s) from 2 camera(s) at once" in out
    assert f"{MONO}: 3/3 captured" in out
    assert f"{COLOR}: 3/3 captured" in out
    assert "latency median" in out
    assert "timestamp node Timestamp" in out
    assert "frames/s" in out
    assert pyspin.system.instances == 0


def test_reports_dropped_frames(pyspin, capsys):
    fake = pyspin.system.cameras[0]
    old = fake.device_time_ns() - 10**9
    fake.queue_frames(
        FrameSpec(timestamp_ns=old),
        FrameSpec(status=SPINNAKER_IMAGE_STATUS_MISSING_TRAILER),
    )
    main(["spinnaker", "--serial", MONO, "--frames", "2"])
    out = capsys.readouterr().out
    assert "dropped 1 stale, 0 incomplete; 1 accepted with a missing trailer" in out


def test_applies_exposure_and_pixel_format(pyspin, capsys):
    args = ["--serial", COLOR, "--frames", "1", "--exposure", "0.002"]
    assert main(["spinnaker", *args, "--pixel-format", "Mono16"]) == 0
    out = capsys.readouterr().out
    assert "Mono16, exposure 2 ms (Off)" in out


def test_applies_gain(pyspin, capsys):
    assert main(["spinnaker", "--serial", MONO, "--frames", "1", "--gain", "10"]) == 0
    assert "gain 10.0 dB" in capsys.readouterr().out


def test_failed_captures_fail_the_command(pyspin, capsys):
    pyspin.system.cameras[0].buffer_count = 0  # every grab times out
    assert main(["spinnaker", "--serial", MONO, "--frames", "2"]) == 1
    out = capsys.readouterr().out
    assert f"{MONO}: 0/2 captured" in out
    assert out.count("failed:") == 2


def test_unknown_serial_fails(pyspin, capsys):
    assert main(["spinnaker", "--serial", "99999999"]) == 1
    assert "no Spinnaker camera with serial number 99999999" in capsys.readouterr().err


def test_without_pyspin_points_to_the_install_docs(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "PySpin", None)
    assert main(["spinnaker"]) == 1
    assert "docs/hardware/spinnaker.md" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [["--frames", "-1"], ["--exposure", "0"]])
def test_rejects_bad_arguments(bad):
    with pytest.raises(SystemExit):
        main(["spinnaker", *bad])


def test_incomplete_frames_on_every_camera_are_counted_and_retried(pyspin, capsys):
    # As when several cameras saturate one USB link.
    for fake in pyspin.system.cameras:
        fake.queue_frames(
            *(
                FrameSpec(status=SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE)
                for _ in range(3)
            )
        )
    assert main(["spinnaker", "--frames", "2"]) == 0
    out = capsys.readouterr().out
    assert out.count("dropped 0 stale, 3 incomplete") == 2


def test_a_driver_bug_is_reported_per_camera_not_as_a_thread_traceback(
    pyspin, capsys, monkeypatch
):
    from educelab.devices.cameras.spinnaker import SpinnakerCamera

    original = SpinnakerCamera._take

    def broken(self, image, not_before):
        if self.info.serial_number == MONO:
            raise AttributeError("module 'PySpin' has no attribute 'X'")
        return original(self, image, not_before)

    monkeypatch.setattr(SpinnakerCamera, "_take", broken)
    hooked = []
    monkeypatch.setattr(threading, "excepthook", hooked.append)
    assert main(["spinnaker", "--frames", "2"]) == 1
    captured = capsys.readouterr()
    assert hooked == []
    assert "Traceback" not in captured.err
    assert f"{MONO}: 0/2 captured" in captured.out
    assert captured.out.count("failed: AttributeError: module 'PySpin'") == 2
    assert f"{COLOR}: 2/2 captured" in captured.out
    assert pyspin.system.cameras[0].held == []
    assert pyspin.system.instances == 0
