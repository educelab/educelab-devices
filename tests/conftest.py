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


@pytest.fixture(autouse=True)
def no_live_view_settle(request, monkeypatch):
    """Fakes show a zoom write at once, so don't wait for it. Real cameras
    don't, so hardware tests keep the wait."""
    if request.node.get_closest_marker("hardware"):
        return
    from educelab.devices.cameras import gphoto2

    monkeypatch.setattr(gphoto2, "LIVE_VIEW_SETTLE_S", 0.0)
