import pytest

from contracts.device import DeviceContract
from educelab.devices.errors import FocusLimitReached, NotConnected


class FocusDriveContract(DeviceContract):
    """Rules every `FocusDrive` follows."""

    def test_counter_is_unknown_after_open(self, device):
        assert device.focus_steps is None
        assert device.last_limit is None

    def test_counter_counts_from_near_limit(self, device):
        device.drive_to_limit("near")
        assert (device.last_limit, device.focus_steps) == ("near", 0)
        device.step(5)
        device.step(-2)
        assert device.focus_steps == 3

    def test_counter_counts_from_far_limit(self, device):
        device.drive_to_limit("far")
        assert (device.last_limit, device.focus_steps) == ("far", 0)
        device.step(-4)
        assert device.focus_steps == -4

    @pytest.mark.parametrize(("limit", "steps"), [("near", -1), ("far", 1)])
    def test_stepping_past_a_limit_raises(self, device, limit, steps):
        device.drive_to_limit(limit)
        with pytest.raises(FocusLimitReached) as info:
            device.step(steps)
        assert info.value.limit == limit
        assert (device.last_limit, device.focus_steps) == (limit, 0)

    def test_unknown_limit_raises(self, device):
        with pytest.raises(ValueError):
            device.drive_to_limit("middle")

    def test_non_integer_steps_raise(self, device):
        with pytest.raises(TypeError):
            device.step(1.5)

    def test_counter_is_forgotten_on_reopen(self, device):
        device.drive_to_limit("near")
        device.close()
        device.open()
        assert device.focus_steps is None

    def test_closed_focus_drive_raises_not_connected(self, make_device):
        drive = make_device()
        with pytest.raises(NotConnected):
            drive.step(1)
