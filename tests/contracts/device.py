from collections.abc import Callable, Iterator

import pytest

from educelab.devices import Device, DeviceInfo


class DeviceContract:
    """Lifecycle rules every device follows."""

    @pytest.fixture
    def make_device(self) -> Callable[[], Device]:
        raise NotImplementedError("override make_device in the driver's test class")

    @pytest.fixture
    def device(self, make_device: Callable[[], Device]) -> Iterator[Device]:
        with make_device() as device:
            yield device

    def test_new_device_is_closed(self, make_device):
        assert not make_device().is_open

    def test_open_and_close_are_idempotent(self, make_device):
        device = make_device()
        device.open()
        device.open()
        assert device.is_open
        device.close()
        device.close()
        assert not device.is_open

    def test_device_reopens_after_close(self, make_device):
        device = make_device()
        with device:
            pass
        with device:
            assert device.is_open

    def test_info_is_device_info(self, device):
        assert isinstance(device.info, DeviceInfo)

    def test_discover_returns_device_infos(self, device):
        found = type(device).discover()
        assert isinstance(found, list)
        assert all(isinstance(info, DeviceInfo) for info in found)
