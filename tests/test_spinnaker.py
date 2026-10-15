"""The Spinnaker driver, tested against the fake PySpin in `fakes.pyspin`."""

import subprocess
import sys

import pytest

from educelab.devices.cameras.spinnaker import SpinnakerSystem, load_pyspin
from fakes.pyspin import FakePySpin

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
