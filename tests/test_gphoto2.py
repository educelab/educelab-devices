import logging
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from contracts import FileCameraContract
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
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceNotFound
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
        DeviceInfo("gphoto2", "Nikon Z 7II", "usb:020,006"),
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
