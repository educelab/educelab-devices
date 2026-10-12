# EduceLab Devices

The basis for every hardware-interfacing project at EduceLab: Python APIs for the lab's imaging hardware. Device APIs are consumed and wrapped by applications (e.g. `pgs-scan`) but never depend on them.

## Language

**Device**:
A physical unit that owns a connection to the host and the open/close lifecycle of that connection. A device offers one or more roles (e.g. a turntable that also has a built-in light).
_Avoid_: Controller, driver, component

**Consumer**:
An application or script that uses devices from this package, possibly wrapping them for its own framework.
_Avoid_: Client, app

**Simulated device**:
A device implementation with no hardware behind it that behaves like the real one for testing and development.
_Avoid_: Mock, fake, virtual device

### Roles

**Camera**:
A role that acquires images from a single sensor. A camera captures either frames or camera files, depending on what the hardware natively produces.

**Frame**:
A decoded pixel array captured by a camera, with its capture metadata.
_Avoid_: Image (ambiguous with camera file)

**Setting**:
A named, adjustable camera parameter with a type, a range or set of choices, and a current value. Canonical settings (e.g. `exposure_time`) mean the same thing on every camera; all others use the camera's native name.

**Focus drive**:
A role offered by some cameras that moves the lens focus in signed, relative steps toward near or far. A focus drive can detect its physical limits but cannot report an absolute position.

**Camera file**:
An encoded file exactly as a camera produced it (e.g. a NEF raw or a JPEG), held in memory until a consumer saves it. One capture may produce several camera files.
_Avoid_: Raw, capture file

**Rig**:
A consumer-defined group of devices used together (e.g. four cameras capturing simultaneously). Rigs belong to consumers, not to this package.

**Light**:
A role that emits illumination through one or more channels.
_Avoid_: Light controller, LED board

**Channel**:
One independently addressable emitter of a light, identified by a string ID and described by its kind (monochromatic or RGB) and, optionally, its wavelength. A channel's level is a fraction from 0 (off) to 1 (full).

**Stage**:
A role that positions something along one or more axes.
_Avoid_: Motor, motion controller, gantry (except for the specific X-Carve device)

**Axis**:
One degree of freedom of a stage, either linear (millimetres) or rotary (degrees), with limits and, for rotary axes, optional wrapping. Positions are always in machine coordinates.

**Homing**:
Establishing a stage's reference position from the hardware (e.g. driving to limit switches). Homing is always an explicit request and never a side effect of connecting. Stages without a hardware reference (e.g. the Foldio360) do not home.
_Avoid_: Origin (a consumer-side offset, not part of a stage)

**Hub**:
A role that switches power to a set of USB ports, typically to reset hung devices.
