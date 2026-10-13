import pytest

from contracts import FocusDriveContract
from educelab.devices.cameras import SimulatedFocusDrive
from educelab.devices.errors import FocusLimitReached


class TestSimulatedFocusDrive(FocusDriveContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedFocusDrive


def test_step_past_far_limit_raises_and_stops_at_limit():
    with SimulatedFocusDrive(travel_steps=100, start=90) as drive:
        with pytest.raises(FocusLimitReached) as info:
            drive.step(20)
        assert info.value.limit == "far"
        assert (drive.last_limit, drive.focus_steps) == ("far", 0)
        drive.step(-10)
        assert drive.focus_steps == -10


def test_step_past_near_limit_raises():
    with SimulatedFocusDrive(travel_steps=100, start=5) as drive:
        with pytest.raises(FocusLimitReached) as info:
            drive.step(-6)
        assert info.value.limit == "near"


def test_start_must_be_within_travel():
    with pytest.raises(ValueError):
        SimulatedFocusDrive(travel_steps=100, start=101)
