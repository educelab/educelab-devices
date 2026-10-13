import pytest

from contracts.device import DeviceContract
from educelab.devices.errors import NotConnected


class HubContract(DeviceContract):
    """Rules every `Hub` follows."""

    def test_hub_has_ports(self, device):
        assert device.ports
        assert len(device.ports) == len(set(device.ports))

    def test_power_off_and_on(self, device):
        port = device.ports[0]
        device.power_off(port)
        assert not device.is_powered(port)
        device.power_on(port)
        assert device.is_powered(port)

    def test_power_cycle_leaves_port_powered(self, device):
        port = device.ports[-1]
        device.power_off(port)
        device.power_cycle(port, off_time=0)
        assert device.is_powered(port)

    def test_unknown_port_raises_key_error(self, device):
        with pytest.raises(KeyError):
            device.power_on(max(device.ports) + 1)

    def test_closed_hub_raises_not_connected(self, make_device):
        hub = make_device()
        with pytest.raises(NotConnected):
            hub.is_powered(0)
