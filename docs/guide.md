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
   One device can offer several roles. A driver either implements them in one class or
   hands out a second object for a role that shares the device's connection and lock:
   the Foldio360 is a `Stage`, and its built-in light is the `Light` at
   `Foldio360.halo`; a gphoto2 camera's lens focus is the `FocusDrive` at
   `camera.focus`. Such a role is opened and closed on its own. Opening it opens the
   device if needed, and closing it closes the device only if it was the one that opened
   it. Closing the device closes the role too.
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
- **`info` identifies a device**: `backend`, `model`, and, where known, `address`,
  `serial_number`, `firmware_version` and `vendor`. Every camera reports `vendor` once
  open, if the hardware can say; most drivers fill in more once open. A camera's hardware
  doc says where its strings come from.

---

## 3. Blocking and timeouts

Long operations take a `timeout` in seconds. `None` means the device's default: for
stages, `Stage.move_timeout`; for frame cameras, `FrameCamera.capture_timeout`. Stages
that can predict a move's duration (the Foldio360 and the X-Carve) leave `move_timeout`
at `None` and derive each limit from the prediction; set `move_timeout` to use a fixed
limit instead. Pass `math.inf` to wait indefinitely. A device that
doesn't finish in time raises `DeviceTimeout`, which is also a `TimeoutError`.

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
| `StreamStalled` | A free-running camera stopped sending frames (also a `DeviceTimeout`); `.counters` holds what it counted. See [A stalled stream](#a-stalled-stream) |
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
| Guess at state the hardware can't report | A wrong guess (e.g. that a turntable was power-cycled because it reads 0) is worse than a documented habit. The hardware doc says what can't be detected |
| Retry commands | Whether a retry is safe depends on the device and on what the consumer is doing |
| Sequence a capture or manage a rig | Rigs and workflows belong to consumers |
| Offer framework wrappers | Qt or asyncio wrappers live in consumers until two of them need the same one (ADR 0001) |

---

## 9. Roles at a glance

| Role | Methods |
|---|---|
| `Camera` | `settings()`, `setting(name)`, `set_setting(name, value)`, `start_preview(region=None, max_size=None)`, `stop_preview()`, `preview_frame(not_before=None) -> PreviewFrame` |
| `FrameCamera` | adds `capture_frame(not_before=None, timeout=None) -> Frame`; `not_before` (an aware `datetime`) rejects frames with an earlier `timestamp`, for free-running cameras. Which moment a camera timestamps varies; its hardware doc says |
| `FileCamera` | adds `capture_files() -> list[CameraFile]`; save with `CameraFile.save(dir, stem)`, which refuses to overwrite unless asked |
| `FocusDrive` | `step(n)` (positive = toward far), `drive_to_limit("near" \| "far")`, `focus_steps`, `last_limit` |
| `Light` | `channels`, `set_level(id, 0.0–1.0)`, `set_color(id, (r, g, b))`, `level(id)`, `color(id)`, `all_off()` |
| `Stage` | `axes`, `move_to({axis: pos})`, `move_by({axis: offset})`, `jog(axis, ±1)`, `stop()`, `position()`, `speed()`, `set_speed(value)`, `home()`, `is_homed`, `can_home`, `requires_homing` |
| `Hub` | `ports`, `power_on(port)`, `power_off(port)`, `is_powered(port)`, `power_cycle(port)` |

Units: seconds for time, millimetres for linear axes, degrees for rotary axes, and
fractions from 0.0 to 1.0 for light levels. Canonical camera settings (`exposure_time` in
seconds, `gain` in dB, `iso`, `aperture` as an f-number, `pixel_format`) mean the same thing
on every camera. Frame cameras also report their size in pixels through read-only canonical
settings: `width` and `height` for the frames a capture will return, so they're known before
the first capture, and `sensor_width` and `sensor_height` for the whole sensor, which can be
larger. Other settings use the camera's own names. Gain and ISO are both canonical but are
not interchangeable: converting between them needs a camera's base ISO, so each camera
exposes whichever it has natively. Features only one camera has so far, such as binning or
a DSLR's image size, also use native names.
A driver may instead give a device-specific feature a descriptive name and unit, if its
hardware doc maps the name to the native node and gives the conversion.

Numeric arguments can be Python numbers or numpy scalars (e.g. values from `np.linspace`).

Frames with more than 8 bits per value come as `uint16`, with the value in the low bits:
a 10-bit pixel runs from 0 to 1023. Cameras that know the depth say so in
`frame.metadata["bit_depth"]`.

On Windows before Python 3.13, `datetime.now()` and `time.time()` tick every 15.6 ms,
so a `not_before` taken from them can be up to 15.6 ms early, letting through a frame
exposed that much before the request. Python 3.13 and later don't have this problem.

### Preview

Preview is a state every camera has, for framing a shot and judging focus
([ADR 0007](adr/0007-preview-is-an-explicit-camera-state.md)). `start_preview()` enters
it, `stop_preview()` and `close()` leave it, and `camera.preview` is the current
`PreviewGeometry`, or `None`.

```python
camera.start_preview()  # the whole sensor, every pixel
frame = camera.preview_frame()  # blocks for the next image
show(frame.pixels, frame.pixel_format)  # scale for display yourself
camera.stop_preview()
```

- **Images are not converted.** `PreviewFrame.pixels` is at the camera's resolution
  and in its current pixel format, so a Mono16 camera previews in 16 bits. Scaling for
  a screen, zoom, pan, histograms and focus scores are the caller's.
- **Zoom and pan on the host when you can.** A full-resolution preview of the whole
  sensor never touches the camera's geometry, so cropping it yourself is instant.
- **Ask for less to move less data.** `start_preview(region, max_size)` takes a
  `PreviewRegion` (or an `(x, y, width, height)` tuple) in fractions of the sensor,
  and a cap on the longest side in pixels. Use them for thumbnails of several cameras
  on one link, or for full-resolution pixels from a camera that only gives them
  zoomed (the Z7 II).
- **Each frame says what it shows.** `frame.geometry` is the region and size of that
  image, and `frame.timestamp` when it was taken. The region a camera applies lies
  within the one asked for, rounded to what it can deliver.
- **Changing a preview** is another `start_preview()` call. What it costs depends on
  the camera:

  | Change | GenICam cameras | Z7 II |
  |---|---|---|
  | Start | Immediate for the whole sensor | About 1 s |
  | Move a region of the same size | Immediate (a restart of 0.1–0.4 s on the IDS through IDS peak's GenTL producer) | About 0.5 s |
  | Change the region's size or `max_size` | Restart, 0.1–0.4 s | About 0.5 s, up to 0.8 s |
  | Stop | Restart if a region or `max_size` was used | 0.6 s |

- **Preview leaves the exposure alone.** A camera whose frame rate caps the exposure
  (the CM3) would speed up for a smaller image and shorten a long exposure, so the
  driver holds its frame rate at the whole image's for the length of the preview.
  With auto exposure on, the exposure is the camera's.
- **To build a preview into an application,** see [Building a live
  preview](preview.md): threading, when to ask the camera for a new region, display,
  capturing, and what each camera is like.
- **To look at a preview,** `el-devices preview <backend>` shows one in a window,
  with scroll to zoom, drag to move and `c` to capture. It's a development tool
  that needs OpenCV (`pip install "educelab-devices[viewer]"`); the subcommand
  isn't listed without it.
- **Captures refuse during preview**, with `PreviewError`. To capture, stop, capture
  and start again, and show the captured image meanwhile:

  ```python
  camera.stop_preview()
  frame = camera.capture_frame()
  camera.start_preview(region, max_size)
  ```

- **Settings can change during preview** and apply to the next images. `width` and
  `height` keep describing captures.
- **`preview_frame(not_before=...)`** skips images from before a moment, such as the
  end of a focus step. A camera that can't time its frames raises `DeviceError`
  instead, as it does for `capture_frame()`.
- **On a gphoto2 body** the camera's own screens go blank while live view runs, and on
  the Z7 II they stay blank until `close()`, not `stop_preview()`.

### GenICam cameras

`SpinnakerCamera`, `AravisCamera` and `GenTLCamera` are all `GenICamCamera`s, which
`educelab.devices.cameras` exports for type hints and `isinstance` checks. Besides the
canonical settings they offer `TRIGGER` (`"Off"` or `"Software"`, where each capture
fires a software trigger), `THROUGHPUT_LIMIT` in MB/s (`None` while the camera's limit is
off), the camera's auto modes by their native names (`ExposureAuto`, `GainAuto`), and
the read-only `current_throughput`, `resulting_frame_rate` and `link_speed`, each only
where the camera has the feature. `link_speed` is in MB/s: the camera's
`DeviceLinkSpeed`, or the nominal rate of its USB speed (625 for USB 3, 60 for USB 2)
on cameras that only name it.

`diagnostics()` returns counters for diagnosing a stream that stops sending, where the
camera and backend have them: the camera's `transmit_failures` and `link_recoveries`,
and, through GenTL, the stream's `frames_started`, `frames_delivered`, `underruns`,
`buffers_announced`, `buffers_queued` and `buffers_awaiting_delivery`. Keys differ
between cameras and backends, and a counter that can't be read is left out.

Frame timestamps are measured against `host_time()`, a fine-grained `time.time()`; use
it to time a capture against its frame.

A USB camera linked slower than USB 3 logs a warning at open, where it reports its USB
speed (`U3VCurrentSpeed`). Replug it; a USB 2 link can't carry what the camera sends.

#### A stalled stream

A free-running camera can stop sending frames while it's still acquiring. On the lab's
Windows rig, one of five CM3s on a shared hub did once, and stayed silent until it was
closed (`docs/hardware/usb3-vision.md`, Hazards). The cause wasn't found, and it couldn't
be made to happen again.

The capture then waits out its timeout and raises `StreamStalled`, a `DeviceTimeout`
whose message and `.counters` hold the camera's `diagnostics()` at that moment. A capture
that received any frame, even a stale or incomplete one, raises a plain `DeviceTimeout`
instead, and so does a software-triggered capture, where a lost trigger looks the same,
and one whose timeout is no longer than the exposure time.
The driver doesn't retry (see section 8): whether a scan can wait for a recovery is the
app's call. Log the counters, then try these in order, each followed by a capture:

1. `camera.restart_acquisition()` stops and starts the stream, logging the counters before
   and after. Settings are kept. Frame IDs, and GenTL's stream counters, start again
   from 0.
2. `camera.close()` and `camera.open()`. Opening sets `trigger` back to `"Off"`, so
   apply your settings again.
3. Power-cycle the camera's hub port (`Hub.power_cycle(port)`), wait for it to come back
   (`open()` raises `DeviceNotFound` until it does), then open it and apply your
   settings: the camera starts from its defaults.
4. Give up and fail the scan, with the counters from each step.

```python
import logging

from educelab.devices.errors import StreamStalled

log = logging.getLogger(__name__)


def capture(camera, apply_settings):
    try:
        return camera.capture_frame()
    except StreamStalled as exc:
        log.warning("%s; restarting acquisition", exc)
    camera.restart_acquisition()
    try:
        return camera.capture_frame()
    except StreamStalled as exc:
        log.warning("%s; reopening the camera", exc)
    camera.close()
    camera.open()
    apply_settings(camera)
    return camera.capture_frame()  # a third StreamStalled goes to the caller
```

On the rig, step 1 cleared a stall forced by stopping a camera's acquisition behind the
driver's back, through GenTL and Spinnaker. Whether it clears a stall like the one that
happened by itself is unknown. When one happens, record which step cleared it in
`usb3-vision.md`.

A frame's `metadata` holds:

| Key | Meaning |
|---|---|
| `exposure_time`, `gain` | The settings when the frame was taken, in seconds and dB; `None` if the camera has no such node |
| `bit_depth` | Bits per pixel value |
| `device_timestamp_ns` | The camera's own timestamp counter, which marks the end of exposure; `frame.timestamp` is the start, in host time |
| `frame_id` | The camera's frame counter |
| `timestamp_source` | How `frame.timestamp` was found: `"latch"` (the camera's timestamp latch), `"arrival"` (when frames reach the host, about a millisecond less exact), `"trigger"` (the software trigger that exposed it), or `None` |
| `stale_frames` | Frames skipped for being older than `not_before` |
| `incomplete_frames` | Frames skipped for arriving incomplete |
| `retries` | `stale_frames + incomplete_frames` |
| `retriggers` | Software triggers fired again because no frame came |
| `missing_trailer` | The frame arrived without its trailer; it's used as complete, but flagged |
| `timestamp_remeasured` | Too many stale frames made the capture measure the timestamp offset again |

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
With `strict=True`, a value more than half a step past either end raises `ValueError`
instead, for rounding a value someone typed without quietly replacing one far off.

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

Positions are only as fine as the hardware counts them. A stage that counts whole degrees
(the Foldio360) rounds targets to the nearest degree and accepts a move that lands within
±1° of it, so compare its positions with that tolerance, not exactly. The stage's hardware
doc gives its resolution.


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
`SimulatedFileCamera(bulb=True, auto_iso=True)` starts with `exposure_time` and `iso`
reading as `None`, like a DSLR at Bulb and Auto ISO. `SimulatedCamera(color=True)` acts
like a colour camera: it also offers `RGB8` and starts in it, as GenICam colour cameras do.
`SimulatedCamera(auto=True)` offers `ExposureAuto` and `GainAuto` (`Off` or `Continuous`)
under their GenICam names, starting at `Continuous`. While one is on, `exposure_time` or
`gain` is read-only and changes a little from frame to frame, as the frame metadata shows.
As on real cameras, exposure is adjusted first, so with both on, gain stays at its minimum;
turn `ExposureAuto` off to see gain move. `SimulatedCamera(sensor_size=(w, h))` reports a
sensor larger than its frames, like a camera that crops (the IDS U3-36P0XCP does).

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
