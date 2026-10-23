"""`GenTLCamera`, tested against the fake ``genicam`` package in `fakes.gentl`."""

import re
import subprocess
import sys

import numpy as np
import pytest

from contracts import FrameCameraContract
from contracts.genicam import BackendContract
from educelab.devices.cameras import PIXEL_FORMAT, gentl
from educelab.devices.cameras._genicam import TRIGGER, BufferStatus
from educelab.devices.cameras.gentl import (
    BUFFER_COUNT,
    GENTL_INSTALL_HINT,
    GenTLCamera,
    find_producers,
    load_genicam,
)
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from fakes.genicam import FakeCamera, FrameSpec
from fakes.gentl import PRODUCER, FakeGenicam, TimeoutException

MONO = "4108000001"
COLOR = "4108000002"


@pytest.fixture
def genicam():
    return FakeGenicam(
        [
            FakeCamera(MONO, "U3-36P0XCP-M"),
            FakeCamera(COLOR, "U3-36P0XCP-C", color=True),
        ]
    )


@pytest.fixture
def sim(genicam):
    return genicam.cameras[0]


def make(serial, genicam):
    return GenTLCamera(serial, producer=PRODUCER, genicam=genicam)


@pytest.fixture
def camera(genicam):
    with make(MONO, genicam) as camera:
        yield camera


def connection_of(camera):
    return camera._connection


# --- Loading genicam and finding producers ------------------------------------


def test_importing_the_driver_does_not_import_genicam():
    code = (
        "import sys, educelab.devices.cameras.gentl; "
        "assert 'genicam' not in sys.modules, 'genicam was imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_genicam_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "genicam", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[gentl\]"):
        load_genicam()


def test_the_install_hint_names_the_producer_variable():
    assert "GENICAM_GENTL64_PATH" in GENTL_INSTALL_HINT
    assert "usb3-vision.md" in GENTL_INSTALL_HINT


def test_producers_are_found_on_the_gentl_path(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "ids_u3vgentl.cti").touch()
    (tmp_path / "a" / "readme.txt").touch()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "other.cti").touch()
    path = f"{tmp_path / 'a'}{__import__('os').pathsep}{tmp_path / 'b'}"
    monkeypatch.setenv("GENICAM_GENTL64_PATH", path)
    assert find_producers() == [
        str(tmp_path / "a" / "ids_u3vgentl.cti"),
        str(tmp_path / "b" / "other.cti"),
    ]


def test_no_producer_on_the_path_points_to_the_install_steps(monkeypatch, genicam):
    monkeypatch.delenv("GENICAM_GENTL64_PATH", raising=False)
    with pytest.raises(ImportError, match="GENICAM_GENTL64_PATH"):
        GenTLCamera(MONO, genicam=genicam).open()


def test_a_producer_on_the_path_is_used(tmp_path, monkeypatch, sim):
    cti = tmp_path / "ids_u3vgentl.cti"
    cti.touch()
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    genicam = FakeGenicam([sim], producers=[str(cti)])
    with GenTLCamera(MONO, genicam=genicam):
        assert genicam.producers_opened == [str(cti)]


def test_a_producer_that_will_not_load_raises_import_error(genicam):
    camera = GenTLCamera(MONO, producer="/nowhere/broken.cti", genicam=genicam)
    with pytest.raises(ImportError, match="broken.cti"):
        camera.open()
    assert genicam.open_modules == []


def producer_folder(tmp_path, *names):
    """A folder of empty .cti files, put on the GenTL path; returns their paths."""
    paths = []
    for name in names:
        (tmp_path / name).touch()
        paths.append(str(tmp_path / name))
    return paths


def test_a_producer_that_will_not_load_is_skipped(tmp_path, monkeypatch, sim, caplog):
    # IDS peak installs several producers in one folder (usb3-vision.md).
    _, good = producer_folder(tmp_path, "a_broken.cti", "ids_u3vgentl.cti")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    genicam = FakeGenicam([sim], producers=[good])
    with caplog.at_level("WARNING", logger="educelab.devices.cameras.gentl"):
        assert [i.serial_number for i in GenTLCamera.discover(genicam=genicam)] == [
            MONO
        ]
        with GenTLCamera(MONO, genicam=genicam):
            pass
    assert "a_broken.cti" in caplog.text
    assert genicam.open_modules == []


def test_a_producer_that_fails_to_list_cameras_is_skipped(tmp_path, monkeypatch, sim):
    first, second = producer_folder(tmp_path, "a.cti", "b.cti")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    genicam = FakeGenicam([sim], producers=[first, second], listing_fails=[first])
    assert [i.serial_number for i in GenTLCamera.discover(genicam=genicam)] == [MONO]
    with GenTLCamera(MONO, genicam=genicam) as camera:
        assert camera.info.serial_number == MONO
    assert genicam.open_modules == []


def test_when_no_producer_loads_the_error_names_each(tmp_path, monkeypatch, sim):
    producer_folder(tmp_path, "a.cti", "b.cti")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    genicam = FakeGenicam([sim], producers=[])
    with pytest.raises(ImportError, match=r"a\.cti.*b\.cti"):
        GenTLCamera.discover(genicam=genicam)
    with pytest.raises(ImportError, match=r"a\.cti.*b\.cti"):
        GenTLCamera(MONO, genicam=genicam).open()


def test_discover_can_be_limited_to_one_producer(tmp_path, monkeypatch, sim):
    first, second = producer_folder(tmp_path, "a.cti", "b.cti")
    monkeypatch.setenv("GENICAM_GENTL64_PATH", str(tmp_path))
    genicam = FakeGenicam([sim], producers=[first, second])
    GenTLCamera.discover(producer=second, genicam=genicam)
    assert genicam.producers_opened == [second]


def test_what_a_producer_prints_as_it_loads_is_logged_not_shown(sim, capfd, caplog):
    chatter = b"GenTL producer does not implement DSGetNumFlows\n"
    genicam = FakeGenicam([sim], chatter=chatter)
    with (
        caplog.at_level("DEBUG", logger="educelab.devices.cameras.gentl"),
        make(MONO, genicam),
    ):
        pass
    assert "DSGetNumFlows" not in capfd.readouterr().err
    assert "DSGetNumFlows" in caplog.text


def test_constructing_a_camera_touches_nothing(genicam):
    make(MONO, genicam)
    assert genicam.producers_opened == []


# --- Discovery, open and close ------------------------------------------------


def test_discover_lists_attached_cameras(genicam, monkeypatch):
    monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
    monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER])
    found = GenTLCamera.discover()
    assert [i.serial_number for i in found] == [MONO, COLOR]
    assert found[0].backend == "gentl"
    assert found[0].model == "U3-36P0XCP-M"
    assert found[0].address == f"IDS-{MONO}"
    assert genicam.open_modules == []


def test_open_finds_the_camera_by_serial(camera, sim):
    assert sim.opened
    assert camera.info.backend == "gentl"


def test_unknown_serial_raises_device_not_found_naming_what_is_attached(genicam):
    with pytest.raises(DeviceNotFound, match=f"{MONO}.*{COLOR}"):
        make("99999999", genicam).open()
    assert genicam.open_modules == []


def test_camera_held_elsewhere_raises_device_busy(genicam, sim):
    sim.busy = True
    with pytest.raises(DeviceBusy, match=MONO) as info:
        make(MONO, genicam).open()
    assert info.value.__context__ is None
    assert genicam.open_modules == []


def test_a_camera_open_in_this_process_is_busy_even_if_the_producer_allows_it(sim):
    genicam = FakeGenicam([sim], reopenable=True)
    with make(MONO, genicam), pytest.raises(DeviceBusy, match="already open"):
        make(MONO, genicam).open()
    with make(MONO, genicam):
        pass


def test_close_releases_every_module(genicam, sim):
    camera = make(MONO, genicam)
    camera.open()
    camera.capture_frame()
    camera.close()
    assert genicam.open_modules == []
    assert not sim.opened
    assert sim.held == []


def test_cameras_share_one_producer(genicam):
    with make(MONO, genicam), make(COLOR, genicam):
        assert genicam.producers_opened == [PRODUCER]
    assert genicam.open_modules == []


def test_a_failed_open_leaves_a_shared_producer_loaded(genicam, monkeypatch):
    with make(MONO, genicam) as camera:
        monkeypatch.setattr(gentl, "_node_map", lambda *args: None)
        with pytest.raises(DeviceError, match="describes no features"):
            make(COLOR, genicam).open()
        monkeypatch.undo()
        assert genicam.producers_opened == [PRODUCER]
        camera.capture_frame()
    assert genicam.open_modules == []


def test_reopening_works(genicam, sim):
    camera = make(MONO, genicam)
    for _ in range(2):
        with camera:
            camera.capture_frame()
    assert sim.open_calls == 2
    assert genicam.open_modules == []


def test_a_zipped_xml_is_loaded(sim):
    genicam = FakeGenicam([sim], zipped_xml=True)
    with make(MONO, genicam) as camera:
        assert camera.info.model == "U3-36P0XCP-M"


# --- Stream -------------------------------------------------------------------


def test_open_asks_the_stream_for_newest_only(camera, genicam):
    assert genicam.streams()[0].nodes.mode == "NewestOnly"


def test_acquiring_starts_the_stream_and_the_camera(camera, genicam, sim):
    stream = genicam.streams()[0]
    assert stream.started and sim.streaming
    assert sim.get("TLParamsLocked") == 1
    assert len(stream.announced) == BUFFER_COUNT
    connection_of(camera).stop_acquisition()
    assert not stream.started and not sim.streaming
    assert sim.get("TLParamsLocked") == 0
    assert stream.announced == []


def test_grab_returns_the_newest_frame_and_requeues_older_ones(camera, genicam, sim):
    stream = genicam.streams()[0]
    assert stream.arrive(3) == 3
    buffer = connection_of(camera).grab(1.0)
    assert buffer.frame_id() == sim.frames_grabbed
    assert len(sim.held) == 1
    buffer.release()
    assert sim.held == []
    assert len(stream.input) == BUFFER_COUNT


def test_newest_only_works_without_a_stream_node_map(sim):
    genicam = FakeGenicam([sim], stream_nodes=False)
    with make(MONO, genicam) as camera:
        genicam.streams()[0].arrive(2)
        buffer = connection_of(camera).grab(1.0)
        assert buffer.frame_id() == sim.frames_grabbed
        buffer.release()


def test_long_waits_are_cut_into_short_slices(camera, genicam, sim):
    # genicam holds the GIL while it waits (harvesters #508).
    sim.buffer_count = 0
    with pytest.raises(DeviceTimeout):
        connection_of(camera).grab(0.25)
    waits = genicam.streams()[0].waits
    assert waits[-3:] == [100, 100, 50]


def test_a_zero_timeout_grab_does_not_wait(camera, genicam):
    with pytest.raises(DeviceTimeout):
        connection_of(camera).grab(0)
    assert genicam.streams()[0].waits == [0]


def test_an_incomplete_buffer_is_incomplete(camera, sim):
    sim.queue_frames(FrameSpec(status=BufferStatus.INCOMPLETE))
    buffer = connection_of(camera).grab(1.0)
    assert buffer.status() is BufferStatus.INCOMPLETE
    buffer.release()


def test_a_frame_is_shaped_by_its_pixel_format(genicam):
    with make(COLOR, genicam) as camera:
        for fmt, shape, dtype in (
            ("RGB8", (6, 8, 3), np.uint8),
            ("Mono16", (6, 8), np.uint16),
            ("BayerRG8", (6, 8), np.uint8),
        ):
            camera.set_setting(PIXEL_FORMAT, fmt)
            frame = camera.capture_frame()
            assert (frame.pixel_format, frame.pixels.shape) == (fmt, shape)
            assert frame.pixels.dtype == dtype


def test_buffers_are_sized_to_the_payload(camera, genicam):
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    stream = genicam.streams()[0]
    assert all(len(b.raw_buffer) == stream.payload_size for b in stream.announced)


def test_pixels_survive_requeueing(camera):
    buffer = connection_of(camera).grab(1.0)
    pixels = buffer.pixels()
    before = pixels.copy()
    buffer.release()
    np.testing.assert_array_equal(pixels, before)
    assert pixels.flags.writeable


def test_ten_bit_frames_are_unpacked():
    sim = FakeCamera(MONO, formats=["Mono8", "Mono10g40IDS"])
    with make(MONO, FakeGenicam([sim])) as camera:
        camera.set_setting(PIXEL_FORMAT, "Mono10g40IDS")
        frame = camera.capture_frame()
    assert frame.pixels.dtype == np.uint16
    assert frame.pixels.shape == (sim.height, sim.width)
    assert frame.metadata["bit_depth"] == 10
    assert frame.pixels.max() == frame.metadata["frame_id"] % 200


# --- Errors -------------------------------------------------------------------


def test_gentl_timeouts_become_device_timeouts(camera, monkeypatch):
    def slow(*args):
        raise TimeoutException("timed out")

    monkeypatch.setattr(connection_of(camera)._events, "update_event_data", slow)
    with pytest.raises(DeviceTimeout) as info:
        camera.capture_frame(timeout=0.2)
    assert info.value.__context__ is None


def test_genapi_errors_become_device_errors(camera, sim):
    sim.set("ExposureAuto", "Continuous")
    with pytest.raises(DeviceError, match="writable") as info:
        connection_of(camera).set_float("ExposureTime", 5.0)
    assert info.value.__context__ is None


def test_an_unknown_enum_entry_is_a_device_error(camera):
    with pytest.raises(DeviceError):
        connection_of(camera).set_enum("ExposureAuto", "Sometimes")


# --- Contracts ----------------------------------------------------------------


class TestGenTLBackend(BackendContract):
    @pytest.fixture
    def make_backend(self):
        return lambda cameras: gentl._GenTLBackend(FakeGenicam(cameras), [PRODUCER])


class TestGenTLCameraContract(FrameCameraContract):
    @pytest.fixture(autouse=True)
    def genicam(self, monkeypatch):
        genicam = FakeGenicam([FakeCamera(MONO, color=True)])
        monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
        monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER])
        return genicam

    @pytest.fixture
    def make_device(self, genicam):
        return lambda: make(MONO, genicam)

    def test_closing_releases_every_module(self, make_device, genicam):
        with make_device() as camera:
            camera.capture_frame()
            camera.preview_frame()
        assert genicam.open_modules == []


class TestGenTLCameraContractSoftwareTrigger(TestGenTLCameraContract):
    """The same rules with every capture software-triggered."""

    @pytest.fixture
    def device(self, make_device):
        with make_device() as camera:
            camera.set_setting(TRIGGER, "Software")
            yield camera


# --- el-devices discover ------------------------------------------------------


def test_el_devices_discover_lists_gentl_cameras(
    genicam, monkeypatch, capsys, discover_only
):
    discover_only("gentl")
    from educelab.devices.app.cli import main

    monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
    monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER])
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "gentl: 2 found" in out
    assert re.search(rf"U3-36P0XCP-M +{MONO} +IDS-{MONO}", out)
