import sys
import types

import pytest

from educelab.devices import Device, DeviceInfo
from educelab.devices.app import discover
from educelab.devices.app.cli import main
from educelab.devices.errors import DeviceBusy, import_optional


class FoundDevice(Device):
    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        return [
            DeviceInfo("fake", "FakeLight", address="/dev/ttyACM0", serial_number="42")
        ]


class MissingSdkDevice(Device):
    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        import_optional("not_a_real_sdk_module", extra="serial")
        return []


class BusyDevice(Device):
    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        raise DeviceBusy("held by ptpcamerad")


@pytest.fixture
def backends(monkeypatch):
    module = types.ModuleType("fake_backend")
    module.FoundDevice = FoundDevice  # type: ignore[attr-defined]
    module.MissingSdkDevice = MissingSdkDevice  # type: ignore[attr-defined]
    module.BusyDevice = BusyDevice  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_backend", module)

    def use(*entries: tuple[str, str]) -> None:
        monkeypatch.setattr(
            discover,
            "BACKENDS",
            tuple(discover.Backend(cls, "fake_backend", cls) for cls, _ in entries),
        )

    return use


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "discover" in out
    assert "simulate" in out


def test_discover_lists_found_devices(backends, capsys):
    backends(("FoundDevice", ""))
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "FakeLight" in out
    assert "/dev/ttyACM0" in out
    assert "42" in out


def test_discover_reports_backends_missing_their_sdk(backends, capsys):
    backends(("MissingSdkDevice", ""), ("FoundDevice", ""))
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "educelab-devices[serial]" in out
    assert "FakeLight" in out


def test_discover_reports_device_errors_and_continues(backends, capsys):
    backends(("BusyDevice", ""), ("FoundDevice", ""))
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "ptpcamerad" in out
    assert "FakeLight" in out


def test_discover_with_nothing_found(backends, capsys):
    backends()
    assert main(["discover"]) == 0
    assert "No devices found" in capsys.readouterr().out


def test_simulate_exercises_every_role(capsys):
    assert main(["simulate"]) == 0
    out = capsys.readouterr().out
    for role in ("light", "stage", "camera", "file-camera", "hub", "focus"):
        assert f"[{role}]" in out


def test_simulate_selected_roles(capsys):
    assert main(["simulate", "hub", "light"]) == 0
    out = capsys.readouterr().out
    assert "[hub]" in out and "[light]" in out
    assert "[stage]" not in out


def test_simulate_rejects_unknown_role(capsys):
    with pytest.raises(SystemExit) as info:
        main(["simulate", "laser"])
    assert info.value.code == 2
