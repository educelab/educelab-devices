"""`AravisCamera`, tested against the fake ``gi.repository`` in `fakes.aravis`."""

import builtins
import re
import subprocess
import sys

import numpy as np
import pytest

from contracts import FrameCameraContract
from contracts.genicam import BackendContract
from educelab.devices.cameras import PIXEL_FORMAT, aravis
from educelab.devices.cameras._genicam import TRIGGER, BufferStatus
from educelab.devices.cameras.aravis import (
    ARAVIS_INSTALL_HINT,
    BUFFER_COUNT,
    AravisCamera,
    load_aravis,
)
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from fakes.aravis import DEVICE_DOMAIN, FakeRepository, GLibError
from fakes.aravis import DeviceError as ArvDeviceError
from fakes.genicam import FakeCamera, FrameSpec

MONO = "4108000001"
COLOR = "4108000002"


@pytest.fixture
def repository():
    return FakeRepository(
        [
            FakeCamera(MONO, "U3-36P0XCP-M"),
            FakeCamera(COLOR, "U3-36P0XCP-C", color=True),
        ]
    )


@pytest.fixture
def sim(repository):
    return repository.cameras[0]


@pytest.fixture
def camera(repository):
    with AravisCamera(MONO, repository=repository) as camera:
        yield camera


def backend_of(camera):
    return camera._backend


def connection_of(camera):
    return camera._connection


# --- Loading Aravis -----------------------------------------------------------


def test_importing_the_driver_does_not_import_gi():
    code = (
        "import sys, educelab.devices.cameras.aravis; "
        "assert 'gi' not in sys.modules, 'gi was imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.fixture
def no_gi(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "gi" or name.startswith("gi."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "gi", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_missing_pygobject_points_to_the_install_steps(no_gi):
    with pytest.raises(ImportError, match="docs/hardware/usb3-vision.md"):
        load_aravis()


def test_the_install_hint_covers_each_os():
    for os_name in ("macOS", "Linux"):
        assert os_name in ARAVIS_INSTALL_HINT


def test_missing_aravis_typelib_points_to_the_install_steps(monkeypatch):
    class FakeGi:
        @staticmethod
        def require_version(namespace, version):
            raise ValueError(f"Namespace {namespace} not available")

    monkeypatch.setitem(sys.modules, "gi", FakeGi)
    with pytest.raises(ImportError, match="Aravis 0.8.*usb3-vision.md"):
        load_aravis()


def test_constructing_a_camera_does_not_need_gi(no_gi):
    AravisCamera(MONO)


def test_opening_without_gi_raises_import_error(no_gi):
    with pytest.raises(ImportError, match="usb3-vision.md"):
        AravisCamera(MONO).open()


# --- Discovery, open and close ------------------------------------------------


def test_discover_lists_attached_cameras(repository, monkeypatch):
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    found = AravisCamera.discover()
    assert [i.serial_number for i in found] == [MONO, COLOR]
    assert found[0].backend == "aravis"
    assert found[0].model == "U3-36P0XCP-M"
    assert found[0].address == "usb:1-1"


def test_constructor_does_not_touch_aravis(repository, sim):
    AravisCamera(MONO, repository=repository)
    assert repository.update_calls == 0
    assert not sim.opened


def test_open_finds_the_camera_by_serial(camera, sim):
    assert sim.opened
    assert camera.info.model == "U3-36P0XCP-M"
    assert camera.info.backend == "aravis"


def test_a_flir_opened_by_usb_serial_reports_its_own_serial():
    sim = FakeCamera(
        "0135BAD4", "Chameleon3 CM3-U3-50S5M", vendor="Point Grey Research"
    )
    sim.set("DeviceSerialNumber", "20298452")
    repository = FakeRepository([sim])
    with AravisCamera("0135BAD4", repository=repository) as camera:
        assert camera.info.serial_number == "20298452"


@pytest.mark.parametrize(
    ("platform", "mode"),
    # On macOS, async mode can hang closing a stream in arv_uv_stream_finalize;
    # on Linux, sync mode drops frames from the IDS camera (usb3-vision.md).
    [("darwin", "SYNC"), ("linux", "ASYNC"), ("win32", "ASYNC")],
)
def test_usb_transfers_suit_the_platform(repository, monkeypatch, platform, mode):
    monkeypatch.setattr(aravis.sys, "platform", platform)
    with AravisCamera(MONO, repository=repository) as camera:
        usb_mode = connection_of(camera)._camera.usb_mode
    assert usb_mode == getattr(repository.Aravis.UvUsbMode, mode)


def test_open_turns_on_access_checks_but_not_range_checks(camera, repository):
    # Aravis's range check refuses Mono16 on the CM3-U3-50S5M, whose XML gives the
    # PixelFormat register a stale maximum (usb3-vision.md). The core checks ranges.
    device = connection_of(camera)._device
    assert device.access_check == repository.Aravis.AccessCheckPolicy.ENABLE
    assert device.range_check == repository.Aravis.RangeCheckPolicy.DISABLE


def test_a_locked_node_is_not_writable(camera, sim):
    # Aravis's access mode ignores locks (usb3-vision.md), so the driver asks.
    sim.set("ExposureAuto", "Continuous")
    conn = connection_of(camera)
    assert conn.is_readable("ExposureTime")
    assert not conn.is_writable("ExposureTime")
    sim.set("ExposureAuto", "Off")
    assert conn.is_writable("ExposureTime")


def test_open_queues_a_pool_of_buffers_sized_to_the_payload(camera, sim):
    stream = connection_of(camera)._stream
    buffers = list(stream.input) + list(stream.output)
    assert len(buffers) == BUFFER_COUNT
    assert all(b.size == sim.width * sim.height for b in buffers)


def test_unknown_serial_raises_device_not_found_naming_what_is_attached(repository):
    with pytest.raises(DeviceNotFound, match=f"{MONO}.*{COLOR}"):
        AravisCamera("99999999", repository=repository).open()


def test_camera_held_elsewhere_raises_device_busy(repository, sim):
    sim.busy = True
    with pytest.raises(DeviceBusy, match=MONO) as info:
        AravisCamera(MONO, repository=repository).open()
    assert info.value.__context__ is None


def test_reopening_skips_the_slow_device_list_update(repository):
    # Each update waits about a second on GigE discovery (usb3-vision.md).
    camera = AravisCamera(MONO, repository=repository)
    with camera:
        pass
    updates = repository.update_calls
    with camera:
        pass
    assert repository.update_calls == updates


def test_a_camera_attached_since_the_last_listing_is_found(repository):
    repository.Aravis.update_device_list()
    late = FakeCamera("4108000003")
    repository.cameras.append(late)
    with AravisCamera("4108000003", repository=repository):
        assert late.opened


def test_a_camera_unplugged_since_the_last_listing_is_not_found(repository, sim):
    repository.Aravis.update_device_list()
    sim.attached = False
    with pytest.raises(DeviceNotFound):
        AravisCamera(MONO, repository=repository).open()


def test_a_camera_open_in_this_process_is_refused_without_asking_aravis(repository):
    # Aravis's failed second open releases USB interfaces it never claimed;
    # refusing it ourselves is a precaution (usb3-vision.md).
    with AravisCamera(MONO, repository=repository):
        attempts = repository.open_attempts
        with pytest.raises(DeviceBusy, match="already open"):
            AravisCamera(MONO, repository=repository).open()
        assert repository.open_attempts == attempts
    with AravisCamera(MONO, repository=repository):
        pass


def test_close_lets_go_of_the_camera(repository, sim):
    camera = AravisCamera(MONO, repository=repository)
    camera.open()
    camera.close()
    assert not sim.opened
    assert not sim.streaming
    assert sim.held == []


def test_reopening_works(repository, sim):
    camera = AravisCamera(MONO, repository=repository)
    with camera:
        camera.capture_frame()
    with camera:
        camera.capture_frame()
    assert sim.open_calls == 2
    assert not sim.opened


# --- Stream -------------------------------------------------------------------


def test_grab_returns_the_newest_frame_and_recycles_older_ones(camera, sim):
    conn = connection_of(camera)
    stream = conn._stream
    assert stream.arrive(3) == 3
    buffer = conn.grab(1.0)
    assert buffer.frame_id() == sim.frames_grabbed
    assert len(sim.held) == 1
    buffer.release()
    assert sim.held == []
    assert stream.get_n_buffers() == (BUFFER_COUNT, 0)


def test_frames_landing_while_nobody_grabs_leave_only_the_newest(camera, sim):
    # Otherwise Aravis fills every buffer and then drops the newer frames.
    stream = connection_of(camera)._stream
    stream.arrive(BUFFER_COUNT * 2)
    assert len(stream.output) == 1
    assert stream.output[0].get_frame_id() == sim.frames_grabbed
    assert len(sim.held) == 1


def test_the_stream_callback_does_not_keep_the_camera_alive(repository, sim):
    camera = AravisCamera(MONO, repository=repository)
    camera.open()
    stream = connection_of(camera)._stream
    assert stream.callback is not None
    del stream
    camera.close()
    assert not sim.opened


def test_a_leftover_buffer_does_not_keep_the_camera_alive(repository, sim):
    # A failed test's traceback can hold one; the camera must still be let go.
    camera = AravisCamera(MONO, repository=repository)
    camera.open()
    leftover = connection_of(camera).grab(1.0)
    leftover.release()
    camera.close()
    assert not sim.opened
    del leftover


def test_tl_params_are_left_alone(camera, sim):
    # Setting it stops the U3-36PxXCP-M streaming through Aravis.
    assert sim.get("TLParamsLocked") == 0
    connection_of(camera).stop_acquisition()
    assert sim.get("TLParamsLocked") == 0


def test_grab_timeout_is_passed_in_microseconds(camera):
    conn = connection_of(camera)
    conn.grab(0.25).release()
    assert ("timeout", 250_000) in conn._stream.pops


def test_grab_with_no_timeout_blocks(camera):
    conn = connection_of(camera)
    conn.grab(None).release()
    assert ("blocking", None) in conn._stream.pops


def test_a_zero_timeout_grab_only_takes_frames_already_there(camera):
    conn = connection_of(camera)
    with pytest.raises(DeviceTimeout):
        conn.grab(0)
    assert all(kind == "try" for kind, _ in conn._stream.pops)


def test_a_grab_that_times_out_raises_device_timeout(camera, sim):
    sim.buffer_count = 0
    with pytest.raises(DeviceTimeout):
        connection_of(camera).grab(0.01)


def test_an_incomplete_buffer_is_incomplete(camera, sim):
    sim.queue_frames(FrameSpec(status=BufferStatus.INCOMPLETE))
    buffer = connection_of(camera).grab(1.0)
    assert buffer.status() is BufferStatus.INCOMPLETE
    assert buffer.status_text() == "missing-packets"
    buffer.release()


def test_a_frame_is_shaped_by_its_pixel_format(repository):
    with AravisCamera(COLOR, repository=repository) as camera:
        for fmt, shape, dtype in (
            ("RGB8", (6, 8, 3), np.uint8),
            ("Mono16", (6, 8), np.uint16),
            ("BayerRG8", (6, 8), np.uint8),
        ):
            camera.set_setting(PIXEL_FORMAT, fmt)
            frame = camera.capture_frame()
            assert (frame.pixel_format, frame.pixels.shape) == (fmt, shape)
            assert frame.pixels.dtype == dtype


def test_a_larger_payload_gets_new_buffers(camera, repository):
    allocated = repository.buffers_allocated
    camera.set_setting(PIXEL_FORMAT, "Mono16")
    assert repository.buffers_allocated == allocated + BUFFER_COUNT
    camera.capture_frame()
    camera.set_setting(PIXEL_FORMAT, "Mono8")
    camera.capture_frame()


def test_stopping_drops_the_stream_and_its_waiting_frames(camera, sim):
    # Its channel would otherwise stay enabled, and the U3-36PxXCP-M refuses
    # PixelFormat writes while it is.
    conn = connection_of(camera)
    conn._stream.arrive(2)
    conn.stop_acquisition()
    assert conn._stream is None
    assert sim.held == []
    conn._device.set_string_feature_value("PixelFormat", "Mono16")


def test_pixels_survive_release(camera):
    buffer = connection_of(camera).grab(1.0)
    pixels = buffer.pixels()
    before = pixels.copy()
    buffer.release()
    np.testing.assert_array_equal(pixels, before)
    assert pixels.flags.writeable


def test_ten_bit_frames_are_unpacked():
    sim = FakeCamera(MONO, formats=["Mono8", "Mono10g40IDS"])
    with AravisCamera(MONO, repository=FakeRepository([sim])) as camera:
        camera.set_setting(PIXEL_FORMAT, "Mono10g40IDS")
        frame = camera.capture_frame()
    assert frame.pixels.dtype == np.uint16
    assert frame.pixels.shape == (sim.height, sim.width)
    assert frame.metadata["bit_depth"] == 10
    assert frame.pixels.max() == frame.metadata["frame_id"] % 200


# --- Errors -------------------------------------------------------------------


def test_aravis_timeouts_become_device_timeouts(camera, monkeypatch):
    def slow(name):
        raise GLibError("timed out", DEVICE_DOMAIN, int(ArvDeviceError.TIMEOUT))

    monkeypatch.setattr(connection_of(camera)._device, "execute_command", slow)
    with pytest.raises(DeviceTimeout, match="timed out") as info:
        camera.measure_timestamp_offset()
    assert info.value.__context__ is None


def test_other_aravis_errors_become_device_errors(camera, monkeypatch):
    def broken(name):
        raise GLibError("transfer", DEVICE_DOMAIN, int(ArvDeviceError.TRANSFER_ERROR))

    monkeypatch.setattr(connection_of(camera)._device, "execute_command", broken)
    with pytest.raises(DeviceError, match="transfer") as info:
        camera.measure_timestamp_offset()
    assert type(info.value) is DeviceError


# --- Contracts ----------------------------------------------------------------


class TestAravisBackend(BackendContract):
    @pytest.fixture
    def make_backend(self):
        return lambda cameras: aravis._AravisBackend(FakeRepository(cameras))


class TestAravisCameraContract(FrameCameraContract):
    @pytest.fixture(autouse=True)
    def repository(self, monkeypatch):
        repository = FakeRepository([FakeCamera(MONO, color=True)])
        monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
        return repository

    @pytest.fixture
    def make_device(self, repository):
        return lambda: AravisCamera(MONO, repository=repository)

    def test_closing_lets_go_of_the_camera(self, make_device, repository):
        with make_device() as camera:
            camera.capture_frame()
            camera.preview_frame()
        assert not repository.cameras[0].opened
        assert repository.cameras[0].held == []


class TestAravisCameraContractSoftwareTrigger(TestAravisCameraContract):
    """The same rules with every capture software-triggered."""

    @pytest.fixture
    def device(self, make_device):
        with make_device() as camera:
            camera.set_setting(TRIGGER, "Software")
            yield camera


# --- el-devices discover ------------------------------------------------------


def test_el_devices_discover_lists_aravis_cameras(
    repository, monkeypatch, capsys, discover_only
):
    discover_only("aravis")
    from educelab.devices.app.cli import main

    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "aravis: 2 found" in out
    assert re.search(rf"U3-36P0XCP-M +{MONO} +usb:1-1", out)
