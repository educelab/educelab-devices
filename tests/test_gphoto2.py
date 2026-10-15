import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from educelab.devices.cameras import _gphoto2, gphoto2
from educelab.devices.cameras._gphoto2 import (
    GP_ERROR,
    GP_ERROR_IO_USB_CLAIM,
    GP_ERROR_IO_USB_FIND,
    GP_ERROR_MODEL_NOT_FOUND,
)
from educelab.devices.cameras.gphoto2 import GPhoto2Camera
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceNotFound
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
        "import sys, educelab.devices.cameras.gphoto2; "
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


@pytest.fixture(autouse=True)
def no_open_delay(monkeypatch):
    monkeypatch.setattr(gphoto2, "OPEN_RETRY_DELAY_S", 0)


def test_constructor_does_not_touch_the_library():
    library = FakeLibrary(FakeCamera())
    camera = GPhoto2Camera("usb:020,007", library=library)
    assert library.open_attempts == 0
    assert not camera.is_open


def test_open_and_close_the_camera_at_a_port():
    other, wanted = FakeCamera(port="usb:020,006"), FakeCamera(port="usb:020,007")
    camera = GPhoto2Camera("usb:020,007", library=FakeLibrary(other, wanted))

    with camera:
        assert wanted.is_open and not other.is_open
        assert camera.info == DeviceInfo(
            backend="gphoto2", model="Nikon Z 7II", address="usb:020,007"
        )
    assert not wanted.is_open


def test_without_a_port_open_uses_the_only_camera_attached():
    attached = FakeCamera(port="usb:001,004")
    with GPhoto2Camera(library=FakeLibrary(attached)) as camera:
        assert attached.is_open
        assert camera.info.address == "usb:001,004"


def test_without_a_port_open_refuses_to_choose_between_cameras():
    library = FakeLibrary(
        FakeCamera(port="usb:001,004"), FakeCamera(port="usb:001,005")
    )
    camera = GPhoto2Camera(library=library)
    with pytest.raises(DeviceError, match="usb:001,004.*usb:001,005"):
        camera.open()
    assert library.open_attempts == 0
    assert not camera.is_open


def test_claimed_usb_interface_raises_device_busy_naming_ptpcamerad():
    library = FakeLibrary(FakeCamera(), open_errors=[GP_ERROR_IO_USB_CLAIM])
    camera = GPhoto2Camera("usb:020,007", library=library)
    with pytest.raises(DeviceBusy, match="ptpcamerad") as raised:
        camera.open()
    assert "killall" in str(raised.value)
    assert library.open_attempts == 1
    assert not camera.is_open


def test_camera_still_enumerating_is_retried():
    attached = FakeCamera()
    library = FakeLibrary(
        attached, open_errors=[GP_ERROR_MODEL_NOT_FOUND, GP_ERROR_IO_USB_FIND]
    )
    with GPhoto2Camera("usb:020,007", library=library):
        assert attached.is_open
    assert library.open_attempts == 3


def test_camera_that_never_appears_raises_device_not_found():
    library = FakeLibrary(
        FakeCamera(), open_errors=[GP_ERROR_MODEL_NOT_FOUND] * gphoto2.OPEN_ATTEMPTS
    )
    camera = GPhoto2Camera("usb:020,007", library=library)
    with pytest.raises(DeviceNotFound):
        camera.open()
    assert library.open_attempts == gphoto2.OPEN_ATTEMPTS


def test_empty_bus_raises_device_not_found_after_retrying(monkeypatch):
    library = FakeLibrary()
    detections = []
    monkeypatch.setattr(library, "autodetect", lambda: detections.append(1) or [])
    with pytest.raises(DeviceNotFound, match="no camera"):
        GPhoto2Camera(library=library).open()
    assert len(detections) == gphoto2.OPEN_ATTEMPTS


def test_other_open_errors_are_not_retried():
    library = FakeLibrary(FakeCamera(), open_errors=[GP_ERROR])
    with pytest.raises(DeviceError, match=r"\[-1\]"):
        GPhoto2Camera("usb:020,007", library=library).open()
    assert library.open_attempts == 1


def test_close_error_still_leaves_the_camera_closed():
    attached = FakeCamera()
    camera = GPhoto2Camera("usb:020,007", library=FakeLibrary(attached))
    camera.open()
    attached.fail_next("close", GP_ERROR_IO_USB_FIND)
    with pytest.raises(DeviceError):
        camera.close()
    assert not camera.is_open
    camera.open()
    assert camera.is_open
