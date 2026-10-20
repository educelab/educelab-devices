import dataclasses
import logging
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from contracts import FileCameraContract, FocusDriveContract
from educelab.devices.app import discover
from educelab.devices.cameras import _gphoto2, gphoto2
from educelab.devices.cameras._gphoto2 import (
    GP_ERROR,
    GP_ERROR_CAMERA_BUSY,
    GP_ERROR_IO_USB_CLAIM,
    GP_ERROR_IO_USB_FIND,
    GP_ERROR_MODEL_NOT_FOUND,
    Event,
    FilePath,
)
from educelab.devices.cameras.base import (
    APERTURE,
    EXPOSURE_TIME,
    ISO,
    CameraFile,
    Setting,
)
from educelab.devices.cameras.gphoto2 import GPhoto2Camera
from educelab.devices.device import DeviceInfo, Disconnected
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    FocusLimitReached,
    NotConnected,
)
from fakes.gphoto2 import FOLDER, NIKON_WIDGETS, FakeCamera, FakeLibrary, jpeg


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


@pytest.mark.parametrize(
    ("platform", "remedy"),
    [
        ("darwin", r"educelab-devices\[gphoto2\]"),
        ("linux", r"educelab-devices\[gphoto2\]"),
        ("win32", "isn't available on Windows"),
    ],
)
def test_missing_sdk_says_what_to_do(monkeypatch, platform, remedy):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "gphoto2", None)
    with pytest.raises(ImportError, match=remedy):
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
        "shutterspeed",
        "Shutterspeed",
        "radio",
        "1/60",
        choices=("1/60", "1/30"),
        section="capturesettings",
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
    assert widgets["manualfocusdrive"].section == "actions"


class ExplodingWidget(StubWidget):
    def get_value(self):
        raise AssertionError("an unread section's widget was read")


def test_other_section_is_never_read():
    t = WIDGET_TYPES
    iso = StubWidget(t.GP_WIDGET_RADIO, "iso", "100", choices=["100"])
    # On the Z7 II, reading d1fd's NULL value crashes python-gphoto2.
    unnamed = ExplodingWidget(t.GP_WIDGET_TEXT, "d1fd", None)
    root = StubWidget(
        t.GP_WIDGET_WINDOW,
        "main",
        children=[
            StubWidget(t.GP_WIDGET_SECTION, "capturesettings", children=[iso]),
            StubWidget(t.GP_WIDGET_SECTION, "other", children=[unnamed]),
        ],
    )

    assert list(_gphoto2.flatten_config(t, root)) == ["iso"]


class FreedFile:
    """A python-gphoto2 ``CameraFile`` whose buffer is scribbled on when freed."""

    def __init__(self, data: bytes) -> None:
        self.buffer = bytearray(data)

    def get_data_and_size(self):
        return memoryview(self.buffer)

    def __del__(self):
        self.buffer[:4] = b"\x00" * 4


def test_preview_bytes_are_copied_before_the_file_is_freed():
    jpeg = b"\xff\xd8\xff\xdb" + b"x" * 16 + b"\xff\xd9"
    camera = SimpleNamespace(
        get_abilities=lambda: SimpleNamespace(model="Nikon Z7_2"),
        capture_preview=lambda: FreedFile(jpeg),
    )
    gp = SimpleNamespace(GPhoto2Error=RuntimeError)
    session = _gphoto2._LibSession(gp, camera)

    assert session.capture_preview() == jpeg


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
            backend="gphoto2", model="Nikon Z7_2", address="usb:020,007"
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


@pytest.mark.parametrize(
    ("platform", "named", "not_named"),
    [
        ("darwin", ["ptpcamerad", "killall -9 ptpcamerad"], ["gvfs"]),
        ("linux", ["gvfs-gphoto2-volume-monitor"], ["ptpcamerad", "killall"]),
        ("freebsd14", ["Another process holds"], ["ptpcamerad", "gvfs", "killall"]),
    ],
)
def test_claimed_usb_interface_raises_device_busy_with_the_platform_remedy(
    monkeypatch, platform, named, not_named
):
    monkeypatch.setattr(sys, "platform", platform)
    library = FakeLibrary(FakeCamera(), open_errors=[GP_ERROR_IO_USB_CLAIM])
    camera = GPhoto2Camera("usb:020,007", library=library)
    with pytest.raises(DeviceBusy) as raised:
        camera.open()
    message = str(raised.value)
    assert all(text in message for text in named), message
    assert not any(text in message for text in not_named), message
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


def test_discover_lists_attached_cameras_without_opening_them(monkeypatch):
    attached = [
        FakeCamera(port="usb:020,006"),
        FakeCamera("Canon EOS R5", "usb:020,009"),
    ]
    library = FakeLibrary(*attached)
    monkeypatch.setattr(gphoto2, "_default_library", lambda: library)

    assert GPhoto2Camera.discover() == [
        DeviceInfo("gphoto2", "Nikon Z7_2", "usb:020,006"),
        DeviceInfo("gphoto2", "Canon EOS R5", "usb:020,009"),
    ]
    assert library.open_attempts == 0


def test_discover_failure_raises_device_error(monkeypatch):
    library = FakeLibrary()

    def autodetect():
        raise _gphoto2.GPhoto2Error(GP_ERROR, "Unspecified error")

    monkeypatch.setattr(library, "autodetect", autodetect)
    monkeypatch.setattr(gphoto2, "_default_library", lambda: library)
    with pytest.raises(DeviceError, match=r"\[-1\]"):
        GPhoto2Camera.discover()


def test_el_devices_discover_includes_gphoto2():
    assert (
        discover.Backend("gphoto2", "educelab.devices.cameras.gphoto2", "GPhoto2Camera")
        in discover.BACKENDS
    )


@pytest.fixture
def fast_capture(monkeypatch):
    monkeypatch.setattr(gphoto2, "DOWNLOAD_RETRY_DELAY_S", 0)
    monkeypatch.setattr(gphoto2, "EVENT_POLL_S", 0)


def opened(attached):
    camera = GPhoto2Camera(attached.port, library=FakeLibrary(attached))
    camera.open()
    return camera


def test_capture_returns_the_file_in_memory(fast_capture):
    attached = FakeCamera()
    with opened(attached) as camera:
        assert camera.capture_files() == [
            CameraFile("DSC_0001.NEF", b"DSC_0001.NEF contents")
        ]
        assert camera.capture_files()[0].name == "DSC_0002.NEF"


def test_capture_returns_every_file_one_release_produces(fast_capture):
    attached = FakeCamera(suffixes=(".NEF", ".JPG"))
    with opened(attached) as camera:
        files = camera.capture_files()
    assert [(f.name, f.data) for f in files] == [
        ("DSC_0001.NEF", b"DSC_0001.NEF contents"),
        ("DSC_0001.JPG", b"DSC_0001.JPG contents"),
    ]
    assert attached.calls.count("capture") == 1


def test_capture_drains_the_camera_events(fast_capture):
    attached = FakeCamera(suffixes=(".NEF", ".JPG"))
    with opened(attached) as camera:
        camera.capture_files()
        assert not attached.events


def test_file_added_twice_is_returned_once(fast_capture):
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.on_capture = lambda: attached.events.append(
            Event("file_added", FilePath(FOLDER, "DSC_0001.NEF"))
        )
        assert [f.name for f in camera.capture_files()] == ["DSC_0001.NEF"]


def test_files_left_over_from_an_earlier_capture_are_not_returned(fast_capture, caplog):
    attached = FakeCamera()
    with opened(attached) as camera:
        late = FilePath(FOLDER, "DSC_0000.JPG")
        attached.storage[late] = b"late"
        attached.events.append(Event("file_added", late))
        with caplog.at_level(logging.WARNING, logger=gphoto2.__name__):
            files = camera.capture_files()
    assert [f.name for f in files] == ["DSC_0001.NEF"]
    assert "DSC_0000.JPG" in caplog.text


def test_endless_events_stop_the_drain(fast_capture, monkeypatch):
    class Chatty(FakeCamera):
        def wait_for_event(self, timeout_s):
            super().wait_for_event(timeout_s)
            return Event("unknown", detail="PTP Property d1a6 changed")

    monkeypatch.setattr(gphoto2, "DRAIN_LIMIT_S", 0.01)
    with opened(Chatty()) as camera:
        assert [f.name for f in camera.capture_files()] == ["DSC_0001.NEF"]


def test_failed_download_is_retried(fast_capture):
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("download", GP_ERROR, GP_ERROR)
        assert camera.capture_files()[0].data == b"DSC_0001.NEF contents"
    assert attached.calls.count("download") == 3


def test_download_that_keeps_failing_raises(fast_capture):
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("download", *[GP_ERROR] * gphoto2.DOWNLOAD_ATTEMPTS)
        with pytest.raises(DeviceError, match="DSC_0001.NEF"):
            camera.capture_files()
        assert not camera._capture_in_flight.is_set()
        assert camera.capture_files()[0].name == "DSC_0002.NEF"


def test_failed_capture_raises_device_error(fast_capture):
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("capture", GP_ERROR_CAMERA_BUSY)
        with pytest.raises(DeviceBusy):
            camera.capture_files()
        assert not camera._capture_in_flight.is_set()


def test_capture_in_flight_is_flagged_and_blocks_other_captures(fast_capture):
    attached = FakeCamera()
    camera = opened(attached)
    released, second_done = threading.Event(), threading.Event()
    seen_in_flight = []

    def hold_shutter():
        seen_in_flight.append(camera._capture_in_flight.is_set())
        if len(seen_in_flight) == 1:
            released.wait(5)

    attached.on_capture = hold_shutter
    first = threading.Thread(target=camera.capture_files)
    first.start()
    while not seen_in_flight:
        time.sleep(0.001)

    def capture_again():
        camera.capture_files()
        second_done.set()

    second = threading.Thread(target=capture_again)
    second.start()
    assert not second_done.wait(0.05)
    assert attached.calls.count("capture") == 1

    released.set()
    first.join(5)
    second.join(5)
    assert second_done.is_set()
    assert seen_in_flight == [True, True]
    assert not camera._capture_in_flight.is_set()
    camera.close()


class TestGPhoto2Contract(FileCameraContract):
    """The Device, Camera, and FileCamera rules, with keep-alive pinging fast
    so it runs alongside every call."""

    @pytest.fixture(autouse=True)
    def attached(self, monkeypatch, fast_capture, fast_keep_alive):
        library = FakeLibrary(FakeCamera(suffixes=(".NEF", ".JPG")))
        monkeypatch.setattr(gphoto2, "_default_library", lambda: library)
        return library

    @pytest.fixture
    def make_device(self, attached):
        return lambda: GPhoto2Camera("usb:020,007", library=attached)


def at_bulb_and_auto_iso():
    """Nikon widgets with the shutter at Bulb and ISO at Auto."""
    widgets = {w.name: w for w in NIKON_WIDGETS}
    widgets["shutterspeed"] = dataclasses.replace(widgets["shutterspeed"], value="Bulb")
    iso = widgets["iso"]
    widgets["iso"] = dataclasses.replace(
        iso, value="Auto", choices=("Auto", *iso.choices)
    )
    return list(widgets.values())


class TestGPhoto2AtBulbContract(TestGPhoto2Contract):
    """The same rules with the camera at Bulb and Auto ISO, where the canonical
    settings have no value."""

    @pytest.fixture(autouse=True)
    def attached(self, monkeypatch, fast_capture, fast_keep_alive):
        camera = FakeCamera(suffixes=(".NEF", ".JPG"), widgets=at_bulb_and_auto_iso())
        library = FakeLibrary(camera)
        monkeypatch.setattr(gphoto2, "_default_library", lambda: library)
        return library


def settings_of(attached):
    with opened(attached) as camera:
        return camera.settings()


def test_exposure_time_lists_the_shutter_speeds_in_seconds():
    exposure = settings_of(FakeCamera())[EXPOSURE_TIME]
    assert exposure.unit == "s"
    assert exposure.value == pytest.approx(1 / 60)
    assert exposure.choices == pytest.approx(
        [1 / 8000, 1 / 1000, 1 / 60, 1 / 3, 10 / 13, 1.0, 2.5, 30.0]
    )


def test_shutter_speeds_that_are_not_durations_are_left_out():
    exposure = settings_of(FakeCamera())[EXPOSURE_TIME]
    assert all(isinstance(choice, float) for choice in exposure.choices)


def test_canonical_settings_at_bulb_and_auto_have_no_value():
    settings = settings_of(FakeCamera(widgets=at_bulb_and_auto_iso()))
    assert settings[EXPOSURE_TIME].value is None
    assert settings[EXPOSURE_TIME].choices == pytest.approx(
        [1 / 8000, 1 / 1000, 1 / 60, 1 / 3, 10 / 13, 1.0, 2.5, 30.0]
    )
    assert settings[ISO].value is None
    assert settings[ISO].choices == (64, 100, 200, 400, 25600)
    # Bulb isn't offered under the native name either.
    assert "shutterspeed" not in settings


def test_writing_a_number_leaves_bulb_and_auto():
    attached = FakeCamera(widgets=at_bulb_and_auto_iso())
    with opened(attached) as camera:
        exposure = camera.setting(EXPOSURE_TIME)
        camera.set_setting(EXPOSURE_TIME, exposure.nearest(1 / 50))
        camera.set_setting(ISO, 200)
        assert camera.setting(EXPOSURE_TIME).value == pytest.approx(1 / 60)
        assert camera.setting(ISO).value == 200
    assert attached.widgets["shutterspeed"].value == "1/60"
    assert attached.widgets["iso"].value == "200"


def test_widget_with_no_numeric_choices_is_canonical_with_none_allowed():
    widgets = {w.name: w for w in NIKON_WIDGETS}
    widgets["iso"] = dataclasses.replace(
        widgets["iso"], value="Auto", choices=("Auto",)
    )
    attached = FakeCamera(widgets=list(widgets.values()))
    with opened(attached) as camera:
        iso = camera.setting(ISO)
        assert (iso.value, iso.choices) == (None, ())
        with pytest.raises(ValueError):
            camera.set_setting(ISO, 100)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1/8000", 1 / 8000),
        ("10/13", 10 / 13),
        ("30", 30.0),
        ("2.5", 2.5),
        ("0.5s", 0.5),
        ("1/250s", 1 / 250),
        ("Bulb", None),
        ("Time", None),
        ("x 1/200", None),
    ],
)
def test_shutter_speed_text_is_read_as_seconds(text, seconds):
    assert gphoto2.shutter_seconds(text) == (
        None if seconds is None else pytest.approx(seconds)
    )


def test_nearest_exposure_time_is_written_as_the_native_shutter_speed():
    attached = FakeCamera()
    with opened(attached) as camera:
        exposure = camera.setting(EXPOSURE_TIME)
        camera.set_setting(EXPOSURE_TIME, exposure.nearest(0.4))
        assert attached.widgets["shutterspeed"].value == "1/3"
        assert camera.setting(EXPOSURE_TIME).value == pytest.approx(1 / 3)


def test_exposure_time_off_the_shutter_speed_list_raises():
    with opened(FakeCamera()) as camera, pytest.raises(ValueError):
        camera.set_setting(EXPOSURE_TIME, 0.4)


def test_iso_and_aperture_are_canonical_numbers():
    settings = settings_of(FakeCamera())
    assert settings[ISO].value == 100
    assert settings[ISO].choices == (64, 100, 200, 400, 25600)
    assert settings[APERTURE].value == 8.0
    assert settings[APERTURE].choices == (4.0, 5.6, 8.0, 11.0)
    assert settings[APERTURE].unit == "f-number"


def test_canonical_settings_are_written_natively():
    attached = FakeCamera()
    with opened(attached) as camera:
        camera.set_setting(ISO, 400)
        camera.set_setting(APERTURE, 5.6)
    assert attached.widgets["iso"].value == "400"
    assert attached.widgets["f-number"].value == "f/5.6"


def test_other_widgets_are_settings_by_native_name():
    settings = settings_of(FakeCamera())
    assert settings["imagequality"] == Setting(
        "imagequality",
        "NEF (Raw)",
        choices=("JPEG Fine", "NEF (Raw)", "NEF+Fine"),
    )
    assert settings["colortemperature"] == Setting(
        "colortemperature", 5000.0, 2500.0, 10000.0, 10.0
    )
    assert settings["artist"] == Setting("artist", "")
    assert settings["serialnumber"].read_only
    assert "shutterspeed" not in settings
    assert "f-number" not in settings


def test_actions_are_not_settings():
    settings = settings_of(FakeCamera())
    assert not {"viewfinder", "autofocusdrive", "manualfocusdrive"} & set(settings)


def test_toggles_are_booleans():
    toggle = _gphoto2.Widget("recordingmedia", "Media", "toggle", 1, section="settings")
    attached = FakeCamera(widgets=[*NIKON_WIDGETS, toggle])
    with opened(attached) as camera:
        assert camera.setting("recordingmedia").value is True
        camera.set_setting("recordingmedia", False)
    assert attached.widgets["recordingmedia"].value == 0


def test_camera_refusing_a_setting_raises_device_error():
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("set_config", GP_ERROR)
        with pytest.raises(DeviceError, match="imagequality"):
            camera.set_setting("imagequality", "JPEG Fine")


def test_preview_is_a_decoded_rgb_array():
    with opened(FakeCamera(preview=jpeg(64, 48))) as camera:
        preview = camera.preview_frame()
    assert preview.shape == (48, 64, 3)
    assert preview.dtype == np.uint8


def test_first_preview_turns_live_view_on_and_continuous_af_off():
    attached = FakeCamera()
    with opened(attached) as camera:
        camera.preview_frame()
        camera.preview_frame()
        assert attached.writes == [{"viewfinder": 1, "continuousaf": "Off"}]
        assert attached.calls.count("capture_preview") == 2


@pytest.mark.parametrize("name", ["eosviewfinder", "d1a6"])
def test_live_view_uses_the_viewfinder_widget_the_camera_has(name):
    widgets = [w for w in NIKON_WIDGETS if w.name not in ("viewfinder", "continuousaf")]
    widgets.append(_gphoto2.Widget(name, name, "toggle", 0, section="actions"))
    attached = FakeCamera(widgets=widgets)
    with opened(attached) as camera:
        camera.preview_frame()
        assert attached.writes == [{name: 1}]


def test_continuous_af_toggle_is_turned_off():
    widgets = [w for w in NIKON_WIDGETS if w.name != "continuousaf"]
    widgets.append(
        _gphoto2.Widget("continuousaf", "AF", "toggle", 1, section="capturesettings")
    )
    attached = FakeCamera(widgets=widgets)
    with opened(attached) as camera:
        camera.preview_frame()
        assert attached.writes == [{"viewfinder": 1, "continuousaf": 0}]


def test_preview_works_without_live_view_widgets():
    names = ("viewfinder", "continuousaf")
    attached = FakeCamera(widgets=[w for w in NIKON_WIDGETS if w.name not in names])
    with opened(attached) as camera:
        assert camera.preview_frame().ndim == 3
        assert attached.writes == []


def test_live_view_that_cannot_be_enabled_still_previews(caplog):
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("set_config", GP_ERROR)
        with caplog.at_level(logging.WARNING, logger=gphoto2.__name__):
            assert camera.preview_frame().ndim == 3
            camera.preview_frame()
        assert "live view" in caplog.text
        assert attached.calls.count("set_config") == 1


def test_undecodable_preview_raises_device_error():
    camera = opened(FakeCamera(preview=b"not a jpeg"))
    with camera, pytest.raises(DeviceError, match="preview"):
        camera.preview_frame()


def test_failed_preview_raises_device_error():
    attached = FakeCamera()
    with opened(attached) as camera:
        attached.fail_next("capture_preview", GP_ERROR)
        with pytest.raises(DeviceError, match="preview"):
            camera.preview_frame()


def test_close_turns_live_view_off_and_reopen_turns_it_on_again():
    attached = FakeCamera()
    camera = opened(attached)
    camera.preview_frame()
    camera.close()
    assert attached.writes[-1] == {"viewfinder": 0}
    camera.open()
    camera.preview_frame()
    camera.close()
    # Continuous AF is still off from the first session.
    assert attached.writes[-2] == {"viewfinder": 1}


def test_close_without_preview_leaves_live_view_alone():
    attached = FakeCamera()
    with opened(attached):
        pass
    assert attached.writes == []


@pytest.fixture
def fast_keep_alive(monkeypatch):
    monkeypatch.setattr(gphoto2, "KEEP_ALIVE_INTERVAL_S", 0.005)


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


def pings(attached):
    return attached.calls.count("wait_for_event")


def test_keep_alive_pings_an_idle_camera_until_close(fast_keep_alive):
    attached = FakeCamera()
    camera = opened(attached)
    wait_until(lambda: pings(attached) >= 3)
    thread = camera._keep_alive_thread
    assert thread is not None and thread.daemon
    camera.close()
    assert not thread.is_alive()
    after_close = pings(attached)
    time.sleep(0.03)
    assert pings(attached) == after_close


def test_keep_alive_restarts_on_reopen(fast_keep_alive):
    attached = FakeCamera()
    camera = opened(attached)
    camera.close()
    camera.open()
    before = pings(attached)
    wait_until(lambda: pings(attached) > before)
    camera.close()


def test_keep_alive_pauses_during_capture(fast_keep_alive, fast_capture):
    attached = FakeCamera()
    camera = opened(attached)
    wait_until(lambda: pings(attached) >= 1)
    during = []

    def hold_shutter():
        start = pings(attached)
        time.sleep(0.05)
        during.append(pings(attached) - start)

    attached.on_capture = hold_shutter
    camera.capture_files()
    camera.close()
    assert during == [0]


def test_keep_alive_failure_is_logged_and_pinging_continues(fast_keep_alive, caplog):
    attached = FakeCamera()
    attached.fail_next("wait_for_event", GP_ERROR)
    with caplog.at_level(logging.WARNING, logger=gphoto2.__name__):
        camera = opened(attached)
        wait_until(lambda: pings(attached) >= 3)
        camera.close()
    assert "keep-alive" in caplog.text


def test_keep_alive_reports_files_added_between_captures(fast_keep_alive, caplog):
    attached = FakeCamera()
    attached.events.append(Event("file_added", FilePath(FOLDER, "DSC_0000.JPG")))
    with caplog.at_level(logging.WARNING, logger=gphoto2.__name__):
        camera = opened(attached)
        wait_until(lambda: not attached.events)
        camera.close()
    assert "DSC_0000.JPG" in caplog.text


def disconnections(camera):
    events = []
    camera.add_listener(
        lambda event: events.append(event) if isinstance(event, Disconnected) else None
    )
    return events


def test_three_failed_pings_in_a_row_report_disconnected(fast_keep_alive, caplog):
    attached = FakeCamera()
    camera = GPhoto2Camera(attached.port, library=FakeLibrary(attached))
    events = disconnections(camera)
    attached.fail_next("wait_for_event", *[GP_ERROR_IO_USB_FIND] * 5)
    with caplog.at_level(logging.WARNING, logger=gphoto2.__name__):
        camera.open()
        wait_until(lambda: pings(attached) >= 8)
        camera.close()
    assert len(events) == 1
    assert events[0].device is camera
    assert "-52" in events[0].reason
    assert caplog.text.count("keep-alive ping failed") == 5


def test_failures_interrupted_by_a_good_ping_are_not_a_disconnection(fast_keep_alive):
    attached = FakeCamera()
    camera = GPhoto2Camera(attached.port, library=FakeLibrary(attached))
    events = disconnections(camera)
    real_wait = attached.wait_for_event
    outcomes = iter([GP_ERROR, GP_ERROR, None, GP_ERROR, GP_ERROR, None])

    def flaky(timeout_s):
        code = next(outcomes, None)
        if code is not None:
            attached.calls.append("wait_for_event")
            raise _gphoto2.GPhoto2Error(code, "flaky")
        return real_wait(timeout_s)

    attached.wait_for_event = flaky
    camera.open()
    wait_until(lambda: pings(attached) >= 8)
    camera.close()
    assert events == []


def test_listener_can_close_the_camera_on_disconnection(fast_keep_alive):
    attached = FakeCamera()
    camera = GPhoto2Camera(attached.port, library=FakeLibrary(attached))
    closed = threading.Event()

    def close_on_disconnect(event):
        if isinstance(event, Disconnected):
            event.device.close()
            closed.set()

    camera.add_listener(close_on_disconnect)
    attached.fail_next("wait_for_event", *[GP_ERROR] * 3)
    camera.open()
    assert closed.wait(5)
    assert not camera.is_open


@pytest.fixture
def focus_waits(monkeypatch):
    """Record the focus drive's waits instead of sleeping."""
    waits = []
    monkeypatch.setattr(gphoto2, "_wait", waits.append)
    return waits


def focus_of(attached):
    camera = opened(attached)
    focus = camera.focus
    assert focus is not None
    return camera, focus


def test_step_drives_focus_by_that_many_steps(focus_waits):
    attached = FakeCamera(focus_position=2500)
    camera, focus = focus_of(attached)
    focus.step(300)
    focus.step(-100)
    camera.close()
    assert attached.focus_moves == [300.0, -100.0]
    assert attached.focus_position == 2700


def test_large_steps_are_chunked(focus_waits, monkeypatch):
    monkeypatch.setattr(gphoto2, "FOCUS_CHUNK_STEPS", 1000)
    attached = FakeCamera(focus_position=2500, focus_travel=10000)
    camera, focus = focus_of(attached)
    focus.step(2500)
    focus.step(-2500)
    camera.close()
    assert attached.focus_moves == [1000, 1000, 500, -1000, -1000, -500]


def test_chunks_never_exceed_the_widget_range(focus_waits):
    widgets = [w for w in NIKON_WIDGETS if w.name != "manualfocusdrive"]
    widgets.append(
        _gphoto2.Widget(
            "manualfocusdrive",
            "MF",
            "range",
            0.0,
            False,
            (),
            -200.0,
            200.0,
            1.0,
            section="actions",
        )
    )
    attached = FakeCamera(widgets=widgets)
    camera, focus = focus_of(attached)
    focus.step(500)
    camera.close()
    assert attached.focus_moves == [200, 200, 100]


def test_each_chunk_waits_to_settle(focus_waits, monkeypatch):
    monkeypatch.setattr(gphoto2, "FOCUS_CHUNK_STEPS", 1000)
    camera, focus = focus_of(FakeCamera(focus_position=0, focus_travel=10000))
    focus.step(2500)
    camera.close()
    assert focus_waits == pytest.approx([0.6, 0.6, 0.35])


@pytest.mark.parametrize(
    ("steps", "seconds"), [(1, 0.1005), (-1000, 0.6), (5800, 3.0), (32767, 3.0)]
)
def test_settle_time_grows_with_the_move_up_to_a_cap(steps, seconds):
    assert gphoto2.focus_settle_s(steps) == pytest.approx(seconds)


def test_busy_focus_drive_is_retried(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    attached.fail_next("set_config", GP_ERROR_CAMERA_BUSY, GP_ERROR_CAMERA_BUSY)
    focus.step(100)
    camera.close()
    assert attached.focus_moves == [100]
    assert focus_waits.count(gphoto2.FOCUS_BUSY_WAIT_S) == 2


def test_focus_drive_that_stays_busy_raises(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    attached.fail_next(
        "set_config", *[GP_ERROR_CAMERA_BUSY] * gphoto2.FOCUS_BUSY_ATTEMPTS
    )
    with pytest.raises(DeviceBusy):
        focus.step(100)
    camera.close()
    assert attached.focus_moves == []


@pytest.mark.parametrize(
    ("start", "steps", "limit", "end"),
    [(4900, 300, "far", 5000), (100, -300, "near", 0)],
)
def test_end_stop_raises_focus_limit_reached(focus_waits, start, steps, limit, end):
    attached = FakeCamera(focus_position=start, focus_travel=5000)
    camera, focus = focus_of(attached)
    with pytest.raises(FocusLimitReached) as raised:
        focus.step(steps)
    assert raised.value.limit == limit
    assert (focus.last_limit, focus.focus_steps) == (limit, 0)
    focus.step(-50 if limit == "far" else 50)
    assert focus.focus_steps == (-50 if limit == "far" else 50)
    camera.close()
    assert attached.focus_position == end + (-50 if limit == "far" else 50)


def test_end_stop_stops_the_remaining_chunks(focus_waits, monkeypatch):
    monkeypatch.setattr(gphoto2, "FOCUS_CHUNK_STEPS", 1000)
    attached = FakeCamera(focus_position=4500, focus_travel=5000)
    camera, focus = focus_of(attached)
    with pytest.raises(FocusLimitReached):
        focus.step(2500)
    camera.close()
    assert attached.focus_moves == [1000]


def test_end_stop_drains_events_and_waits_to_recover(focus_waits):
    attached = FakeCamera(focus_position=4900, focus_travel=5000)
    camera, focus = focus_of(attached)
    attached.events.append(Event("unknown", detail="PTP Property d1a6 changed"))
    with pytest.raises(FocusLimitReached):
        focus.step(300)
    camera.close()
    assert not attached.events
    assert focus_waits[-1] == gphoto2.FOCUS_LIMIT_RECOVERY_S


def test_other_focus_errors_raise_and_lose_the_count(focus_waits):
    attached = FakeCamera(focus_position=4900, focus_travel=5000)
    camera, focus = focus_of(attached)
    with pytest.raises(FocusLimitReached):
        focus.step(300)
    attached.fail_next("set_config", GP_ERROR)
    with pytest.raises(DeviceError, match="focus"):
        focus.step(-100)
    camera.close()
    assert focus.focus_steps is None


@pytest.mark.parametrize(
    ("limit", "end", "moves"),
    [("near", 0, [-32767, -32767]), ("far", 100000, [32767, 32767])],
)
def test_drive_to_limit_steps_until_the_end_stop(focus_waits, limit, end, moves):
    attached = FakeCamera(focus_position=50000, focus_travel=100000)
    camera, focus = focus_of(attached)
    focus.drive_to_limit(limit)
    camera.close()
    assert attached.focus_position == end
    assert attached.focus_moves == moves


def test_drive_to_limit_resets_the_step_counter(focus_waits):
    camera, focus = focus_of(FakeCamera())
    focus.drive_to_limit("near")
    assert (focus.last_limit, focus.focus_steps) == ("near", 0)
    focus.step(400)
    assert focus.focus_steps == 400
    focus.drive_to_limit("far")
    assert (focus.last_limit, focus.focus_steps) == ("far", 0)
    camera.close()


def test_drive_to_limit_at_the_limit_already(focus_waits):
    attached = FakeCamera(focus_position=0)
    camera, focus = focus_of(attached)
    focus.drive_to_limit("near")
    camera.close()
    assert attached.focus_moves == [-32767]


def test_drive_to_limit_without_an_end_stop_raises(focus_waits, monkeypatch):
    monkeypatch.setattr(gphoto2, "FOCUS_LIMIT_MAX_STEPS", 100_000)
    attached = FakeCamera(focus_position=500_000, focus_travel=1_000_000)
    camera, focus = focus_of(attached)
    with pytest.raises(DeviceError, match="no near limit"):
        focus.drive_to_limit("near")
    camera.close()
    assert len(attached.focus_moves) == 4
    assert focus.focus_steps is None


def test_drive_to_limit_failure_raises_and_loses_the_count(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    focus.drive_to_limit("near")
    attached.fail_next("set_config", GP_ERROR)
    with pytest.raises(DeviceError, match="focus"):
        focus.drive_to_limit("far")
    camera.close()
    assert (focus.last_limit, focus.focus_steps) == (None, None)


def without_focus_widget():
    return [w for w in NIKON_WIDGETS if w.name != "manualfocusdrive"]


def test_focus_is_none_without_a_manualfocusdrive_widget():
    with opened(FakeCamera(widgets=without_focus_widget())) as camera:
        assert camera.focus is None


def test_focus_is_none_for_a_choice_type_manualfocusdrive():
    canon = _gphoto2.Widget(
        "manualfocusdrive",
        "Drive Canon DSLR Manual focus",
        "radio",
        "None",
        choices=("Near 3", "Near 2", "Near 1", "None", "Far 1", "Far 2", "Far 3"),
        section="actions",
    )
    with opened(FakeCamera(widgets=[*without_focus_widget(), canon])) as camera:
        assert camera.focus is None


def test_focus_of_a_closed_camera_raises_not_connected():
    camera = GPhoto2Camera("usb:020,007", library=FakeLibrary(FakeCamera()))
    with pytest.raises(NotConnected):
        _ = camera.focus


def test_focus_is_checked_once_per_session():
    attached = FakeCamera()
    with opened(attached) as camera:
        focus = camera.focus
        assert camera.focus is focus
        assert attached.calls.count("config") == 1


def test_closing_the_camera_closes_its_focus_drive(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    focus.drive_to_limit("near")
    camera.close()
    assert not focus.is_open
    assert focus.focus_steps is None
    with pytest.raises(NotConnected):
        focus.step(1)
    with pytest.raises(NotConnected):
        focus.open()

    camera.open()
    assert camera.focus is focus and focus.is_open
    camera.close()


def test_closing_the_focus_drive_leaves_the_camera_open(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    focus.drive_to_limit("near")
    focus.close()
    assert camera.is_open and attached.is_open
    assert camera.focus is focus and not focus.is_open
    focus.open()
    assert focus.focus_steps is None
    assert camera.focus is focus
    camera.close()


def test_focus_drive_reports_the_camera_identity():
    with opened(FakeCamera()) as camera:
        assert camera.focus.info == camera.info


def test_reopening_a_focus_drive_rechecks_the_camera(focus_waits):
    attached = FakeCamera()
    camera, focus = focus_of(attached)
    camera.close()
    attached.widgets.pop("manualfocusdrive")
    camera.open()
    with pytest.raises(DeviceError, match="no longer drive focus"):
        focus.open()
    assert camera.focus is None
    camera.close()


class TestGPhoto2FocusDriveContract(FocusDriveContract):
    """The FocusDrive rules for ``camera.focus``, with keep-alive running."""

    @pytest.fixture
    def make_device(self, focus_waits, fast_keep_alive):
        cameras = []

        def make():
            camera = opened(FakeCamera())
            cameras.append(camera)
            focus = camera.focus
            focus.close()
            return focus

        yield make
        for camera in cameras:
            camera.close()
