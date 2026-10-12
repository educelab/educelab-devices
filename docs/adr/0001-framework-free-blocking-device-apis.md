# Device APIs are blocking and framework-free

Devices expose plain synchronous, blocking methods and never depend on Qt, asyncio, or any other application framework. Consumers own threading and event delivery (e.g. `pgs-scan` wraps devices in `QObject`s on worker threads). Where a device produces events on its own (progress, disconnects), it reports them through plain-Python callbacks. A transport may use threads or asyncio internally (e.g. a private event loop for BLE), but that never leaks into the public API.

## Considered Options

- **asyncio public API**: natural for BLE but awkward to bridge into Qt and burdensome for simple scripts.
- **Qt signals in the device classes** (as in `pgs-scan`): forces Qt on every consumer and mixes threading policy into drivers. Qt wrappers may later move into a separate distribution once more than one consumer needs the same wrapper.
