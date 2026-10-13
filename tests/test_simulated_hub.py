import pytest

from contracts import HubContract
from educelab.devices.hubs import SimulatedHub


class TestSimulatedHub(HubContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedHub


def test_ports_are_configurable_and_start_powered():
    with SimulatedHub(ports=(1, 2)) as hub:
        assert hub.ports == (1, 2)
        assert hub.is_powered(1) and hub.is_powered(2)
