# Preview is an explicit camera state

Every camera has an explicit preview state. `start_preview(region=..., max_size=...)` enters it, or reconfigures it when the camera is already previewing, and `stop_preview()` and `close()` leave it. While previewing, `preview_frame(not_before=None)` pulls one blocking frame. Outside preview, `preview_frame()` raises, and so do `capture_frame()` and `capture_files()` during preview. A consumer that wants a capture during preview takes a snapshot: stop, capture, start. Making the state explicit keeps the costs visible. A GenICam camera has to restart acquisition to change ROI or binning, and a gphoto2 body has to switch live view on and off, so preview can't be a stateless call that changes the camera behind the caller's back.

The **preview region** is a box in fractions of the sensor, and `max_size` caps the longest side of the output. Each driver chooses its own mechanism and reports what it applied as a `PreviewGeometry`, which `camera.preview` also returns (`None` when not previewing). GenICam uses hardware ROI and binning or decimation where they work, and crops and strides on the host where they don't. gphoto2 uses `liveviewzoomarea` and `liveviewsize`, and reports them read-only because preview owns them. Fractions are used because gphoto2 can't express a live-view crop in sensor pixels, and a box in pixels would only fit GenICam. Preview shares every other setting with capture, including the trigger mode and continuous AF, and settings can change during preview. The canonical `width` and `height` keep describing captures.

## Considered Options

- **A stateless `preview_frame()` that switches geometry on demand**: the previous API. Alternating previews and captures would each pay a hidden restart, and gphoto2's live view was already latent state, started by the first preview and ended only by `close()`.
- **Capture during preview leaves and resumes preview automatically**: convenient, but it costs two restarts per capture on GenICam, and those restarts would be invisible to the caller.
- **The caller choosing binning, decimation or stride through native settings**: more control, but every consumer would need per-model knowledge to get a cheap preview, and the result couldn't be compared across cameras.
- **Frames pushed from a driver thread**: rejected by ADR 0001. Consumers already poll on their own timers.
- **A no-repeat rule instead of `not_before`**: repeats only add lag on a display. What matters for focus scoring is a frame taken after the lens moved, which `not_before` states directly, as it does for `capture_frame()`.

## Consequences

- This breaks the previous `preview_frame()`, which worked at any time.
- `docs/guide.md` and the camera contract suite cover the preview state for every driver and both simulated cameras.
