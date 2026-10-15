import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from educelab.devices.cameras import _gphoto2
from fakes.gphoto2 import FakeCamera, FakeLibrary

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


class StubWidget:
    """A python-gphoto2 ``CameraWidget`` with fixed contents."""

    def __init__(self, kind, name, value=None, *, children=(), **extra):
        self.kind, self.name, self.value = kind, name, value
        self.children = list(children)
        self.extra = extra

    def get_type(self):
        return self.kind

    def get_name(self):
        return self.name

    def get_label(self):
        return self.name.title()

    def get_value(self):
        return self.value

    def get_readonly(self):
        return int(self.extra.get("read_only", False))

    def get_children(self):
        return iter(self.children)

    def get_choices(self):
        return iter(self.extra["choices"])

    def get_range(self):
        return self.extra["range"]


WIDGET_TYPES = SimpleNamespace(
    GP_WIDGET_WINDOW=0,
    GP_WIDGET_SECTION=1,
    GP_WIDGET_TEXT=2,
    GP_WIDGET_RANGE=3,
    GP_WIDGET_TOGGLE=4,
    GP_WIDGET_RADIO=5,
    GP_WIDGET_MENU=6,
    GP_WIDGET_BUTTON=7,
    GP_WIDGET_DATE=8,
)


def test_config_tree_is_flattened_by_name():
    t = WIDGET_TYPES
    shutter = StubWidget(
        t.GP_WIDGET_RADIO, "shutterspeed", "1/60", choices=["1/60", "1/30"]
    )
    focus = StubWidget(
        t.GP_WIDGET_RANGE, "manualfocusdrive", 0.0, range=(-32767, 32767, 1)
    )
    serial = StubWidget(t.GP_WIDGET_TEXT, "serialnumber", "6001234", read_only=True)
    duplicate = StubWidget(t.GP_WIDGET_TEXT, "shutterspeed", "ignored")
    root = StubWidget(
        t.GP_WIDGET_WINDOW,
        "main",
        children=[
            StubWidget(t.GP_WIDGET_SECTION, "capturesettings", children=[shutter]),
            StubWidget(t.GP_WIDGET_SECTION, "actions", children=[focus]),
            StubWidget(t.GP_WIDGET_SECTION, "status", children=[serial, duplicate]),
        ],
    )

    widgets = _gphoto2.flatten_config(t, root)

    assert list(widgets) == ["shutterspeed", "manualfocusdrive", "serialnumber"]
    assert widgets["shutterspeed"] == _gphoto2.Widget(
        "shutterspeed", "Shutterspeed", "radio", "1/60", choices=("1/60", "1/30")
    )
    assert widgets["manualfocusdrive"].kind == "range"
    assert (
        widgets["manualfocusdrive"].minimum,
        widgets["manualfocusdrive"].maximum,
    ) == (
        -32767,
        32767,
    )
    assert widgets["serialnumber"].read_only


def test_fakes_fill_the_seam() -> None:
    # Checked by mypy: the fakes must stay interchangeable with the real layer.
    camera: _gphoto2.Session = FakeCamera()
    library: _gphoto2.Library = FakeLibrary(FakeCamera())
    assert camera.model and library.autodetect()
