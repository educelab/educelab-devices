"""The in-memory GenICam backend that the shared core is tested against."""

import pytest

from contracts.genicam import BackendContract
from educelab.devices.cameras._genicam import Backend, Connection
from fakes.genicam import FakeBackend, FakeCamera


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
