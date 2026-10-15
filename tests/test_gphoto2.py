import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from educelab.devices.cameras import _gphoto2

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the gphoto2 extra doesn't install on Windows"
)


def fake_sdk():
    """Just enough of the gphoto2 module to install the logging bridge."""
    sdk = SimpleNamespace(
        GP_LOG_ERROR=0, GP_LOG_VERBOSE=1, GP_LOG_DEBUG=2, GP_LOG_DATA=3, added=[]
    )

    def gp_log_add_func(level, func, data=None):
        sdk.added.append((level, func, data))
        return [len(sdk.added), object()]

    sdk.gp_log_add_func = gp_log_add_func
    return sdk


def test_driver_imports_without_the_sdk():
    code = (
        "import sys, educelab.devices.cameras._gphoto2; "
        "assert 'gphoto2' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "gphoto2", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[gphoto2\]"):
        _gphoto2.import_sdk()


def test_libgphoto2_messages_reach_python_logging(caplog):
    sdk = fake_sdk()
    bridge = _gphoto2.LogBridge()
    bridge.install(sdk)
    ((level, forward, data),) = sdk.added
    assert level == sdk.GP_LOG_DEBUG

    with caplog.at_level(logging.DEBUG, logger=_gphoto2.LIBGPHOTO2_LOGGER):
        forward(sdk.GP_LOG_ERROR, "ptp2/usb", "claim failed", data)
        forward(sdk.GP_LOG_DEBUG, "gp_port", "read 12 bytes", data)

    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        (_gphoto2.LIBGPHOTO2_LOGGER, logging.WARNING, "ptp2/usb: claim failed"),
        (_gphoto2.LIBGPHOTO2_LOGGER, logging.DEBUG, "gp_port: read 12 bytes"),
    ]


def test_logging_bridge_installs_once():
    sdk = fake_sdk()
    bridge = _gphoto2.LogBridge()
    bridge.install(sdk)
    bridge.install(sdk)
    assert len(sdk.added) == 1
