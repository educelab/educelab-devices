import pytest

from contracts import LightContract
from educelab.devices.lights import Channel, ChannelKind, SimulatedLight


class TestSimulatedLight(LightContract):
    @pytest.fixture
    def make_device(self):
        return SimulatedLight


def test_levels_reflect_commands():
    with SimulatedLight() as light:
        light.set_level("white", 0.5)
        light.set_color("rgb", (1.0, 0.0, 0.25))
        assert light.level("white") == 0.5
        assert light.color("rgb") == (1.0, 0.0, 0.25)
        light.all_off()
        assert light.level("white") == 0.0
        assert light.color("rgb") == (0.0, 0.0, 0.0)


def test_channels_are_configurable():
    light = SimulatedLight(channels=(Channel("ring", ChannelKind.RGB),))
    assert [c.id for c in light.channels] == ["ring"]


def test_lights_report_off_before_anything_is_set():
    with SimulatedLight() as light:
        assert light.level("uv") == 0.0
        assert light.color("rgb") == (0.0, 0.0, 0.0)
