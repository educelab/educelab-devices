"""The Spinnaker driver, tested against the fake PySpin in `fakes.pyspin`."""

import importlib.metadata
import subprocess
import sys
import threading

import pytest

from educelab.devices import DeviceInfo
from educelab.devices.cameras.spinnaker import (
    MIN_SPINNAKER_VERSION,
    SpinnakerCamera,
    SpinnakerSystem,
    load_pyspin,
)
from educelab.devices.errors import DeviceError, NotConnected
from fakes.pyspin import FakeCamera, FakePySpin, LibraryVersion, SpinnakerException

# --- Importing PySpin ---------------------------------------------------------


@pytest.fixture
def no_pyspin(monkeypatch):
    """Make ``import PySpin`` fail even on a machine that has it."""
    monkeypatch.setitem(sys.modules, "PySpin", None)


def test_importing_the_driver_does_not_import_pyspin():
    code = (
        "import sys; sys.modules['PySpin'] = None; "
        "import educelab.devices.cameras, educelab.devices.cameras.spinnaker"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_pyspin_points_to_the_install_docs(no_pyspin):
    with pytest.raises(ImportError, match=r"docs/hardware/spinnaker\.md") as info:
        load_pyspin()
    assert "Spinnaker SDK" in str(info.value)
    assert isinstance(info.value.__cause__, ImportError)


def test_load_pyspin_returns_the_installed_module(monkeypatch):
    fake = FakePySpin()
    monkeypatch.setitem(sys.modules, "PySpin", fake)
    assert load_pyspin() is fake


def test_constructing_a_system_does_not_need_pyspin(no_pyspin):
    SpinnakerSystem()


def test_opening_a_system_without_pyspin_raises_import_error(no_pyspin):
    with pytest.raises(ImportError, match=r"docs/hardware/spinnaker\.md"):
        SpinnakerSystem().open()


def test_an_injected_module_is_used_instead_of_pyspin(no_pyspin):
    fake = FakePySpin()
    with SpinnakerSystem(pyspin=fake):
        assert fake.system.get_instance_calls == 1


# --- SpinnakerSystem lifetime -------------------------------------------------


@pytest.fixture
def pyspin():
    return FakePySpin(
        [
            FakeCamera("20000001", "Blackfly S BFS-U3-51S5M"),
            FakeCamera("20000002", "Blackfly S BFS-U3-200S6C", color=True),
            FakeCamera("20000003", "Blackfly S BFS-U3-51S5M"),
        ]
    )


def test_constructing_a_system_does_not_touch_the_sdk(pyspin):
    system = SpinnakerSystem(pyspin=pyspin)
    assert not system.is_open
    assert pyspin.system.get_instance_calls == 0


def test_open_takes_the_instance_once(pyspin):
    system = SpinnakerSystem(pyspin=pyspin)
    system.open()
    system.open()
    assert system.is_open
    assert pyspin.system.get_instance_calls == 1
    system.close()
    system.close()
    assert not system.is_open
    assert pyspin.system.release_instance_calls == 1
    assert pyspin.system.instances == 0


def test_handles_share_the_instance_until_the_last_closes(pyspin):
    first, second = SpinnakerSystem(pyspin=pyspin), SpinnakerSystem(pyspin=pyspin)
    first.open()
    second.open()
    assert pyspin.system.get_instance_calls == 1
    first.close()
    assert pyspin.system.release_instance_calls == 0
    assert second.discover()
    second.close()
    assert pyspin.system.release_instance_calls == 1
    assert pyspin.system.instances == 0


def test_a_closed_handle_can_reopen(pyspin):
    system = SpinnakerSystem(pyspin=pyspin)
    with system:
        pass
    with system:
        assert system.is_open
    assert pyspin.system.get_instance_calls == 2
    assert pyspin.system.instances == 0


def test_handles_on_different_modules_are_independent(pyspin):
    other = FakePySpin()
    with SpinnakerSystem(pyspin=pyspin), SpinnakerSystem(pyspin=other):
        assert pyspin.system.instances == 1
        assert other.system.instances == 1


def test_get_instance_failure_raises_device_error_and_can_be_retried(pyspin):
    pyspin.system.get_instance_error = SpinnakerException("no transport layer")
    system = SpinnakerSystem(pyspin=pyspin)
    with pytest.raises(DeviceError, match="no transport layer"):
        system.open()
    assert not system.is_open
    pyspin.system.get_instance_error = None
    system.open()
    assert pyspin.system.instances == 1
    system.close()


def test_release_failure_raises_but_leaves_the_handle_closed(pyspin):
    system = SpinnakerSystem(pyspin=pyspin)
    system.open()
    leaked = pyspin.system.GetCameras()
    with pytest.raises(DeviceError, match="not cleared"):
        system.close()
    assert not system.is_open
    leaked.Clear()
    system.open()
    assert pyspin.system.get_instance_calls == 2


def test_library_version(pyspin):
    system = SpinnakerSystem(pyspin=pyspin)
    with pytest.raises(NotConnected):
        system.library_version  # noqa: B018
    with system:
        assert system.library_version == "4.4.0.0"


def test_concurrent_handles_take_and_release_the_instance_once(pyspin):
    handles = [SpinnakerSystem(pyspin=pyspin) for _ in range(8)]
    start = threading.Barrier(len(handles))

    def cycle(handle):
        start.wait()
        for _ in range(20):
            handle.open()
            handle.close()

    threads = [threading.Thread(target=cycle, args=(h,)) for h in handles]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert pyspin.system.instances == 0
    assert pyspin.system.get_instance_calls == pyspin.system.release_instance_calls >= 1


# --- SpinnakerSystem.discover() -----------------------------------------------


def test_discover_lists_every_camera(pyspin):
    # #90: every camera is visited, not just the first.
    infos = SpinnakerSystem(pyspin=pyspin).discover()
    assert infos == [
        DeviceInfo(
            "spinnaker",
            "Blackfly S BFS-U3-51S5M",
            serial_number="20000001",
            vendor="FLIR",
        ),
        DeviceInfo(
            "spinnaker",
            "Blackfly S BFS-U3-200S6C",
            serial_number="20000002",
            vendor="FLIR",
        ),
        DeviceInfo(
            "spinnaker",
            "Blackfly S BFS-U3-51S5M",
            serial_number="20000003",
            vendor="FLIR",
        ),
    ]


def test_discover_releases_everything_it_took(pyspin):
    SpinnakerSystem(pyspin=pyspin).discover()
    assert pyspin.system.uncleared_lists == 0
    assert pyspin.system.live_pointers == 0
    assert pyspin.system.release_instance_calls == 1
    assert pyspin.system.instances == 0


def test_discover_does_not_initialize_cameras(pyspin):
    SpinnakerSystem(pyspin=pyspin).discover()
    assert all(camera.init_calls == 0 for camera in pyspin.system.cameras)


def test_discover_keeps_an_open_handle_open(pyspin):
    with SpinnakerSystem(pyspin=pyspin) as system:
        system.discover()
        assert system.is_open
        assert pyspin.system.instances == 1
        assert pyspin.system.uncleared_lists == 0


def test_discover_with_no_cameras_is_empty():
    assert SpinnakerSystem(pyspin=FakePySpin()).discover() == []


def test_discover_skips_a_camera_unplugged_while_listing(pyspin):
    # Still in the SDK's list, but every read fails.
    pyspin.system.cameras[1].attached = False
    system = SpinnakerSystem(pyspin=pyspin)
    serials = [info.serial_number for info in system.discover()]
    assert serials == ["20000001", "20000003"]
    assert pyspin.system.instances == 0


def test_discover_reports_a_camera_without_a_readable_serial(pyspin):
    pyspin.system.cameras[0].tl_device_nodes["DeviceSerialNumber"]._access = pyspin.NA
    infos = SpinnakerSystem(pyspin=pyspin).discover()
    assert infos[0].serial_number is None
    assert infos[0].model == "Blackfly S BFS-U3-51S5M"
    assert infos[0].vendor == "FLIR"


def test_discover_lists_a_camera_whose_vendor_cant_be_read(pyspin, monkeypatch):
    def fail():
        raise SpinnakerException("node not available")

    node = pyspin.system.cameras[0].tl_device_nodes["DeviceVendorName"]
    monkeypatch.setattr(node, "ToString", fail)
    infos = SpinnakerSystem(pyspin=pyspin).discover()
    assert infos[0].serial_number == "20000001"
    assert infos[0].vendor is None


def test_discover_raises_device_error_when_enumeration_fails(pyspin, monkeypatch):
    def fail():
        raise SpinnakerException("transport layer error")

    monkeypatch.setattr(pyspin.system, "GetCameras", fail)
    with pytest.raises(DeviceError, match="transport layer error"):
        SpinnakerSystem(pyspin=pyspin).discover()
    assert pyspin.system.instances == 0


# --- Minimum SDK version ------------------------------------------------------


@pytest.mark.parametrize(
    "version", [(4, 3, 0, 190), (3, 2, 0, 65), (2, 7, 0, 128), (1, 29, 0, 5)]
)
def test_an_older_sdk_is_refused_naming_both_versions(pyspin, version):
    pyspin.system.version = LibraryVersion(*version)
    system = SpinnakerSystem(pyspin=pyspin)
    installed = ".".join(map(str, version))
    with pytest.raises(ImportError, match=rf"{installed} is installed.*needs 4\.4\.0"):
        system.open()
    assert not system.is_open
    assert pyspin.system.instances == 0


@pytest.mark.parametrize("version", [(4, 4, 0, 246), (4, 4, 1, 0), (5, 0, 0, 1)])
def test_the_minimum_sdk_or_newer_is_accepted(pyspin, version):
    pyspin.system.version = LibraryVersion(*version)
    with SpinnakerSystem(pyspin=pyspin) as system:
        assert system.library_version == ".".join(map(str, version))


def test_an_older_sdk_fails_discovery_and_camera_open(pyspin):
    pyspin.system.version = LibraryVersion(4, 2, 0, 88)
    with pytest.raises(ImportError, match="4.2.0.88"):
        SpinnakerSystem(pyspin=pyspin).discover()
    with pytest.raises(ImportError, match="4.2.0.88"):
        SpinnakerCamera("20000001", pyspin=pyspin).open()
    assert pyspin.system.instances == 0


def test_an_unreadable_sdk_version_is_refused(pyspin, monkeypatch):
    def fail():
        raise SpinnakerException("no version")

    monkeypatch.setattr(pyspin.system, "GetLibraryVersion", fail)
    with pytest.raises(ImportError, match="no version"):
        SpinnakerSystem(pyspin=pyspin).open()
    assert pyspin.system.instances == 0


def test_minimum_version_is_the_checked_release():
    assert MIN_SPINNAKER_VERSION == (4, 4, 0)


def test_installed_pyspin_meets_the_minimum():
    # Reads the installed wheel's version without starting the SDK.
    try:
        installed = importlib.metadata.version("spinnaker-python")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("PySpin isn't installed")
    version = tuple(int(part) for part in installed.split(".")[:3])
    assert version >= MIN_SPINNAKER_VERSION
