"""``el-devices genicam``, against the fake Aravis and ``genicam`` libraries."""

import builtins
import sys

import pytest

from educelab.devices.app.cli import main
from educelab.devices.cameras import aravis, gentl
from fakes.aravis import FakeRepository
from fakes.genicam import FakeCamera, FrameSpec
from fakes.gentl import PRODUCER, FakeGenicam

MONO = "4108000001"
COLOR = "4108000002"


def cameras(**options):
    return [
        FakeCamera(MONO, "U3-36P0XCP-M", **options),
        FakeCamera(COLOR, "U3-36P0XCP-C", color=True, **options),
    ]


@pytest.fixture
def repository(monkeypatch):
    repository = FakeRepository(cameras())
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    return repository


@pytest.fixture
def genicam(monkeypatch):
    genicam = FakeGenicam(cameras())
    monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
    monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER])
    return genicam


@pytest.fixture(params=["aravis", "gentl"])
def backend(request):
    request.getfixturevalue("repository" if request.param == "aravis" else "genicam")
    return request.param


def sims(backend, request):
    library = request.getfixturevalue(
        "repository" if backend == "aravis" else "genicam"
    )
    return library.cameras


def test_backend_is_required():
    with pytest.raises(SystemExit):
        main(["genicam"])


def test_lists_cameras_without_opening_them(backend, capsys, request):
    assert main(["genicam", "--backend", backend, "--frames", "0"]) == 0
    out = capsys.readouterr().out
    assert f"{MONO}  U3-36P0XCP-M" in out
    assert f"{COLOR}  U3-36P0XCP-C" in out
    assert all(not sim.opened for sim in sims(backend, request))


def test_benchmarks_every_camera_at_once(backend, capsys, request):
    assert main(["genicam", "--backend", backend, "--frames", "3"]) == 0
    out = capsys.readouterr().out
    assert "Capturing 3 frame(s) from 2 camera(s) at once" in out
    assert f"{MONO}: 3/3 captured" in out
    assert f"{COLOR}: 3/3 captured" in out
    assert "timestamp offset" in out
    assert "frames/s" in out
    assert all(not sim.opened for sim in sims(backend, request))


def test_reports_bit_depth_and_timestamp_source(monkeypatch, capsys):
    repository = FakeRepository([FakeCamera(MONO, formats=["Mono8", "Mono10g40IDS"])])
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    args = ["--frames", "1", "--pixel-format", "Mono10g40IDS"]
    assert main(["genicam", "--backend", "aravis", *args]) == 0
    out = capsys.readouterr().out
    assert "10-bit pixels" in out
    assert "from the latch" in out


def test_a_camera_without_a_latch_is_timed_by_arrival(monkeypatch, capsys):
    repository = FakeRepository([FakeCamera(MONO, without={"TimestampLatch"})])
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    assert main(["genicam", "--backend", "aravis", "--frames", "2"]) == 0
    out = capsys.readouterr().out
    assert "from frame arrivals" in out
    assert f"{MONO}: 2/2 captured" in out


def test_a_camera_without_auto_exposure_takes_an_exposure(monkeypatch, capsys):
    repository = FakeRepository([FakeCamera(MONO, without={"ExposureAuto"})])
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    args = ["--frames", "1", "--exposure", "0.002"]
    assert main(["genicam", "--backend", "aravis", *args]) == 0
    assert "exposure 2 ms" in capsys.readouterr().out


def test_reports_dropped_frames(repository, capsys):
    sim = repository.cameras[0]
    sim.queue_frames(FrameSpec(timestamp_ns=sim.device_time_ns() - 10**9))
    main(["genicam", "--backend", "aravis", "--serial", MONO, "--frames", "1"])
    assert "dropped 1 stale, 0 incomplete" in capsys.readouterr().out


def test_software_trigger(backend, capsys):
    args = ["--frames", "2", "--trigger", "software"]
    assert main(["genicam", "--backend", backend, *args]) == 0
    out = capsys.readouterr().out
    assert out.count("trigger Software") == 2
    assert out.count("2/2 captured") == 2


def test_unknown_serial_fails(backend, capsys):
    assert main(["genicam", "--backend", backend, "--serial", "99999999"]) == 1
    assert "99999999" in capsys.readouterr().err


def test_a_camera_two_producers_list_is_used_once(monkeypatch, capsys):
    genicam = FakeGenicam(cameras())
    monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
    monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER, PRODUCER])
    assert main(["genicam", "--backend", "gentl", "--frames", "1"]) == 0
    out = capsys.readouterr().out
    assert "Capturing 1 frame(s) from 2 camera(s)" in out


def test_without_aravis_points_to_the_install_steps(monkeypatch, capsys):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gi" or name.startswith("gi."):
            raise ImportError("No module named 'gi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "gi", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert main(["genicam", "--backend", "aravis"]) == 1
    assert "usb3-vision.md" in capsys.readouterr().err


def test_without_a_producer_says_so(monkeypatch, capsys):
    monkeypatch.setattr(gentl, "load_genicam", lambda: FakeGenicam([]))
    monkeypatch.setattr(gentl, "find_producers", list)
    assert main(["genicam", "--backend", "gentl"]) == 1
    assert "GENICAM_GENTL64_PATH" in capsys.readouterr().err


def test_bandwidth_counts_packed_pixels_as_sent(monkeypatch, capsys):
    from educelab.devices.app import _capture

    rates = []
    original = _capture._streaming_rate
    monkeypatch.setattr(
        _capture,
        "_streaming_rate",
        lambda stamps, frame_bytes: (
            rates.append(frame_bytes) or original(stamps, frame_bytes)
        ),
    )
    sim = FakeCamera(MONO, formats=["Mono8", "Mono10g40IDS"])
    repository = FakeRepository([sim])
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    args = ["--frames", "2", "--pixel-format", "Mono10g40IDS"]
    assert main(["genicam", "--backend", "aravis", *args]) == 0
    assert rates == [sim.width * sim.height * 10 // 8]
