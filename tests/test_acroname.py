import importlib
import subprocess
import sys

import pytest

from educelab.devices.hubs import acroname


def test_importing_the_driver_does_not_import_brainstem():
    code = (
        "import sys; import educelab.devices.hubs.acroname; "
        "assert 'brainstem' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_brainstem_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "brainstem", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[acroname\]"):
        acroname.load_sdk()


def test_brainstem_library_load_failure_is_an_import_error(monkeypatch):
    # On Windows without the MSVC runtime, brainstem raises OSError at import.
    def fail(name):
        raise OSError("missing MSVC redistributable")

    monkeypatch.setattr(importlib, "import_module", fail)
    with pytest.raises(ImportError, match="acroname-usbhub3p.md") as info:
        acroname.load_sdk()
    assert isinstance(info.value.__cause__, OSError)
