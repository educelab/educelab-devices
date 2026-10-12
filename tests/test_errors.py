import sys

import pytest

from educelab.devices import errors
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    FocusLimitReached,
    NotConnected,
    PositionError,
    import_optional,
)


@pytest.mark.parametrize(
    "cls",
    [
        DeviceNotFound,
        NotConnected,
        DeviceTimeout,
        CommandRejected,
        DeviceBusy,
        PositionError,
        FocusLimitReached,
    ],
)
def test_every_error_is_a_device_error(cls):
    assert issubclass(cls, DeviceError)


def test_device_timeout_is_a_timeout_error():
    assert issubclass(DeviceTimeout, TimeoutError)


def test_focus_limit_reached_names_the_limit():
    err = FocusLimitReached("far")
    assert err.limit == "far"
    assert "far" in str(err)


def test_import_optional_returns_the_module():
    assert import_optional("json", extra="serial") is sys.modules["json"]


def test_import_optional_names_the_extra_to_install():
    with pytest.raises(ImportError) as info:
        import_optional("not_a_real_sdk_module", extra="serial")
    message = str(info.value)
    assert "not_a_real_sdk_module" in message
    assert "educelab-devices[serial]" in message
    assert isinstance(info.value.__cause__, ImportError)


def test_import_optional_uses_hint_when_there_is_no_extra():
    with pytest.raises(ImportError, match="Spinnaker SDK"):
        import_optional("not_a_real_sdk_module", hint="Install the Spinnaker SDK.")


def test_import_optional_requires_extra_or_hint():
    with pytest.raises(ValueError):
        import_optional("json")


def test_errors_module_exports_hierarchy():
    assert set(errors.__all__) >= {
        "DeviceError",
        "DeviceNotFound",
        "NotConnected",
        "DeviceTimeout",
        "CommandRejected",
        "DeviceBusy",
        "PositionError",
        "FocusLimitReached",
        "import_optional",
    }
