# Building a live preview

For applications that show a camera's preview: a viewfinder for framing, a zoomed view
for focusing, thumbnails of several cameras. It assumes the preview section of the
[guide](guide.md#preview), which says what the API does. This says how to build on it,
and comes from building `el-devices preview` and running it on the lab's cameras.

`el-devices preview` is the reference: about 300 lines in
[`app/preview.py`](../src/educelab/devices/app/preview.py), with every pattern below in
it. Run it against a camera before writing your own (`el-devices preview --help`).

---

## 1. Take frames on a worker thread

`preview_frame()` blocks until the camera has an image, and `start_preview()` blocks
while the camera changes: about 0.5 s on the Z7 II, up to 0.4 s on the others. Call
either from the UI thread and the UI stops for that long. With the first version of the
viewer, a drag lagged by seconds.

So one thread per camera owns every preview call, and the UI thread only reads what it
publishes:

```python
class Feed:
    """Previews one camera on its own thread; the UI reads `frame`."""

    def __init__(self, camera, max_size):
        self.camera, self.max_size = camera, max_size
        self.frame = None          # the newest PreviewFrame
        self.error = None          # set if the camera fails
        self._asked = []           # at most one region waiting
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def ask(self, region):
        """Preview `region` from the next frame on; only the latest counts."""
        with self._lock:
            self._asked = [region]

    def _run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    asked, self._asked = self._asked, []
                if asked:
                    self.camera.start_preview(asked[0], self.max_size)
                self.frame = self.camera.preview_frame()
        except DeviceError as exc:
            self.error = exc
```

- **Only the newest request matters.** A user who scrolls ten notches wants the last
  region, not ten camera changes. Keep one pending request and overwrite it.
- **Only the newest frame matters.** Publish the latest and let the UI take it when it
  redraws. Don't queue frames: a UI that falls behind should skip, not lag.
- **Hand errors to the UI thread** and stop the loop. Don't retry in the worker.
- **In Qt,** the worker is a `QThread` (or a plain thread) and the publish is a signal
  carrying the frame. Emit from the worker, connect queued, and drop a frame if the
  previous one hasn't been drawn. The devices themselves stay framework-free
  ([ADR 0001](adr/0001-framework-free-blocking-device-apis.md)).

Devices are thread-safe, so the UI thread may still call the camera, e.g. to change a
setting. It takes turns with the worker: a setting written during a `preview_frame()`
waits for that frame.

---

## 2. Let input settle before asking the camera

Changing the region costs something on every camera ([the table in the
guide](guide.md#preview)). Scrolling and dragging produce dozens of regions a second.

- **Wait for input to stop**, about 0.2 s, before asking for the new region. Never ask
  while the mouse button is down.
- **Show what is pending.** Draw the outline of the requested region on the current
  image until a frame of the new region arrives. Otherwise the half second on the
  Z7 II reads as a hang.
- **Moves are cheaper than size changes** on GenICam cameras: a region of the same size
  only moves the camera's offsets, without a restart. A pan that keeps the zoom level is
  nearly free; a zoom is not.

---

## 3. Ask for what the widget can show

`start_preview(region, max_size)` reduces at the camera where it can, so less data
crosses the link.

- **`max_size` is the widget's longest side in device pixels.** A 1000-pixel view of a
  20 MP sensor needs 1000 pixels across, not 5136.
- **Zooming in is asking for a smaller region with the same `max_size`.** Once the
  region has fewer sensor pixels than the widget, the camera sends every pixel and the
  preview is at full resolution: that is the view for judging focus.
- **Enlarge without smoothing** when the region is smaller than the widget, so one
  sensor pixel is a visible square.
- **The other way to zoom** is to preview the whole sensor with no `max_size` and crop
  on the host. Zoom and pan are then instant, but every frame is a full one: 8 frames/s
  and 20 MB each on the IDS. It suits a single camera and a short session.
- **Thumbnails** are `start_preview(max_size=200)` per camera. Five cameras on one USB 3
  link can't all send full frames at full rate; five thumbnails are a small load.

---

## 4. Trust the frame's geometry, not your request

A camera rounds the region to what it can deliver, and reports what it applied.

- **Map pointer positions through `frame.geometry.region`**, the region the image on
  screen shows, not the one you last asked for. They differ by rounding always, and by
  a whole zoom step while a change is pending.
- **Regions are fractions of what captures cover.** On the Z7 II that is the image area
  (FX, DX, 5:4, 1:1 or 16:9), not the sensor.
- **Show the scale.** `frame.geometry.scale` is preview pixels per capture pixel, so
  `f"{scale:.0%}"` is the familiar 25%, 100%, 200%. It tells the user whether the view
  is good enough to focus by: on the Z7 II it stays at 12% from the whole view down to
  a region a quarter of the width, then steps to 50%, 100% and 200%. It's `None` where
  a camera doesn't say how large its captures are. `geometry.capture_size` turns a
  region into capture pixels.
- **The applied region lies within the one asked for**, so it can be slightly smaller.
  The image size changes by a pixel or two between regions; don't resize the window to
  follow it.
- **Frames from before a change never arrive labelled as the new region.** The driver
  drops them, so `frame.geometry` is always the geometry that frame was taken with.

---

## 5. Display is yours

Preview images are not converted ([ADR 0007](adr/0007-preview-is-an-explicit-camera-state.md)):
they come at the camera's bit depth and pixel format.

- **Scale deeper formats to 8 bits for the screen only.** Keep the original for
  histograms and focus scores. Data is either left-aligned in 16 bits (the CM3's
  `Mono16`, 12 bits) or right-aligned at the format's depth (the IDS's `Mono10g40IDS`).
  Go by the format's name, and treat values at or above `1 << depth` as left-aligned.
  `eight_bit()` in the viewer is that rule.
- **gphoto2 bodies send `RGB8`** from a JPEG, already display-ready.
- **Bayer formats arrive raw.** Demosaic them yourself, or show them as mono.

---

## 6. Capturing from a preview

Captures raise `PreviewError` during preview. To take one:

```python
feed.pause()                      # the worker lets go of the camera
camera.stop_preview()
frame = camera.capture_frame()    # or capture_files()
camera.start_preview(region, max_size)
feed.resume()
```

- **A capture is the whole sensor**, whatever region was previewed. Show the captured
  image while the preview restarts.
- **Budget for the round trip:** nothing on a GenICam camera previewing the whole
  sensor, a restart each way (0.1–0.4 s) if a region or `max_size` was in use, and
  about 1.6 s on the Z7 II.
- **Pause the worker first.** Otherwise it calls `preview_frame()` on a camera that is
  no longer previewing. The `Feed` above leaves pausing out; `_Feed` in the viewer has
  it.

---

## 7. Focusing

- **Wait for the lens with `not_before`.** After a focus step,
  `preview_frame(not_before=datetime.now(UTC))` returns an image taken after the step.
  A focus score from an earlier image belongs to the previous position.
- **Not every camera can time its frames.** One that can't raises `DeviceError` for
  `not_before` (the IDS through IDS peak's GenTL producer). Check
  `camera.timestamp_source` on GenICam cameras, and fall back to discarding a couple of
  frames. gphoto2 bodies time by arrival, so `not_before` always works there.
- **Score at full resolution.** A reduced preview hides focus errors smaller than its
  reduction, so zoom until the region is under the widget's size (section 3).

---

## 8. Exposure and settings

- **Settings can change during preview** and apply to the next images.
- **Preview never changes the exposure.** What you see is what a capture gets, unless
  auto exposure is on, which leaves the exposure to the camera.
- **The CM3 previews at its whole-image frame rate** (about 36 frames/s), however small
  the region. A smaller image would run faster and cap the exposure, so the driver
  holds the rate; the gain is that any exposure a capture allows also works in preview.

---

## 9. When it goes wrong

- **`DeviceTimeout` and `StreamStalled`** from `preview_frame()` mean no image came.
  Stop the preview, tell the user, and offer to restart; `camera.diagnostics()` says
  more on GenICam cameras ([the guide](guide.md#a-stalled-stream)).
- **`close()` ends preview**, as does `stop_preview()`. Both put a camera's geometry
  back; a crash doesn't, but the next `open()` does.
- **Set a `capture_timeout` that fits a preview.** The default is loose, for captures;
  a viewfinder that hasn't had a frame in 3 s has a problem worth showing.

---

## 10. What each camera is like

| | CM3-U3-50S5M | IDS U3-36PxXCP | Nikon Z7 II |
|---|---|---|---|
| Whole view | 2448 × 2048 at 36 frames/s | 5136 × 3856 at 8 frames/s | A JPEG 1024 px wide |
| Full resolution | Any region | Any region | Zoomed only: regions up to 0.12 of the width in FX |
| Change the region | Move 4 ms, resize 0.1 s, then a frame or two | Move 6 ms, resize 0.3 s, then a frame or two (through IDS peak, a move is 0.09 s) | About 0.5 s |
| Deeper formats | `Mono16` (12 bits) | `Mono10g40IDS` | No |
| Times its frames | Yes | Through Aravis; not through IDS peak | By arrival |

- **Z7 II:** the camera has three zoom levels and the driver picks the tightest that
  holds the region, then crops, so any region works and always shows all of what was
  asked for. Detail comes in steps: the whole view and any region wider than a quarter
  of it are cut from a 1024-pixel image, so a region a third of the width is about 340
  pixels across, and one just under a quarter is 1024. Ask for 1024 pixels
  or more: a `max_size` just under a frame's width costs half the pixels. Its screens go blank while live
  view runs and stay blank until `close()`. On macOS, `ptpcamerad` must be killed
  before opening ([gphoto2 cameras](hardware/gphoto2-cameras.md)).
- **GenICam cameras through Aravis:** the smallest regions run at 60 frames/s on the
  IDS. Drawing can't keep up with that; draw at the display's rate and let frames go.
- **Details and measurements** are in [USB3 Vision cameras](hardware/usb3-vision.md#preview)
  and [gphoto2 cameras](hardware/gphoto2-cameras.md).
