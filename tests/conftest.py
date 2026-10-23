import pytest

from educelab.devices.app import discover


@pytest.fixture
def discover_only(monkeypatch):
    """Limit ``el-devices discover`` to the named backends, so a test doesn't
    query whatever real hardware is attached."""

    def limit(*names: str) -> None:
        monkeypatch.setattr(
            discover, "BACKENDS", tuple(b for b in discover.BACKENDS if b.name in names)
        )

    return limit
