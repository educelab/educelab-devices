import numpy as np
import pytest

from educelab.devices import DeviceInfo
from educelab.devices.errors import NotConnected
from educelab.devices.lights import RGB, Channel, ChannelKind, Light


class StubLight(Light):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, object]] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="StubLight")

    @property
    def channels(self) -> tuple[Channel, ...]:
        return (
            Channel("uv", wavelength_nm=365),
            Channel("ring", ChannelKind.RGB),
        )

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _set_level(self, channel: Channel, level: float) -> None:
        self.writes.append((channel.id, level))

    def _set_color(self, channel: Channel, color: RGB) -> None:
        self.writes.append((channel.id, color))


@pytest.fixture
def light():
    with StubLight() as device:
        yield device


def test_set_level_writes_mono_channel(light):
    light.set_level("uv", 0.5)
    assert light.writes == [("uv", 0.5)]


def test_set_level_on_rgb_channel_sets_every_component(light):
    light.set_level("ring", 0.25)
    assert light.writes == [("ring", (0.25, 0.25, 0.25))]


@pytest.mark.parametrize("level", [-0.1, 1.01, float("nan")])
def test_level_outside_range_raises_without_writing(light, level):
    with pytest.raises(ValueError):
        light.set_level("uv", level)
    assert light.writes == []


def test_level_must_be_a_number(light):
    with pytest.raises(TypeError):
        light.set_level("uv", "half")  # type: ignore[arg-type]


def test_unknown_channel_raises_key_error(light):
    with pytest.raises(KeyError, match="ir"):
        light.set_level("ir", 0.5)


def test_set_color_writes_rgb_channel(light):
    light.set_color("ring", (1.0, 0.0, 0.5))
    assert light.writes == [("ring", (1.0, 0.0, 0.5))]


def test_set_color_on_mono_channel_raises(light):
    with pytest.raises(ValueError, match="not RGB"):
        light.set_color("uv", (1.0, 0.0, 0.0))


@pytest.mark.parametrize("color", [(1.0, 0.0), (1.0, 2.0, 0.0), (0.0, 0.0, -1.0)])
def test_set_color_validates_components(light, color):
    with pytest.raises(ValueError):
        light.set_color("ring", color)
    assert light.writes == []


def test_all_off_defaults_to_zeroing_every_channel(light):
    light.all_off()
    assert light.writes == [("uv", 0.0), ("ring", (0.0, 0.0, 0.0))]


def test_light_methods_need_open_device():
    light = StubLight()
    with pytest.raises(NotConnected):
        light.set_level("uv", 0.5)
    with pytest.raises(NotConnected):
        light.all_off()


def test_levels_are_unknown_until_set(light):
    assert light.level("uv") is None
    assert light.color("ring") is None


def test_getters_report_last_value_set(light):
    light.set_level("uv", 0.5)
    light.set_color("ring", (1.0, 0.0, 0.5))
    assert light.level("uv") == 0.5
    assert light.color("ring") == (1.0, 0.0, 0.5)


def test_set_level_on_rgb_channel_is_reported_as_color(light):
    light.set_level("ring", 0.25)
    assert light.color("ring") == (0.25, 0.25, 0.25)


def test_all_off_is_reported(light):
    light.all_off()
    assert light.level("uv") == 0.0
    assert light.color("ring") == (0.0, 0.0, 0.0)


def test_getters_match_channel_kind(light):
    with pytest.raises(ValueError, match="color"):
        light.level("ring")
    with pytest.raises(ValueError, match="level"):
        light.color("uv")


def test_failed_write_is_not_remembered(light):
    def broken(channel: Channel, level: float) -> None:
        raise OSError("port gone")

    light._set_level = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        light.set_level("uv", 0.5)
    assert light.level("uv") is None


def test_levels_are_forgotten_on_close():
    light = StubLight()
    with light:
        light.set_level("uv", 0.5)
    with light:
        assert light.level("uv") is None


def test_numpy_levels_are_accepted(light):
    light.set_level("uv", np.float32(0.5))
    assert light.level("uv") == 0.5
