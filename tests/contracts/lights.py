import pytest

from contracts.device import DeviceContract
from educelab.devices.errors import NotConnected
from educelab.devices.lights import ChannelKind


class LightContract(DeviceContract):
    """Rules every `Light` follows."""

    def test_channels_have_unique_ids(self, device):
        ids = [channel.id for channel in device.channels]
        assert ids
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("level", [0.0, 0.5, 1.0])
    def test_every_channel_accepts_valid_levels(self, device, level):
        for channel in device.channels:
            device.set_level(channel.id, level)

    @pytest.mark.parametrize("level", [-0.01, 1.01, float("nan")])
    def test_level_outside_range_raises(self, device, level):
        with pytest.raises(ValueError):
            device.set_level(device.channels[0].id, level)

    def test_unknown_channel_raises_key_error(self, device):
        with pytest.raises(KeyError):
            device.set_level("no-such-channel", 0.5)

    def test_set_color_follows_channel_kind(self, device):
        for channel in device.channels:
            if channel.kind is ChannelKind.RGB:
                device.set_color(channel.id, (1.0, 0.5, 0.0))
            else:
                with pytest.raises(ValueError):
                    device.set_color(channel.id, (1.0, 0.5, 0.0))

    def test_getters_report_what_was_set(self, device):
        for channel in device.channels:
            if channel.kind is ChannelKind.RGB:
                device.set_color(channel.id, (1.0, 0.5, 0.0))
                assert device.color(channel.id) == pytest.approx((1.0, 0.5, 0.0))
            else:
                device.set_level(channel.id, 0.5)
                assert device.level(channel.id) == pytest.approx(0.5)

    def test_getters_match_channel_kind(self, device):
        for channel in device.channels:
            if channel.kind is ChannelKind.RGB:
                with pytest.raises(ValueError):
                    device.level(channel.id)
            else:
                with pytest.raises(ValueError):
                    device.color(channel.id)

    def test_all_off(self, device):
        for channel in device.channels:
            device.set_level(channel.id, 1.0)
        device.all_off()
        for channel in device.channels:
            if channel.kind is ChannelKind.RGB:
                assert device.color(channel.id) == (0.0, 0.0, 0.0)
            else:
                assert device.level(channel.id) == 0.0

    def test_closed_light_raises_not_connected(self, make_device):
        light = make_device()
        with pytest.raises(NotConnected):
            light.all_off()
