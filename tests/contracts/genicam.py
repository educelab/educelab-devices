"""Rules every GenICam backend follows at the seam in `cameras._genicam`.

A backend's test module subclasses `BackendContract` and overrides
``make_backend``, which takes `fakes.genicam.FakeCamera`s and returns a
backend that reaches them through the backend's library (or a fake of it).
"""

import numpy as np
import pytest

from educelab.devices.cameras._genicam import BufferStatus
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceTimeout
from fakes.genicam import FakeCamera, FrameSpec

SERIAL = "4108000001"
OTHER = "4108000002"


class BackendContract:
    """Rules every GenICam `Backend` and its connections follow."""

    @pytest.fixture
    def make_backend(self):
        raise NotImplementedError("override make_backend")

    @pytest.fixture
    def sim(self):
        return FakeCamera(SERIAL, "U3-36P0XCP-M", vendor="IDS Imaging")

    @pytest.fixture
    def backend(self, make_backend, sim):
        return make_backend([sim, FakeCamera(OTHER, "U3-36P0XCP-M")])

    @pytest.fixture
    def conn(self, backend):
        conn = backend.open(SERIAL)
        yield conn
        conn.close()

    @pytest.fixture
    def streaming(self, conn):
        conn.start_acquisition()
        return conn

    # --- Discovery and lifecycle ------------------------------------------------

    def test_discover_lists_every_camera_without_opening_it(self, backend, sim):
        found = {info.serial_number: info for info in backend.discover()}
        assert set(found) == {SERIAL, OTHER}
        assert found[SERIAL].model == "U3-36P0XCP-M"
        assert found[SERIAL].vendor == "IDS Imaging"
        assert found[SERIAL].backend == backend.name
        assert not sim.opened

    def test_open_an_unknown_serial_returns_none(self, backend):
        assert backend.open("99999999") is None

    def test_open_a_camera_held_elsewhere_raises_device_busy(self, backend, sim):
        sim.busy = True
        with pytest.raises(DeviceBusy):
            backend.open(SERIAL)
        assert not sim.opened

    def test_close_lets_go_of_the_camera(self, backend, sim):
        conn = backend.open(SERIAL)
        assert sim.opened
        conn.close()
        assert not sim.opened

    def test_close_twice_does_nothing(self, backend):
        conn = backend.open(SERIAL)
        conn.close()
        conn.close()

    def test_close_stops_acquisition(self, backend, sim):
        conn = backend.open(SERIAL)
        conn.start_acquisition()
        conn.close()
        assert not sim.streaming
        assert not sim.opened

    def test_camera_can_be_opened_again(self, backend, sim):
        backend.open(SERIAL).close()
        conn = backend.open(SERIAL)
        assert sim.opened
        conn.close()

    # --- Node map ---------------------------------------------------------------

    def test_a_missing_node_is_neither_readable_nor_writable(self, conn):
        assert not conn.has("NoSuchNode")
        assert not conn.is_readable("NoSuchNode")
        assert not conn.is_writable("NoSuchNode")

    def test_reading_a_missing_node_raises(self, conn):
        with pytest.raises(DeviceError):
            conn.get_float("NoSuchNode")

    def test_floats_have_a_value_and_range(self, conn, sim):
        sim.set("ExposureAuto", "Off")
        assert conn.get_float("ExposureTime") == sim.get("ExposureTime")
        assert conn.float_range("ExposureTime") == sim.range("ExposureTime")
        conn.set_float("ExposureTime", 20_000.0)
        assert sim.get("ExposureTime") == 20_000.0

    def test_access_follows_the_camera(self, conn, sim):
        sim.set("ExposureAuto", "Continuous")
        assert conn.is_readable("ExposureTime")
        assert not conn.is_writable("ExposureTime")
        sim.set("ExposureAuto", "Off")
        assert conn.is_writable("ExposureTime")

    def test_writing_a_read_only_node_raises(self, conn, sim):
        sim.set("ExposureAuto", "Continuous")
        with pytest.raises(DeviceError):
            conn.set_float("ExposureTime", 20_000.0)

    def test_writing_out_of_range_raises(self, conn, sim):
        sim.set("ExposureAuto", "Off")
        _, high = sim.range("ExposureTime")
        with pytest.raises(DeviceError):
            conn.set_float("ExposureTime", high * 2)

    def test_integers_have_a_value_range_and_increment(self, conn, sim):
        assert conn.get_int("Width") == sim.width
        low, high, inc = conn.int_range("DeviceLinkThroughputLimit")
        assert (low, high, inc) == sim.range("DeviceLinkThroughputLimit")
        conn.set_int("DeviceLinkThroughputLimit", low + inc)
        assert sim.get("DeviceLinkThroughputLimit") == low + inc

    def test_strings(self, conn):
        assert conn.get_string("DeviceSerialNumber") == SERIAL
        assert conn.get_string("DeviceModelName") == "U3-36P0XCP-M"

    def test_enums_read_and_write_by_symbol(self, conn, sim):
        assert conn.get_enum("ExposureAuto") == sim.get("ExposureAuto")
        conn.set_enum("ExposureAuto", "Off")
        assert sim.get("ExposureAuto") == "Off"

    def test_enum_entries_are_the_available_ones(self, conn, sim):
        assert conn.enum_entries("PixelFormat") == sim.entries("PixelFormat")

    def test_setting_an_unknown_enum_entry_raises(self, conn):
        with pytest.raises(DeviceError):
            conn.set_enum("ExposureAuto", "Sometimes")

    def test_commands_execute(self, conn, sim):
        conn.execute("TimestampLatch")
        assert sim.latch_calls == 1
        assert conn.get_int(sim.timestamp_node) == sim.latched_ns

    def test_command_nodes_exist(self, conn):
        assert conn.has("TriggerSoftware")

    # --- Stream -----------------------------------------------------------------

    def test_grab_before_acquiring_raises(self, conn):
        with pytest.raises(DeviceError):
            conn.grab(0.1)

    def test_start_and_stop(self, conn, sim):
        conn.start_acquisition()
        assert sim.streaming
        conn.stop_acquisition()
        assert not sim.streaming
        conn.stop_acquisition()

    def test_grab_returns_a_complete_frame(self, streaming, sim):
        buffer = streaming.grab(1.0)
        try:
            assert buffer.status() is BufferStatus.COMPLETE
            assert buffer.pixel_format() == sim.get("PixelFormat")
            pixels = buffer.pixels()
            assert pixels.shape[:2] == (sim.height, sim.width)
            assert buffer.frame_id() >= 0
            assert buffer.device_timestamp_ns() > 0
        finally:
            buffer.release()

    def test_buffers_carry_arrival_times_if_and_only_if_promised(self, streaming):
        buffer = streaming.grab(1.0)
        try:
            arrival = buffer.host_timestamp_ns()
        finally:
            buffer.release()
        assert (arrival is not None) == streaming.times_arrivals

    def test_pixels_are_a_copy(self, streaming):
        buffer = streaming.grab(1.0)
        pixels = buffer.pixels()
        before = pixels.copy()
        buffer.release()
        np.testing.assert_array_equal(pixels, before)

    def test_frames_come_in_order(self, streaming, sim):
        sim.queue_frames(FrameSpec(timestamp_ns=1_000), FrameSpec(timestamp_ns=2_000))
        stamps = []
        for _ in range(2):
            buffer = streaming.grab(1.0)
            stamps.append(buffer.device_timestamp_ns())
            buffer.release()
        assert stamps == [1_000, 2_000]

    def test_an_incomplete_frame_says_so(self, streaming, sim):
        sim.queue_frames(FrameSpec(status=BufferStatus.INCOMPLETE))
        buffer = streaming.grab(1.0)
        assert buffer.status() is BufferStatus.INCOMPLETE
        assert buffer.status_text()
        buffer.release()

    def test_grab_times_out_without_a_frame(self, streaming, sim):
        sim.set("TriggerMode", "Off")
        sim.set("TriggerSource", "Software")
        sim.set("TriggerMode", "On")
        with pytest.raises(DeviceTimeout):
            streaming.grab(0.05)

    def test_a_zero_timeout_grab_returns_only_a_waiting_frame(self, streaming, sim):
        sim.set("TriggerMode", "Off")
        sim.set("TriggerSource", "Software")
        sim.set("TriggerMode", "On")
        with pytest.raises(DeviceTimeout):
            streaming.grab(0)
        streaming.execute("TriggerSoftware")
        streaming.grab(0).release()

    def test_a_software_trigger_produces_a_frame(self, streaming, sim):
        sim.set("TriggerMode", "Off")
        sim.set("TriggerSource", "Software")
        sim.set("TriggerMode", "On")
        streaming.execute("TriggerSoftware")
        streaming.grab(1.0).release()
        assert sim.trigger_calls == 1

    def test_holding_every_buffer_starves_the_stream(self, streaming, sim):
        # Fewer than any backend's own pool, so the camera's limit is what binds.
        sim.buffer_count = 2
        held = [streaming.grab(1.0) for _ in range(sim.buffer_count)]
        with pytest.raises(DeviceTimeout):
            streaming.grab(0.05)
        for buffer in held:
            buffer.release()
        streaming.grab(1.0).release()
