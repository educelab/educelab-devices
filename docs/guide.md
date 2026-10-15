# Using `educelab.devices`

A guide for consumers: the applications and scripts that import devices from this
package, whether that's a capture script, a Qt app, or a test. It covers how every device
behaves, whatever its hardware. Device-specific behavior is in the hardware docs
([`hardware/`](hardware/README.md)), and the reasons behind the design are in the ADRs
([`adr/`](adr/)).

The short version: a device is a blocking, thread-safe handle on a piece of hardware that
remembers only what it learned since `open()`. Most of what follows comes from that.

---

## 1. Facts that shape every call

1. **Every method blocks until the hardware is done.** For a stage move, "done" means
   the stage has arrived. There are no futures, coroutines, or Qt signals (ADR 0001). A
   consumer that needs a responsive UI calls devices from a worker thread.
2. **One call at a time per device.** Each device has a lock, and public methods take it,
   so calls from different threads run one after another instead of interleaving. The
   exceptions are `Stage.stop()` and `Stage.position()` (§4).
3. **Devices persist nothing.** No cached addresses, origins, or settings files. A device
   forgets everything it learned (homing, focus position) when it closes. If you want
   something remembered, your application owns that, along with the hazard of it going
   stale.
4. **Roles, not vendors.** Code against `Camera`, `Light`, `Stage`, `Hub`, or `FocusDrive`.
   One device can offer several roles; a turntable with a built-in light is both a
   `Stage` and a `Light`.
5. **Positions are machine coordinates.** A stage reports what the hardware counts, not
   an origin you pinned (ADR 0003).

---

## 2. Lifecycle

```python
from educelab.devices.lights import SimulatedLight

light = SimulatedLight()  # stores configuration; no I/O
light.open()  # connects
try:
    light.set_level("uv", 0.5)
finally:
    light.close()
```

The context manager does exactly this:

```python
with SimulatedLight() as light:
    light.set_level("uv", 0.5)
```

Both forms are supported on purpose. A long-lived worker that connects on one event and
disconnects on another can't use a `with` block, so `open()` and `close()` are public and
**idempotent**. Calling `open()` on an open device does nothing, and so does `close()` on a
closed one. A closed device can be opened again.

- **Constructors never touch hardware.** You can build devices from a config file, pass
  them around, and open them later.
- **`close()` always leaves the device closed.** If disconnecting fails, the error is
  raised, but `is_open` is already `False`, so a retry of `open()` starts clean.
- **Calling a role method on a closed device raises `NotConnected`.**
- **`discover()` is a classmethod** that lists devices of that type attached to the host,
  as `DeviceInfo`s, without opening them. Drivers that can't enumerate return `[]`.
  `el-devices discover` runs every installed backend's `discover()`.

---

## 3. Blocking and timeouts

Long operations take a `timeout` in seconds. `None` means the device's default (for
stages, `Stage.move_timeout`). A device that doesn't finish in time raises
`DeviceTimeout`, which is also a `TimeoutError`.

Pass your own timeout when you know something the default doesn't, e.g. a heavy load
that accelerates slowly, or a test where you'd rather fail fast. Defaults are deliberately
loose: a false timeout on a healthy move is worse than a late one on a wedged move.

After a timeout, the hardware's state is suspect. A move that timed out may have stopped
partway, so read `position()` before assuming where the stage is.

---

## 4. Threading

Public methods are thread-safe and serialized by a per-device reentrant lock. Two threads
can share a device safely. They just take turns.

Two `Stage` methods don't take the lock, because they have to work *during* a move:

- **`stop()`** can be called from any thread while another thread is blocked in
  `move_to()`. The interrupted move raises `PositionError`.
- **`position()`** returns the last known position without waiting for a move to finish,
  so while the stage is moving the value may be slightly stale.

A driver may run its own threads or a private asyncio loop (e.g. for BLE). They never
appear in the public API.

---

## 5. Errors

There are two families, and they mean opposite things about who made the mistake.

**Your arguments**, checked before anything reaches the hardware, raise built-in
exceptions. These are bugs in the calling code, so don't catch them to paper over a
problem.

| Exception | Raised for |
|---|---|
| `KeyError` | An unknown channel ID, axis name, port, or setting name |
| `ValueError` | A value out of range: a light level outside 0.0–1.0, a setting outside its limits, a `set_color()` on a mono channel |
| `TypeError` | A value of the wrong type: a string level, a float step count |

**The device or its connection** raises a `DeviceError` (in `educelab.devices.errors`):

| Exception | Means |
|---|---|
| `DeviceNotFound` | Nothing answered at the configured address |
| `NotConnected` | The device isn't open, or its connection was lost |
| `DeviceTimeout` | The device didn't respond or finish in time (also a `TimeoutError`) |
| `CommandRejected` | The device received a command and refused it |
| `DeviceBusy` | Another process holds the device, or it's busy with another operation |
| `PositionError` | A stage target is outside its axis's limits, the stage needs homing, or `stop()` interrupted a move |
| `FocusLimitReached` | A focus drive hit its near or far limit; `.limit` says which |

Catch `DeviceError` for "the hardware didn't cooperate". Error messages name the device
and, when there's a known remedy, say what to try.

**Missing SDKs** raise `ImportError` when you first use the backend, not when you import
the package. The message names the extra to install, e.g.
`pip install 'educelab-devices[serial]'`.

---

## 6. Events

A few things happen without a method call, such as a connection dropping. Devices report
these to listeners:

```python
from educelab.devices import Disconnected


def on_event(event):
    if isinstance(event, Disconnected):
        print(f"{event.device!r} lost its connection: {event.reason}")


device.add_listener(on_event)
```

- Listeners receive a `DeviceEvent` subclass and can dispatch on its type.
- They may be called from a driver's internal thread, so return quickly. A Qt consumer
  re-emits the event as a signal.
- An exception in a listener is logged, not raised into the device.
- Adding a listener twice has no effect; removing one that was never added is harmless.

---

## 7. Logging

Every driver logs through `logging.getLogger(__name__)` under `educelab.devices`. Raw
protocol traffic is at `DEBUG`. Devices never `print`. To see traffic:

```python
import logging

logging.getLogger("educelab.devices").setLevel(logging.DEBUG)
```

or run `el-devices -v <command>`.

---

## 8. What the library deliberately doesn't do

Each of these is a decision, not an omission. If you need the behavior, it belongs in your
code.

| Not done | Why, and where it goes instead |
|---|---|
| Persist anything | A stored origin or address can go stale without anyone noticing. The code that persists it owns the warning. `CameraFile.save()` is the only file the library writes |
| Pin origins on stages | Stages use machine coordinates (ADR 0003). Offsets belong to the consumer |
| Home on connect | Homing moves hardware and is always an explicit `home()` |
| Decode raw camera files | Cameras return files exactly as produced (ADR 0002). Decode the bytes in memory if you need pixels |
| Retry commands | Whether a retry is safe depends on the device and on what the consumer is doing |
| Sequence a capture or manage a rig | Rigs and workflows belong to consumers |
| Offer framework wrappers | Qt or asyncio wrappers live in consumers until two of them need the same one (ADR 0001) |

---

## 9. Roles at a glance

| Role | Methods |
|---|---|
| `Camera` | `settings()`, `setting(name)`, `set_setting(name, value)`, `preview_frame()` |
| `FrameCamera` | adds `capture_frame() -> Frame` |
| `FileCamera` | adds `capture_files() -> list[CameraFile]`; save with `CameraFile.save(dir, stem)`, which refuses to overwrite unless asked |
| `FocusDrive` | `step(n)` (positive = toward far), `drive_to_limit("near" \| "far")`, `focus_steps`, `last_limit` |
| `Light` | `channels`, `set_level(id, 0.0–1.0)`, `set_color(id, (r, g, b))`, `level(id)`, `color(id)`, `all_off()` |
| `Stage` | `axes`, `move_to({axis: pos})`, `move_by({axis: offset})`, `jog(axis, ±1)`, `stop()`, `position()`, `speed()`, `set_speed(value)`, `home()`, `is_homed`, `can_home`, `requires_homing` |
| `Hub` | `ports`, `power_on(port)`, `power_off(port)`, `is_powered(port)`, `power_cycle(port)` |

Units: seconds for time, millimetres for linear axes, degrees for rotary axes, and
fractions from 0.0 to 1.0 for light levels. Canonical camera settings (`exposure_time` in
seconds, `gain` in dB, `iso`, `aperture` as an f-number, `pixel_format`) mean the same thing
on every camera. Other settings use the camera's own names. Gain and ISO are both
canonical but are not interchangeable: converting between them needs a camera's base ISO,
so each camera exposes whichever it has natively. Features only one camera has so far,
such as binning or a DSLR's image size, also use native names.

Numeric arguments can be Python numbers or numpy scalars (e.g. values from `np.linspace`).

### Focus drives

`focus_steps` counts steps from `last_limit`. A move that fails partway for any reason
other than reaching a limit (a timeout, a lost connection) leaves the lens somewhere
unknown, so both become `None` until the next `drive_to_limit()`.

### Hubs

`power_cycle()` turns the port back on even if the wait is interrupted (e.g. by Ctrl-C).

### Settings

Camera settings and a stage's `speed()` are `Setting`s. A `Setting` says what it accepts:

- `choices`: only these values (e.g. a DSLR's shutter speeds, as seconds).
- `minimum` and `maximum`: any value in range, when there are no choices.
- `step`: the hardware takes only `minimum + k * step` and rounds anything else itself.

A value is stored as the setting's own type, so an int written to a float setting such as
`exposure_time` reads back as a float.

A setting's `value` is `None` while the device is in a state the setting can't express as a
value, such as a DSLR at Bulb (`exposure_time`) or Auto ISO (`iso`). The setting is still
listed with what it accepts, and writing one of those values leaves that state. Code that
reads `.value` must handle `None`.

Code that searches for a value, like automatic exposure calibration, should use
`setting.nearest(value)` to get one the camera will keep exactly. It picks the closest
allowed value, breaking ties toward the lower one. `direction="down"` picks the largest
allowed value not above `value`, and `direction="up"` the smallest not below it. A value
outside the allowed range gives the closest allowed value at that end of the range.

A `Setting` is a snapshot. What one setting accepts can depend on others (maximum exposure
on frame rate, shutter speeds on the exposure mode), so read it again after changing
anything else.

### Light levels

`level(id)` and `color(id)` report the hardware's levels when the driver can read them.
Otherwise they report the last value set since `open()`, or `None` if nothing has been set.
`open()` doesn't change the lights, so reconnecting mid-session leaves them as they were.

### Stage speed, homing, and rotary axes

`speed()` is a `Setting` named `"speed"`, in mm/s if the stage has any linear axis and deg/s
if it is rotary only. Stages with fixed speeds (the Foldio360) list them as `choices`;
others (GRBL feed rates) give a range. `set_speed()` applies to later moves and jogs.


`can_home` says the stage has a hardware reference, such as limit switches, that `home()`
can find. `home()` raises `NotImplementedError` on stages without one. `requires_homing`
(which implies `can_home`) means the stage refuses to move until it has been homed on the
current connection.

`move_to()` is absolute. On a wrapping axis, 720° is the same position as 0°, so
`move_to({"theta": 720})` from 0 doesn't move, and repeating any `move_to()` does nothing.
Whole turns are relative moves: `move_by({"theta": 720})` turns twice. If you want
positions that count turns, use a rotary axis that doesn't wrap. Its position keeps
counting past 360, at the cost of losing the count on a reconnect or power cycle.

---

## 10. Developing without hardware

Every role has a simulated device in its subpackage: `SimulatedCamera`,
`SimulatedFileCamera`, and `SimulatedFocusDrive` in `cameras`, `SimulatedLight` in
`lights`, `SimulatedStage` in `stages`, and `SimulatedHub` in `hubs`. They behave like real
devices: moves take time and can be stopped, captures take the exposure time, and focus
drives hit limits. They pass the same contract tests (`tests/contracts/`) as the real
drivers, so code that works against a simulation should work against the hardware.

`el-devices simulate` runs each one through a few operations, which is a quick way to
check an install.

---

## 11. Writing a driver

Subclass the role ABCs your hardware offers. The public methods are already written: they
take the lock, check the device is open, and validate arguments against what you declare
(`channels`, `axes`, `ports`, `Setting` ranges). They then call protected hooks that you
implement (`_open`, `_close`, `_set_level`, `_move_to`, and so on). Device-specific rules
(a wavelength table, an alarm state) go in your hooks.

- If your driver records `stop()` requests (as a flag or event), clear them in
  `_motion_starting()`, which every move, homing, and jog calls first under the lock.
  Clearing them anywhere later can lose a stop that arrived while the move was starting.
- Import the vendor SDK lazily with `educelab.devices.errors.import_optional()`.
- Test protocol logic against a fake transport, and subclass the role's contract class
  from `tests/contracts/` with a `make_device` fixture.
- Record what you learn about the hardware in `docs/hardware/<model>.md`.

---

## Keeping this guide honest

Consumers depend on the behavior described here, so this guide is part of the package's
contract. Change a role or the `Device` base in a way a caller could notice (a new
method, a changed default, a different exception) and update this file in the same change.
