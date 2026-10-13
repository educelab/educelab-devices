import numpy as np
import pytest

from educelab.devices import DeviceInfo
from educelab.devices.cameras import FocusDrive, FocusLimit
from educelab.devices.errors import DeviceTimeout, FocusLimitReached, NotConnected


class StubFocus(FocusDrive):
    def __init__(self) -> None:
        super().__init__()
        self.hit_limit: FocusLimit | None = None
        self.calls: list[object] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="StubFocus")

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _step(self, steps: int) -> None:
        self.calls.append(steps)
        if self.hit_limit:
            raise FocusLimitReached(self.hit_limit)

    def _drive_to_limit(self, limit: FocusLimit) -> None:
        self.calls.append(limit)


@pytest.fixture
def focus():
    with StubFocus() as device:
        yield device


def test_counter_is_unknown_until_a_limit_is_reached(focus):
    focus.step(10)
    assert focus.focus_steps is None
    assert focus.last_limit is None


def test_counter_counts_from_last_limit(focus):
    focus.drive_to_limit("near")
    focus.step(10)
    focus.step(-3)
    assert focus.focus_steps == 7
    assert focus.last_limit == "near"


def test_hitting_a_limit_resets_counter_to_that_limit(focus):
    focus.drive_to_limit("near")
    focus.hit_limit = "far"
    with pytest.raises(FocusLimitReached):
        focus.step(5000)
    assert focus.last_limit == "far"
    assert focus.focus_steps == 0


def test_zero_steps_does_nothing(focus):
    focus.step(0)
    assert focus.calls == []


@pytest.mark.parametrize("steps", [1.5, "3", True])
def test_steps_must_be_an_int(focus, steps):
    with pytest.raises(TypeError):
        focus.step(steps)


def test_drive_to_limit_rejects_unknown_limit(focus):
    with pytest.raises(ValueError):
        focus.drive_to_limit("middle")  # type: ignore[arg-type]


def test_counter_is_forgotten_on_close():
    focus = StubFocus()
    with focus:
        focus.drive_to_limit("far")
    with focus:
        assert focus.focus_steps is None


def test_focus_methods_need_open_device():
    with pytest.raises(NotConnected):
        StubFocus().step(1)


@pytest.mark.parametrize("move", ["step", "drive_to_limit"])
def test_failed_move_loses_the_counter(focus, monkeypatch, move):
    focus.drive_to_limit("near")
    focus.step(10)

    def fail(*args):
        raise DeviceTimeout("lens stopped responding")

    monkeypatch.setattr(focus, f"_{move}", fail)
    with pytest.raises(DeviceTimeout):
        focus.step(500) if move == "step" else focus.drive_to_limit("far")
    assert (focus.focus_steps, focus.last_limit) == (None, None)


def test_numpy_step_counts_are_accepted(focus):
    focus.drive_to_limit("near")
    focus.step(np.int64(7))
    assert focus.calls[-1] == 7
    assert type(focus.focus_steps) is int
