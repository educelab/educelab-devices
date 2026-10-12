import logging
import threading
import time

from educelab.devices import Device, DeviceEvent, DeviceInfo, Disconnected, synchronized


class Worker(Device):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="Worker")

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    @synchronized
    def work(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        self.active -= 1

    @synchronized
    def nested(self) -> str:
        self.work()
        return "done"

    def notify(self, event: DeviceEvent) -> None:
        self._emit(event)


def test_concurrent_calls_run_one_at_a_time():
    with Worker() as device:
        threads = [threading.Thread(target=device.work) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert device.max_active == 1


def test_lock_is_reentrant():
    with Worker() as device:
        assert device.nested() == "done"


def test_listener_receives_events():
    received: list[DeviceEvent] = []
    with Worker() as device:
        device.add_listener(received.append)
        event = Disconnected(device, reason="cable pulled")
        device.notify(event)
    assert received == [event]


def test_removed_listener_receives_nothing():
    received: list[DeviceEvent] = []
    with Worker() as device:
        device.add_listener(received.append)
        device.remove_listener(received.append)
        device.notify(DeviceEvent(device))
    assert received == []


def test_adding_a_listener_twice_delivers_once():
    received: list[DeviceEvent] = []
    with Worker() as device:
        device.add_listener(received.append)
        device.add_listener(received.append)
        device.notify(DeviceEvent(device))
    assert len(received) == 1


def test_removing_an_unknown_listener_is_harmless():
    Worker().remove_listener(print)


def test_listener_exception_is_logged_not_raised(caplog):
    received: list[DeviceEvent] = []

    def broken(event: DeviceEvent) -> None:
        raise RuntimeError("consumer bug")

    with Worker() as device:
        device.add_listener(broken)
        device.add_listener(received.append)
        with caplog.at_level(logging.ERROR, logger="educelab.devices.device"):
            device.notify(DeviceEvent(device))
    assert len(received) == 1
    assert "consumer bug" in caplog.text


def test_events_are_delivered_while_another_thread_holds_the_lock():
    received: list[DeviceEvent] = []
    with Worker() as device:
        device.add_listener(received.append)
        with device._lock:
            emitter = threading.Thread(
                target=device.notify, args=(DeviceEvent(device),)
            )
            emitter.start()
            emitter.join(timeout=1)
            assert not emitter.is_alive()
    assert len(received) == 1
