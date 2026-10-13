import sys

import pytest

from educelab.devices import _serial


def test_missing_pyserial_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[serial\]"):
        _serial.list_ports()
