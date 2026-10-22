# GenICam cameras share a core, with one backend per transport library

Every GenICam frame camera, whatever the library that reaches it, is driven by one shared core (`educelab.devices.cameras._genicam`). The core holds the SFNC behaviour taken out of the Spinnaker driver: canonical settings and their units, snapping and clamping, auto modes, pausing acquisition for writes the camera only takes while stopped, software-trigger priming and re-firing, the device-to-host timestamp offset, the `not_before` loop, and what to do with incomplete frames. It talks to the camera only through a small backend seam: a node map (typed reads and writes, ranges, readability and writability), a stream (start, stop, grab with a timeout, release), and discovery and open by serial number. Each backend translates its library's errors into `DeviceError` subclasses at the seam.

There is one public driver class per backend (`SpinnakerCamera`, `AravisCamera`, `GenTLCamera`), each in its own module with its library imported lazily. The GenTL backend uses EMVA's `genicam` package directly, not Harvesters. Optional features (software trigger, throughput limit, timestamp latch, auto modes, resulting frame rate) are probed when the camera opens, and settings for features a camera lacks are left out.

The Spinnaker driver keeps its public API and its tests unchanged. It becomes the PySpin backend plus the shared core.

## Considered Options

- **A separate driver per library, copying the Spinnaker logic**: the quickest way to a second camera, but the SFNC logic would drift between copies, which is the problem this package exists to fix.
- **One `GenICamCamera(serial, backend=...)` class**: hides which library is in use, but each library needs different constructor arguments (PySpin none, GenTL a producer path), and a single class would make every caller's type check depend on runtime configuration. One class per backend matches the rest of the package.
- **Only GenTL, on every OS**: one library, but no USB3 Vision GenTL producer exists for macOS, and on Linux it needs a vendor SDK where Aravis needs none.
- **Harvesters for GenTL**: a friendlier API over `genicam`, but it hasn't been released since May 2024, has open crash and leak reports, and brings background threads and a fetch model that would have to be undone to fit ADR 0001.
- **Only Aravis, on every OS**: on Windows the camera would have to be rebound to WinUSB, displacing the vendor's driver.
