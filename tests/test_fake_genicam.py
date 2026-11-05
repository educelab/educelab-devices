"""The in-memory GenICam backend that the shared core is tested against."""

import pytest

from contracts.genicam import BackendContract
from educelab.devices.cameras._genicam import Backend, Connection
from fakes.genicam import FakeBackend, FakeCamera, FakeCameraError


class TestFakeBackend(BackendContract):
    @pytest.fixture
    def make_backend(self):
        return FakeBackend


def test_the_fake_fits_the_seam() -> None:
    # For mypy: the assignments fail type-checking if the fake drifts.
    backend: Backend = FakeBackend([FakeCamera()])
    connection: Connection | None = backend.open(FakeCamera().serial)
    assert connection is not None
    connection.close()


# --- FakeCamera rules the contract doesn't reach ------------------------------


@pytest.fixture
def opened():
    camera = FakeCamera()
    camera.open()
    return camera


def triggered(camera):
    camera.write("TriggerSource", "Software")
    camera.write("TriggerMode", "On")
    camera.start()
    return camera


def test_pixel_format_is_read_only_while_acquiring(opened):
    opened.start()
    assert opened.access("PixelFormat") == "RO"
    with pytest.raises(FakeCameraError):
        opened.write("PixelFormat", "Mono16")
    opened.stop()
    opened.write("PixelFormat", "Mono16")
    assert opened.get("PixelFormat") == "Mono16"


def test_timestamp_latch_follows_the_host_clock():
    now = [1_000_000.0]
    camera = FakeCamera(clock=lambda: now[0], uptime=50.0)
    camera.open()
    camera.execute("TimestampLatch")
    assert camera.read("Timestamp") == 50_000_000_000
    now[0] += 1.5
    camera.execute("TimestampLatch")
    assert camera.read("Timestamp") == 51_500_000_000


def test_triggers_can_be_ignored_by_number(opened):
    triggered(opened).ignored_trigger_calls = {2}
    for _ in range(3):
        opened.execute("TriggerSoftware")
    assert opened.trigger_calls == 3
    assert len(opened.pending_triggers) == 2
