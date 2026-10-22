# USB3 Vision cameras (Aravis and GenTL)

The generic GenICam frame-camera drivers: `AravisCamera` (`educelab.devices.cameras.aravis`) and `GenTLCamera` (`educelab.devices.cameras.gentl`). Both share their behaviour with `SpinnakerCamera` through `GenICamCamera` (ADR 0006), so a camera behaves the same whichever library reaches it. This doc covers the libraries, what the drivers add on top of them, and the backend runs. Each camera model has its own doc:

- IDS U3-36P0XCP-M-GL: [ids-u3-36p0xcp.md](ids-u3-36p0xcp.md)
- FLIR Chameleon3 CM3-U3-50S5M: [spinnaker.md](spinnaker.md), with its runs through these drivers [below](#hardware-runs)

Tags: **[V]** verified against a primary source (source file, official doc, formula, PyPI metadata) or tested on a lab Mac (macOS 27, arm64, Homebrew); **[I]** inferred or taken from secondary sources, which makes it a hazard until checked on hardware (see [README](README.md#how-sure-we-are)).

## The drivers

- **Addressing.** Open a camera by the serial number its backend's `discover()` lists. Aravis lists a FLIR camera by its USB serial (`0135BAD4`), while GenTL and Spinnaker list FLIR's decimal serial (`20298452`). After open, `info.serial_number` is always the camera's own `DeviceSerialNumber`, so code that maps saved settings to cameras sees the same serial from every backend. IDS cameras report the same serial everywhere. [V]
- **Installing.** Aravis and PyGObject are installed by hand, like PySpin: sections [1](#1-aravis--pygobject-on-macos-homebrew-apple-silicon) and [2](#2-aravis--pygobject-on-linux-ubuntu-2404-noble). GenTL needs the `[gentl]` extra (`pip install 'educelab-devices[gentl]'`, which installs `genicam`) and a vendor producer whose folder is in `GENICAM_GENTL64_PATH`: section [4](#4-ids-peak-gentl-producer) for IDS. Both drivers import their library only when used, and say where these steps are when it's missing. [V]
- **What frames carry.** Every frame's metadata includes `bit_depth` (significant bits per value: 8, 10 or 16) and `timestamp_source`: `"latch"` from the camera's timestamp latch, `"arrival"` from when frames reach the host (for cameras without a latch), or `"trigger"` for a triggered frame dated by its trigger (see [ids-u3-36p0xcp.md](ids-u3-36p0xcp.md#timestamps)). [V]

## Backends

| OS | Aravis 0.8 (PyGObject) | GenTL (`genicam` + a vendor `.cti`) |
|---|---|---|
| macOS arm64 | Yes: Homebrew `aravis` + pip PyGObject. Verified with IDS and FLIR cameras. | FLIR cameras only, through Spinnaker's producer (verified). IDS has no macOS producer. |
| Linux x86_64/arm64 | Yes: apt `gir1.2-aravis-0.8` + udev rule + usbfs memory. Not yet run on hardware. | Yes: IDS peak's `ids_u3vgentl.cti`. Not yet run on hardware. |
| Windows | Only through MSYS2 (not pip-friendly), and it conflicts with IDS's driver. | Yes: IDS peak's `ids_u3vgentlk.cti`. Not yet run on hardware. |

- Pick one backend per camera per machine, especially on Windows, where a USB device's driver binding is exclusive. [I]
- A vendor's producer opens only that vendor's cameras (IDS's says so; FLIR's lists only FLIR cameras). Aravis opens any USB3 Vision camera. [V]
- GenTL buffers carry no host arrival time, so a camera without a timestamp latch can't be timed through GenTL, and the driver refuses `not_before` on it. Aravis times such cameras by frame arrival. [V from the driver]

## How the drivers differ from Aravis and GenTL

Behaviour the drivers add or change, and why. Each point was verified on the hardware named, in the [Hardware runs](#hardware-runs) below or the camera's own doc.

**Both drivers** (through `GenICamCamera`):

- Captures are newest-only and honour `not_before`, as with `SpinnakerCamera`; stale and incomplete frames are dropped and counted in the metadata.
- Without a timestamp latch, the device-to-host offset comes from frame arrivals (Aravis only), and frames a camera stamps before their trigger are dated by the trigger (IDS).
- A software trigger waits until 10 ms after the last frame, because the IDS camera ignores triggers for about 3 ms after one.
- Host time comes from `host_time()`: `time.time()` where that is fine-grained, and otherwise `time.perf_counter()` lined up with the wall clock at one of its ticks and again every minute. Before Python 3.13, `time.time()` on Windows ticks every 15.6 ms, which threw the latched timestamp offset off by up to 8 ms. See the Windows run below. Short intervals (trigger turnaround, the settle wait) use `time.perf_counter()`, since `time.monotonic()` ticks every 15.6 ms there too.
- `ExposureTimeAbs` stands in for `ExposureTime`, and `TimestampLatchValue` for `Timestamp`, on cameras with the older names. `AcquisitionMode`, `ExposureAuto`, `GainAuto` and the throughput nodes are optional.
- Mono10g40IDS is unpacked to `uint16`; packed formats the core can't unpack (e.g. Mono12Packed) come back as raw bytes.

**Aravis** (`AravisCamera`):

- **USB transfer mode by platform.** Sync (`UvUsbMode.SYNC`) on macOS, where Aravis's async mode can hang closing a camera. Async, Aravis's high-throughput mode, everywhere else: on Linux, sync mode delivered only incomplete frames from the IDS camera. See [Hazards](#hazards).
- **A stream only while acquiring.** Creating a stream enables the camera's stream channel, and the IDS camera then refuses `PixelFormat` and `TriggerMode` writes. The old stream is dropped before a new one is made, because finalizing a stream disables the channel even after a newer stream enabled it.
- **Newest only, by callback.** Aravis has no newest-only mode: once its buffers fill, it drops the newer frames. A stream callback recycles every finished frame but the newest. Four buffers (`BUFFER_COUNT`).
- **Access checks on, range checks off.** Aravis's access mode ignores locks (e.g. `ExposureTime` under auto exposure), so the driver also asks `is_locked()`. Its range check refused Mono16 on the CM3-U3-50S5M, whose XML gives `PixelFormat`'s register a stale maximum; the core checks ranges itself.
- **`TLParamsLocked` is left alone**, as Aravis's own tools do: setting it wedges the IDS camera until a power cycle.
- **Opening.** The driver opens from the last device listing, and updates the list (about 1 s) only when the serial isn't in it. It refuses to open a camera this process already has open, without asking Aravis: a failed second `Camera.new` releases USB interfaces it never claimed. That was once suspected of wedging the IDS camera (the async-mode hang turned out to be the cause), and the refusal stays as a precaution.
- **One lock** around all Aravis calls, since Aravis isn't thread safe (section [3](#3-aravis-python-api-essentials-08x-through-gi), Thread safety).
- Buffers reach the stream only through their connection, so a buffer kept in a traceback can't keep the camera's USB interface after close.

**GenTL** (`GenTLCamera`):

- **Producers** come from `GENICAM_GENTL64_PATH` (or one named with `producer=`, which `discover()` takes too). Each producer is loaded once per process and unloaded when its last camera closes. A producer that fails to load, list its cameras or unload is skipped with a warning naming it; `ImportError` comes only when none works. What a producer prints to stderr while loading (e.g. "GenTL producer does not implement DSGetNumFlows") goes to the debug log.
- **Opening twice** in one process is refused with `DeviceBusy`; FLIR's producer would otherwise allow it.
- **Feature access** goes through `genicam.genapi`, with the camera's XML read from `local:` or `file:` URLs, plain or zipped.
- **Buffers** are `bytes` objects, which the producer fills in place: FLIR's producer refuses a `bytearray`. The data stream is set to `NewestOnly` when it has `StreamBufferHandlingMode`; otherwise older frames are drained.
- **`TLParamsLocked`** is set during acquisition and cleared after, as GenTL consumers do. Unknown on the IDS camera through IDS's producer.
- **Waits** are cut into 100 ms slices (`_WAIT_SLICE_MS`), in case `genicam` holds the GIL while waiting, as reported for Harvesters (#508).

## Timing

- `Aravis.update_device_list()` takes about 1.0 s every call, on GigE Vision discovery, even with no network cameras. `AravisCamera.discover()` always pays it; opening usually doesn't. [V]
- GenTL enumeration waits up to 500 ms per interface and device list (`_LIST_TIMEOUT_MS`). The figure is a loose guess. [I]
- Capture rates through each backend, for the CM3-U3-50S5M, are in the [Hardware runs](#hardware-runs); for the IDS camera, in its doc.

## Hazards

- **Aravis async USB mode on macOS** can hang forever closing a camera, in `arv_uv_stream_finalize`: the stream thread waits for cancelled transfers to complete, while it is the thread that would complete them. When a close doesn't hang, the next session may get only `missing-packets` frames until the camera is power-cycled. The driver uses sync mode on macOS; don't switch it. [V on the IDS camera]
- **Aravis sync USB mode on Linux** can't keep up with a full-resolution USB3 Vision stream. With the IDS camera on Fedora (arm64), `arv-camera-test-0.8` in sync mode reported six or more errors, and the driver in sync mode got only incomplete frames. In async mode the same camera streamed cleanly. The driver uses async mode off macOS. Sync mode keeps a single 1 MiB transfer in flight (`arvuvstream.c`), which is the likely reason. [V for the symptoms, 2026-10-22; I for the reason]
- **`TLParamsLocked` through Aravis.** Setting it wedged the IDS camera until a power cycle. [V]
- **Several cameras on one USB link.** Uncapped, five CM3-U3-50S5M cameras and the IDS camera on one Acroname hub need more than its USB 3 upstream link carries, and some cameras then get only `size-mismatch` frames. Cap each camera's `throughput_limit`. [V]
- **Windows driver binding.** IDS peak's kernel driver claims IDS cameras, so nothing libusb-based (Aravis) can open them while it's installed. [I]
- **Linux usbfs memory.** The kernel's 16 MB default is less than one 20 MP frame. Raise it (section [2](#usbfs-memory)). [I]

## Manual check

`el-devices genicam --backend aravis|gentl` lists cameras through the chosen backend, then captures from them all at once and reports latency, exposure start, dropped frames, bit depth, how frames were timed, and USB bandwidth. It takes the same options as `el-devices spinnaker` (`--serial`, `--frames`, `--exposure`, `--gain`, `--pixel-format`, `--throughput-limit`, `--trigger software`, `--save-missing-trailer`). With `--frames 0` it only lists. With `--backend gentl`, `--producer PATH` uses only that `.cti` file.

The hardware tests share one suite (`tests/contracts/genicam_hardware.py`):

```bash
ARAVIS_SERIAL=<serial> pytest -m hardware tests/test_aravis_hardware.py -s
GENTL_SERIAL=<serial> pytest -m hardware tests/test_gentl_hardware.py -s
```

Without the variable they use the first camera listed. The multi-camera tests use every attached camera of the same model.

## Checked on a lab Mac, without a camera

Aravis 0.8.36 (Homebrew), PyGObject 3.58.1 and pycairo 1.29.2 built into the project venv (Python 3.15, macOS 27, arm64), 2026-10-22. [V] for everything in this section, on that machine.

### Install

- With Homebrew's `aravis`, `pkgconf`, `cairo`, `glib` and `gobject-introspection` present, `pip install pycairo` and then `pip install PyGObject` built and installed with no environment variables. `gi.require_version("Aravis", "0.8")` found the typelib in `/opt/homebrew/lib/girepository-1.0`.
- `tests/test_fake_aravis.py` passes against the real module: the fake's method names, the PFNC pixel-format codes in `cameras/aravis.py`, and the buffer-status and error enums all match.

### Measured timing

- `Aravis.update_device_list()` takes about **1.0 s** every call (measured twice). Opening a camera by ID (`Aravis.Camera.new`) takes about 3 ms. The time goes on GigE Vision discovery, which runs even with no network cameras. The driver therefore opens cameras from the last listing and updates the list only when the serial isn't in it, or when opening a stale entry fails. `discover()` always updates, so it always costs about a second.

### Aravis's simulated camera

Aravis ships a simulated camera on its "Fake" interface (`Aravis.enable_interface("Fake")`, ID `Fake_1`, serial `1`). `tests/test_aravis_library.py` drives `AravisCamera` through the real library with it. What it showed:

- It names exposure **`ExposureTimeAbs`**, the pre-SFNC-2.0 name, and has no `ExposureTime`. The core now falls back to `ExposureTimeAbs`, as it does for `TimestampLatchValue`. Some older real cameras use that name too.
- It has no `TimestampLatch`, `Gain`, `ExposureAuto`, `DeviceFirmwareVersion` or throughput nodes. The driver leaves those settings out. Without the latch, its timestamp offset now comes from frame arrivals, and `not_before` works (`test_without_a_timestamp_latch_frame_arrivals_set_the_offset`).
- `get_feature_access_mode()` returns an `Aravis.GcAccessMode`, and `get_data()` returns `bytes`.
- Every pixel format it offers captured with the right shape and dtype (Mono8/16, RGB8, four Bayer 8-bit). The driver allocated new buffers for the larger RGB8 and Mono16 payloads.
- The software trigger primed in about 38 ms.
- A second `Camera.new` on the same simulated camera succeeds, so busy detection (`DeviceBusy`) can only be checked on a real USB camera (`test_a_second_open_of_the_same_camera_is_busy`).

### genicam's genapi, without a producer

genicam 1.6.0 in a Python 3.12 venv on the same Mac, 2026-10-22, using a hand-written camera XML served through a stand-in port (`tests/test_gentl_library.py`). [V] for each point below:

- `NodeMap.get_node(name)` returns a typed node (`IFloat`, `IInteger`, `IEnumeration`, `ICommand`, `IString`). A missing node raises `genapi.LogicalErrorException` ("Node not existing"), and `has_node()` doesn't raise.
- `get_access_mode()` returns `genapi.EAccessMode` (`RO` = 3, `RW` = 4) and follows `pIsLocked`: a float locked by an auto mode reads as `RO`.
- Writing a locked or read-only node raises `genapi.AccessException`. An unknown enum entry raises `genapi.InvalidArgumentException`. Both derive from `genapi.GenericException`, which is a different class from `gentl.GenericException`.
- `IEnumeration.symbolics` leaves out entries whose `pIsAvailable` is false, so the driver's enum choices are the available ones.
- XML loaded with `load_xml_from_string`, from a `local:` URL (NUL-padded, or zipped), works with a `genapi.AbstractPort` subclass passed to `connect()`.
- `gentl.BufferToken` accepts a `bytearray`.

The GenTL hardware run on FLIR's producer has since checked the stream lifecycle, `TLParamsLocked` and `timestamp_ns` ([GenTL on macOS](#gentl-on-macos)). Still unchecked, pending a run on IDS's producer: the `StreamBufferHandlingMode` node on IDS's data stream, `TLParamsLocked` on the IDS camera, and which exception a camera held by another process raises.

## Hardware runs

### 2026-10-22, macOS, Aravis 0.8.36

The lab Mac with five Chameleon3 CM3-U3-50S5M cameras and one IDS camera, via `tests/test_aravis_hardware.py`.

The IDS camera's results, from the first session (8 of 23 passed) to all passing, are in [ids-u3-36p0xcp.md](ids-u3-36p0xcp.md). What they taught about Aravis itself is above, in [How the drivers differ](#how-the-drivers-differ-from-aravis-and-gentl) and [Hazards](#hazards).

**CM3-U3-50S5M** (serial `0135BAD4` in Aravis, `20298452` in Spinnaker; firmware `1.12.3.00`), Python 3.12 (`aravis-test-flir-1.log`): 20 of 23 passed. All 23 pass after these fixes, each verified on this unit:

- **Serial numbers differ between libraries.** Aravis lists FLIR cameras by their USB serial (`0135BAD4`, also the end of the address `1E100135BAD4`), not FLIR's decimal serial number. Address a camera by the serial its own backend's `discover()` reports.
- **Locks.** `get_feature_access_mode()` ignores `pIsLocked`. `ExposureTime` read as RW under `ExposureAuto = Continuous`, while `is_locked()` was true. The driver now checks `is_locked()` (Aravis's `arv_gc_feature_node_get_actual_access_mode` never consults the lock).
- **Range check.** With Aravis's range check on, writing Mono16 failed: "Value '17825799' greater than allowed maximum '17301505'". The camera's XML gives the register behind `PixelFormat` a maximum of Mono8's code. With the check off, Mono16 writes and captures. The driver leaves the check off, since the core already checks ranges. The camera then accepts an out-of-range exposure write without error.
- **Changing the payload.** Making a new stream before the old one was finalized left the new stream with no frames, intermittently (2 runs in 3). Finalizing a stream disables the camera's stream channel. The driver now drops the old stream first. Mono8 → Mono16 → Mono8 then captured every time.
- **Newest only.** Before the stream callback, a capture after a 1 s pause got a frame exposed 972 ms before the call: the buffers had filled, and Aravis dropped the newer frames. With the callback recycling older frames as new ones land, 300 `not_before` captures met 301 stale frames rather than 599, and the capture rate rose from 11.9 to 17.8 per second.
- **Releasing the camera.** A buffer left in a failed test's traceback kept the stream, and with it the USB interface, after close. Reopening then failed with `LIBUSB_ERROR_ACCESS`. Buffers now reach the stream only through their connection.
- **Timestamps.** The offset spread over 5 measurements was 6 µs. The buffer's arrival time minus the device stamp was 0.8 ms on all 10 frames, at 27.9 ms exposure. Aravis takes the arrival time when the frame's leader packet arrives (`arvuvstream.c`), so the camera stamps the frame just before sending it. That fits the stamp marking the end of exposure, as with Spinnaker.
- **Software trigger.** Switching to software trigger took 1.1 s, with a 57 ms turnaround. Triggered exposures began 0.7–1.0 ms after the request (one at 57 ms), against 2–3 ms through Spinnaker.
- **Missing nodes.** There is no `AcquisitionResultingFrameRate` through Aravis (`AcquisitionFrameRate` exists). `PixelFormat` also offers `Mono12Packed` and `Mono12p`, which the driver can't shape.

### GenTL on macOS

Spinnaker installs a GenTL producer on macOS, in `/usr/local/lib/spinnaker-gentl`, and sets `GENICAM_GENTL64_PATH` to it. Through it, `genicam` lists the five CM3-U3-50S5M cameras by FLIR serial number, with addresses like `USB\VID_1E10&SRL_0135D4E2`. [V] So a macOS GenTL producer does exist, for FLIR cameras. The finding that there's no macOS producer (section 6) holds only for IDS cameras.

**CM3-U3-50S5M through it** (`20298452`, Python 3.12, genicam 1.6.0, 2026-10-22): all 23 GenTL hardware tests passed after two fixes. [V] for each point below, on this unit:

- **Buffer memory.** `DSAnnounceBuffer` refuses a `bytearray` ("Parameter not valid or out of range") and accepts `bytes`, as Harvesters uses. The driver now announces `bytes` objects, which the producer fills in place.
- **Opening twice.** The producer lets one process open a camera it already has open. The driver now refuses that itself, with `DeviceBusy`, and a leftover second instance can no longer stream behind the first.
- **Things that work as expected.** `DataStream.buffer_alignment` raises `NotImplementedException`. `defines_payload_size()` is true, the payload is 5,013,504 bytes for Mono8 at 2448 × 2048, and `buffer_announce_min` is 2.
- **Timestamps.** Node `Timestamp`, with an offset spread of 2 µs over 5 measurements.
- **Captures.** 300 `not_before` captures met 600 stale frames, at 11.9 captures per second, which matches Spinnaker. After a 1 s pause the frame taken was exposed 78 ms before the call, so the stream keeps only the newest frame.
- **Software trigger.** Switching to software trigger took 1.27 s, with a 59 ms turnaround. Exposures began 2.2–3.1 ms after the request (median 3.1), with one outlier at 115.9 ms.
- **Ranges.** genapi refuses an out-of-range exposure write itself (`OutOfRangeException`).

Loading it makes genicam print five lines such as "GenTL producer does not implement DSGetNumFlows" to stderr, one for each optional GenTL 1.6 function the producer lacks. They are harmless. The driver logs them at debug level instead of letting them reach the console.

### 2026-10-22, Windows, GenTL

**CM3-U3-50S5M** (`20305122`, firmware `1.12.3.00`) through Spinnaker's producer (`C:\Program Files\FLIR Systems\Spinnaker\cti64\vs2015\FLIR_GenTL_v140.cti`), Python 3.12.10, genicam 1.6.0: 22 of 23 hardware tests passed. [V] for each point below, on this unit:

- **Coarse clock.** The timestamp offset spread over 5 measurements was 8.2 ms, against 2–35 µs on the Mac: `test_timestamp_offset_is_stable` failed. Python 3.12's `time.time()` on Windows ticks every 15.6 ms, and the latch measurement read it either side of each latch.
- **Stale triggered frames, from the same cause.** In `el-devices genicam --trigger software`, every one of 20 captures dropped its first frame as stale and took the next: exposures began a median 77 ms after the request (61–146 ms in the hardware test), against 2–3 ms on the Mac. An offset several milliseconds off dates a triggered frame more than the 1 ms tolerance before its trigger.
- The driver now reads host time from `host_time()`, and times short intervals with `perf_counter()` (see [How the drivers differ](#how-the-drivers-differ-from-aravis-and-gentl)). The unit tests pass under a simulated 15.6 ms clock. **Not yet rerun** on Windows hardware.
- **Other results.** Busy detection, reopening, pixel formats (Mono8 and Mono16), throughput limit and newest-only all passed. 300 `not_before` captures met 563 stale frames, at 13.4 captures per second. Switching to software trigger took 1.23 s, with a "47 ms" turnaround: exactly three ticks of `time.monotonic()`, which the driver then used to time it. genapi refused an out-of-range exposure.
- **Serial and address.** `discover()` lists the FLIR serial (`20305122`) at an address like `USB\VID_1E10&PID_3300&MI_00\7&C357BFF&0&0000_0`.

### 2026-10-22, Linux (Fedora on Asahi, arm64), GenTL and Aravis

The IDS camera: see [ids-u3-36p0xcp.md](ids-u3-36p0xcp.md#gentl-on-linux). In short, over USB 3 GenTL works but can't time the camera's frames, and Aravis got only incomplete frames.

- **IDS's proxy producer.** IDS peak installs `ids_proxygentl.cti` beside `ids_u3vgentl.cti` and `ids_gevgentl.cti`. The proxy wraps IDS's other producers, so with the folder in `GENICAM_GENTL64_PATH` the driver loaded the camera's producer twice, and opening failed with an exception on close. Moving the proxy out of the folder, or naming `ids_u3vgentl.cti` itself in `GENICAM_GENTL64_PATH` (or `--producer` / `producer=`), fixed it. [V by the maintainer] The driver now skips a producer that fails, with a warning, but naming the one producer you want is still the reliable setup.
- **Check the link is USB 3.** The first runs went through a USB 2 A-to-C adapter. Aravis then reported `USB 2.10 (Low bandwidth)`, its stream completed no frames, and GenTL's control channel fell apart. With a direct USB 3 connection (5000M in `lsusb -t`), GenTL passed every test that doesn't need timing. Aravis logs the link speed when it opens a camera (with `Aravis.debug_enable("device:1")`). [V]

### CM3-U3-50S5M: three backends compared

The same camera (`20298452`, firmware `1.12.3.00`) on the same Mac, Python 3.12, 2026-10-22, through each driver's hardware tests. [V] for each figure, from the runs above and `spinnaker.md` (Last run):

| | `SpinnakerCamera` | `AravisCamera` | `GenTLCamera` (FLIR's producer) |
|---|---|---|---|
| Hardware tests | 16 of 16 | 23 of 23 | 23 of 23 |
| Serial listed by `discover()` | `20298452` | `0135BAD4` (USB serial) | `20298452` |
| Timestamp offset spread (5 measurements) | 35 µs | 6 µs | 2 µs |
| 300 `not_before` captures | 600 stale, about 11.9/s | 301 stale, 17.8/s | 600 stale, 11.9/s |
| Switching to software trigger | 0.19 s | 1.1 s | 1.27 s |
| Trigger turnaround | 58 ms | 57 ms | 59 ms |
| Triggered exposure start after the request | 1.8–3.3 ms | 0.7–1.0 ms (one at 57 ms) | 2.2–3.1 ms (one at 116 ms) |
| Out-of-range exposure write | refused by the core | accepted by the camera (range check off), refused by the core | refused by genapi |

The Spinnaker 11.9/s is as the GenTL run reported it ("matches Spinnaker"). So the FLIR cameras work fully without PySpin through either generic driver. Aravis's newest-only callback met half the stale frames of the other two, and captured about 50% faster; why is **unknown**.

---

The numbered sections below are the research notes from Phase 1 of the `usb3-vision-camera` track (2026-10-21), kept as reference for installing and for the libraries' APIs.

## 1. Aravis + PyGObject on macOS (Homebrew, Apple Silicon)

### Versions
- **Aravis 0.8.36** is the current stable release. Upstream tagged 0.8.36 and the development release 0.9.2 on the same day, 2026-04-26. [V] (GitHub releases; https://formulae.brew.sh/api/formula/aravis.json)
  - Aravis uses odd minor numbers for development and even for stable. Before 1.0, stable series with different minors are parallel-installable and incompatible. 0.9.x is the development series for the next stable, with API version `0.10` (main `meson.build`: `aravis_api_version = '0.10'`). 0.8.x has `aravis_api_version = '0.8'`. [V] (`docs/reference/aravis/building.md`, `meson.build` at tags 0.8.36 and main)
- **Homebrew `aravis`** is 0.8.36 and bottled for arm64 sonoma, sequoia, tahoe and golden_gate, plus Linux. Its runtime deps are `glib gstreamer gtk+3 libusb adwaita-icon-theme gettext`, and `gobject-introspection` is a build-only dep, so the typelib is generated and installed. [V] (https://github.com/Homebrew/homebrew-core/blob/HEAD/Formula/a/aravis.rb)
- **Homebrew `pygobject3`** is 3.58.1. It is built **only for Homebrew `python@3.14` and `python@3.15`**. [V] (Formula/p/pygobject3.rb, `depends_on "python@3.14"` / `"python@3.15"`)
- **PyPI `PyGObject`** is 3.58.1 (2026-10-09). It ships an **sdist only, with no wheels**, has `requires-python >=3.10`, and requires `pycairo>=1.16`. The build backend is meson-python. It needs `girepository-2.0 >= 2.80`, `glib-2.0 >= 2.80` and `libffi`, and needs cairo for the pycairo integration. [V] (PyPI JSON; `meson.build` in the 3.58.1 sdist)
- **PyPI `pycairo`** 1.29.2 has wheels **for Windows only**. On macOS and Linux it builds from source against system cairo. [V] (PyPI JSON)

### Install commands
Recommended path: Homebrew libraries plus a pip-built PyGObject in the venv.
```bash
brew install aravis pkgconf cairo        # glib comes with aravis; it provides girepository-2.0.pc
python3.12 -m venv .venv && source .venv/bin/activate
pip install pycairo                      # must come first (see below)
pip install PyGObject
python -c "import gi; gi.require_version('Aravis','0.8'); from gi.repository import Aravis; print(Aravis.get_major_version(), Aravis.get_minor_version())"
```
- **Tested here [V]:** PyGObject 3.58.1 and pycairo 1.29.2 build from source in a Python 3.12 venv against Homebrew glib 2.90 and cairo 1.18.6. `from gi.repository import GLib` then works **with no `GI_TYPELIB_PATH`**. The built `_gi.so` links `/opt/homebrew/opt/glib/lib/libgirepository-2.0.0.dylib`. Homebrew's `libgirepository-2.0.dylib` has `/opt/homebrew/lib/girepository-1.0` compiled in as a search path (confirmed with `strings`), and `brew install aravis` links `Aravis-0.8.typelib` there. So the typelib should be found automatically. [V for the path; I for Aravis itself, which was not installed here]
- **Gotchas found while testing [V]:**
  - The build needs a **`pkg-config` binary**. Without one, the pycairo build fails with `Did not find pkg-config`. Use `brew install pkgconf`. The PyPI `pkgconf` wheel also works, but you then have to set `PKG_CONFIG_PATH` to include `/opt/homebrew/lib/pkgconfig`, `/opt/homebrew/share/pkgconfig` and Homebrew's macOS SDK pc dir. Brew's pkgconf already knows all three.
  - Install **pycairo in a separate, earlier `pip install`**. If pycairo isn't already in the target environment, the PyGObject build falls back to looking for `py3cairo.h` through pkg-config and can fail with `'py3cairo.h' file not found`. PyGObject's own docs also install `pycairo` first, as a separate step (https://pygobject.gnome.org/getting_started.html).
  - Xcode Command Line Tools are needed for the C compiler. [I]
- **Alternative [V]:** use Homebrew's `python@3.14` or `python@3.15` with `brew install pygobject3`, and create the venv with `--system-site-packages`. This avoids building, but only works for those two Python versions.
- **`GI_TYPELIB_PATH`:** only needed when Aravis lives outside Homebrew's prefix, for example a source build in `/usr/local`. Aravis' own example scripts carry a note to that effect. [V] (`tests/python/*.py` header)
- **The shared library:** girepository dlopens the library named in the typelib. Homebrew typelibs normally record an absolute `/opt/homebrew/...` dylib path, so `DYLD_LIBRARY_PATH` should not be needed. [I]

### `gi.require_version` string
- Use **`gi.require_version('Aravis', '0.8')`** for every 0.8.x release, which covers Homebrew, Ubuntu and Debian. A 0.9.x or 0.10 install would need `'0.10'` and has a slightly different API: 0.9.1 says "replace create_stream_full by stream_create, break API". [V] (Aravis NEWS.md on main; 0.8.36 example scripts)

### USB permissions on macOS
- **[I]** macOS has no udev, and no kernel driver claims USB3 Vision class devices (misc class `EF/02/01`), so libusb can open them as an ordinary user. No macOS-specific USB permission issue showed up in Aravis' issue tracker. The open macOS issue there, #1092, is about GigE discovery. On macOS, Aravis is used through the Homebrew build, and Aravis CI targets Linux, MinGW and MSVC.
- For best throughput, Aravis can use libusb's async API: `camera.uv_set_usb_mode(Aravis.UvUsbMode.ASYNC)`. The default is sync. [V] (`docs/reference/aravis/usb.md`)

---

## 2. Aravis + PyGObject on Linux (Ubuntu 24.04 "noble")

### apt packages
Ubuntu versions are from Launchpad's published sources. [V]

| Series | aravis | pygobject (python3-gi) | libgirepository-2.0-dev |
|---|---|---|---|
| noble 24.04 | **0.8.30-1build2** | 3.48.2-1 | 2.80.0-6ubuntu3.x (glib 2.80) |
| resolute 26.04 | 0.8.34-1build2 | 3.56.2-1 | 2.88.0 |
| next (stonking/tenacious) | 0.8.36-2 | 3.57.1-1 | 2.90.0 |

The Debian source package `aravis` (0.8.36-2) produces these binary packages: `libaravis-0.8-0`, `libaravis-dev`, **`gir1.2-aravis-0.8`**, `libaravis-doc`, `aravis-tools` (viewer) and `aravis-tools-cli` (`arv-tool-0.8`, ...). [V] (https://sources.debian.org/src/aravis/0.8.36-2/debian/control)

**Option A: system Python**
```bash
sudo apt install gir1.2-aravis-0.8 libaravis-0.8-0 aravis-tools-cli python3-gi
python3 -m venv --system-site-packages .venv   # gets python3-gi (Py 3.12 on noble)
```

**Option B: any Python, with PyGObject built by pip** (follows PyGObject's getting-started page)
```bash
sudo apt install gir1.2-aravis-0.8 libgirepository-2.0-dev libcairo2-dev pkg-config python3-dev gcc
pip install pycairo && pip install PyGObject
```
- PyGObject **3.52 and later** link `girepository-2.0`, which ships in glib 2.80+. Older releases used `libgirepository1.0-dev`. Current PyGObject 3.58.1 needs glib >= 2.80. Noble ships glib **2.80.0**, so it meets the requirement exactly, and `libgirepository-2.0-dev` exists in noble. [V] (PyGObject `meson.build`; Launchpad)
- On jammy (22.04, glib 2.72) the current PyGObject won't build, and you'd need to pin `PyGObject<3.51`. [I, inferred from the glib 2.80 floor]
- The typelib goes to `/usr/lib/x86_64-linux-gnu/girepository-1.0/Aravis-0.8.typelib`, which is a default search path, so `GI_TYPELIB_PATH` is not needed. [I]

### Building Aravis from source (Fedora)

Fedora's main repositories don't appear to package Aravis; a COPR project builds it (`emmanuelp/Aravis`). [I] Built from source with meson into the default `/usr/local` prefix, Aravis puts its library and typelib under `/usr/local/lib64`. Fedora searches neither: its linker leaves `/usr/local/lib64` out of the default path (Ubuntu includes `/usr/local/lib`), and GObject introspection looks only in its own prefix. `gi.require_version("Aravis", "0.8")` then fails with "Namespace Aravis not available". [V on the lab's Fedora arm64 machine, 2026-10-22]

PyGObject needs to find two things: the typelib (`Aravis-0.8.typelib`), and the shared library it names (`libaravis-0.8.so.0`), which it loads through the dynamic linker.

**Recommended:** register the library with the linker once, and set the typelib path in the venv:

```bash
echo /usr/local/lib64 | sudo tee /etc/ld.so.conf.d/usr-local.conf && sudo ldconfig
echo 'export GI_TYPELIB_PATH=/usr/local/lib64/girepository-1.0' >> venv/bin/activate
```

Every option, for the library and for the typelib: [I unless marked]

| Option | Library | Typelib | Trade-offs |
|---|---|---|---|
| `/etc/ld.so.conf.d/` entry + `ldconfig` | Yes | No | The standard way to add a library directory: one root-owned file, every program sees it. |
| `GI_TYPELIB_PATH` in the venv's `bin/activate` | No | Yes | Only that venv, and only once activated; `venv/bin/python` run directly doesn't get it. |
| `GI_TYPELIB_PATH` in `/etc/profile.d/aravis.sh` | No | Yes | Every login shell; not services or cron. |
| `LD_LIBRARY_PATH` | Yes | No | Per shell, and it overrides the system's choice of libraries for everything run from that shell. Works ([V]), but best kept for a quick test. |
| Symlink the typelib into `/usr/lib64/girepository-1.0/` | No | Yes | No variable needed, but puts a file the package manager doesn't own in `/usr`. |
| Build with `meson setup --prefix=/usr` | Yes | Yes | Needs nothing else, but installs files the package manager doesn't know about into `/usr`, where a later Aravis package could overwrite them. |
| A packaged Aravis (COPR, or `dnf search aravis`) | Yes | Yes | Installs to `/usr` and is managed by `dnf`; depends on a third-party repository and its version. |

Then install the udev rule and raise usbfs memory as below.

### udev rule
- Upstream ships an **example** file **`src/aravis.rules`**. It is **not installed** by meson, and the docs tell you to copy it into `/etc/udev/rules.d/`. [V] (`src/meson.build` has no rules install; `docs/reference/aravis/usb.md`)
- **Debian/Ubuntu packages don't install it either.** The 0.8.36-2 packaging has no `*.udev` file and the `.install` files don't list it. [V] (sources.debian.org)
- Contents at 0.8.36 [V]. It has one line per vendor; the IDS vendor ID **1409** is included:
  ```
  # IDS Imaging Development Systems GmbH
  SUBSYSTEM=="usb", ATTRS{idVendor}=="1409", MODE:="0666", TAG+="uaccess", TAG+="udev-acl"
  ```
  The other vendors listed are Basler `2676`, TIS `199e`, Point Grey/FLIR `1e10`, Daheng `2ba2`, Dahua `2e03`, Omron Sentech `1421` and Hikrobot `2bdf`.
- The docs also give a generic rule for all USB3 Vision devices [V]:
  ```
  SUBSYSTEM=="usb", ATTRS{bDeviceClass}=="ef", ATTRS{bDeviceSubClass}=="02", ATTRS{bDeviceProtocol}=="01", ENV{ID_USB_INTERFACES}=="*:ef0500:*", MODE="0666"
  ```
- Suggested install: `sudo cp aravis.rules /etc/udev/rules.d/70-aravis.rules && sudo udevadm control --reload-rules && sudo udevadm trigger`. The file name is up to you. [I]
- IDS peak ships its own rule and an `ids_install_udev_rule.sh` in `share/ids-peak/scripts`. [V] (IDS peak Linux readme)

### usbfs memory
- The Linux kernel's default `usbfs_memory_mb` is **16 MB**. Aravis' USB stream code comments: "The kernel USB memory buffer limit has been reached (default 16MBytes) ... `sudo modprobe usbcore usbfs_memory_mb=1000`". [V] (`src/arvuvstream.c`)
- IDS gives the same advice for high-resolution cameras: `echo 1000 > /sys/module/usbcore/parameters/usbfs_memory_mb`, made persistent with `ids_set_usb_mem_size.sh`. [V] (IDS peak 26.06 Linux readme)
- A 20 MP Mono8 frame is about 20 MB, which is already larger than 16 MB. **Set it to 1000** (or 0 for unlimited) through a kernel command-line option or `/etc/modprobe.d`. [I]
- Aravis' maximum USB transfer size is 1 MiB per transfer (`ARV_UV_STREAM_MAXIMUM_TRANSFER_SIZE`). It is configurable from 0.9.0. [V]

---

## 3. Aravis Python API essentials (0.8.x, through GI)

All C functions `arv_<class>_<method>` become `Aravis.<Class>.<method>`. Functions that take `GError **` raise **`gi.repository.GLib.Error`**. [V for headers; GI mapping is standard]

### Discovery (module-level functions, `arvsystem.h`) [V]
```python
Aravis.update_device_list()
n = Aravis.get_n_devices()
for i in range(n):
    Aravis.get_device_id(i), Aravis.get_device_vendor(i), Aravis.get_device_model(i)
    Aravis.get_device_serial_nbr(i), Aravis.get_device_protocol(i)  # "USB3Vision"
    (
        Aravis.get_device_physical_id(i),
        Aravis.get_device_address(i),
        Aravis.get_device_manufacturer_info(i),
    )
Aravis.enable_interface(
    "Fake"
)  # fake camera for tests; also disable_interface/select_interface
Aravis.shutdown()
```
- For USB3 Vision, the camera ID passed to `Aravis.Camera.new(name)` is `<vendor_alias>-<serial>` or `<vendor>-<serial>`. Passing `None` opens the first camera. [V] (doc comment on `arv_camera_new`)
- Calling `update_device_list` takes a global system mutex. [V]

### Camera and device [V]
```python
cam = Aravis.Camera.new(device_id)            # raises GLib.Error if not found
dev = cam.get_device()                        # Aravis.Device: generic GenICam feature access
cam.get_device_serial_number(); cam.get_vendor_name(); cam.get_model_name()
cam.is_uv_device(); cam.uv_set_bandwidth(..); cam.uv_get_bandwidth_bounds(); cam.uv_set_usb_mode(mode)
cam.set_exposure_time(us); cam.get_exposure_time_bounds()  # -> (min, max)
cam.set_gain(db); cam.get_gain_bounds(); cam.set_frame_rate(..); cam.get_frame_rate_bounds()
cam.set_trigger("Software"/"Line1"); cam.software_trigger(); cam.clear_triggers()
cam.set_pixel_format_from_string("Mono12"); cam.dup_available_pixel_formats_as_strings()
cam.get_payload(); cam.start_acquisition(); cam.stop_acquisition(); cam.abort_acquisition()
cam.acquisition(timeout_us)                    # one-shot convenience
```
Device feature API (`arvdevice.h`):
```python
dev.get_float_feature_value(name)
dev.set_float_feature_value(name, v)
dev.get_float_feature_bounds(name)  # -> (min, max)
dev.get_float_feature_increment(name)
dev.get_integer_feature_value / set_integer_feature_value / get_integer_feature_bounds / get_integer_feature_increment
dev.get_boolean_feature_value / set_boolean_feature_value
dev.get_string_feature_value / set_string_feature_value  # also enumerations, by entry name
dev.dup_available_enumeration_feature_values_as_strings(name)  # list[str]
dev.dup_available_enumeration_feature_values_as_display_names(name)
dev.is_enumeration_entry_available(name, entry)
dev.execute_command(name)
dev.is_feature_available(name)  # raises on error; also is_feature_implemented
dev.get_feature_access_mode(name)  # -> Aravis.GcAccessMode: UNDEFINED=-1, RO, WO, RW
dev.get_feature_representation(name)
dev.get_feature_value(name) / set_feature_value(
    name, v
)  # generic GValue accessors (0.8.32+)
dev.get_feature(name)  # ArvGcNode for deeper inspection
dev.set_range_check_policy(Aravis.RangeCheckPolicy.ENABLE)
dev.set_access_check_policy(Aravis.AccessCheckPolicy.ENABLE)
dev.set_register_cache_policy(...)
```
- In 0.8 the enum helpers are named **`dup_available_enumeration_feature_values_as_strings`**, not `get_...`. [V] (`arvdevice.h`)
- **Watch out [V]:** `ARV_RANGE_CHECK_POLICY_DEFAULT = DISABLE` and `ARV_ACCESS_CHECK_POLICY_DEFAULT = DISABLE`. By default, Aravis does **not** check min/max or access mode on writes, so a value out of range goes straight to the device, which may reject or clamp it. Enable both policies, or validate against the bounds yourself, if the contract needs a `ValueError` on out-of-range input.

### Stream and buffers [V]
```python
stream = cam.create_stream(None, None)  # optional callback(user_data, cb_type, buffer)
payload = cam.get_payload()
for _ in range(n):
    stream.push_buffer(Aravis.Buffer.new_allocate(payload))
cam.start_acquisition()
buf = stream.timeout_pop_buffer(timeout_us)  # µs! returns None on timeout
# also: stream.pop_buffer() (blocking), stream.try_pop_buffer() (non-blocking)
if buf is not None:
    st = buf.get_status()  # Aravis.BufferStatus
    data = buf.get_data()  # bytes (copy) of the payload
    # or buf.get_image_data(), get_image_width/height/x/y, get_image_pixel_format()
    buf.get_frame_id()
    buf.get_timestamp()
    buf.get_system_timestamp()
    stream.push_buffer(buf)  # recycle
stream.get_n_buffers()  # -> (n_input, n_output)
stream.get_statistics()  # -> (completed, failures, underruns)
stream.get_info_uint64_by_name("n_underruns")
```
- `timeout_pop_buffer` takes **microseconds** and returns NULL (`None`) on timeout instead of raising. It is a thin wrapper around `g_async_queue_timeout_pop`. [V] (`arvstream.c`)
- **`ArvBufferStatus` values** [V]: `UNKNOWN=-1, SUCCESS=0, CLEARED, TIMEOUT, MISSING_PACKETS, WRONG_PACKET_ID, SIZE_MISMATCH, FILLING, ABORTED, PAYLOAD_NOT_SUPPORTED`.
- **Timestamps** [V]: `get_timestamp()` is the camera timestamp in ns (from the device's tick counter). `get_system_timestamp()` is the host time in ns when the buffer was filled. Issue #887 reports a camera timestamp of 0 on one device; the maintainer says the pop variants don't differ, so a 0 is device-specific. Fall back to the system timestamp. [V] (https://github.com/AravisProject/aravis/issues/887)
- `ArvStreamCallbackType` [V]: `INIT, EXIT, START_BUFFER, BUFFER_DONE`. The callback runs on the stream's **internal thread**.
- With the 0.8 GI bindings, `get_data()` returns a Python `bytes` copy. Wrapping it with `numpy.frombuffer(data, dtype)` costs one copy per frame. [I]

### Errors [V]
- Errors come back as `GLib.Error` with `.domain` (a quark string), `.code` and `.message`.
  - `arv-device-error-quark` maps to `Aravis.DeviceError`: `WRONG_FEATURE, FEATURE_NOT_FOUND, NOT_CONNECTED, PROTOCOL_ERROR, TRANSFER_ERROR, TIMEOUT, NOT_FOUND, INVALID_PARAMETER, GENICAM_NOT_FOUND, NO_STREAM_CHANNEL, NOT_CONTROLLER, UNKNOWN`.
  - `arv-gc-error-quark` maps to `Aravis.GcError`: `PROPERTY_NOT_DEFINED, PVALUE_NOT_DEFINED, INVALID_PVALUE, EMPTY_ENUMERATION, OUT_OF_RANGE, NO_DEVICE_SET, NO_EVENT_IMPLEMENTATION, NODE_NOT_FOUND, ENUM_ENTRY_NOT_FOUND, INVALID_LENGTH, READ_ONLY, SET_FROM_STRING_UNDEFINED, GET_AS_STRING_UNDEFINED, INVALID_BIT_RANGE, INVALID_SYNTAX`.
  - Other domains exist: `arv-chunk-parser-error-quark`, `arv-dom-document-error-quark` and `arv-xml-schema-error-quark`.
- Match errors with `e.matches(Aravis.DeviceError.quark(), Aravis.DeviceError.TIMEOUT)`, or compare `e.domain == 'arv-device-error-quark'`. [I, standard PyGObject]
- The device emits a **`control-lost`** signal when it disconnects, for example `dev.connect('control-lost', cb)`. [I; exists in `arvdevice.c` signals]
- In the 0.8 Python examples, "no camera" sometimes shows up as `TypeError`, for example `Aravis.Camera(name=...)` failing construction. Code should catch both `GLib.Error` and `TypeError`. [V] (`tests/python/arv-feature-test.py`)

### "Newest only" idiom
- There is **no StreamBufferHandlingMode in ArvStream** [V]. The model is two `GAsyncQueue`s: an input queue of empty buffers and an output queue of filled ones. When the input queue is empty, the frame is dropped and `n_underruns` goes up. [V] (`arvuvstream.c`)
- Idioms [I]:
  1. **Drain to newest:** `buf = timeout_pop_buffer(t)`, then loop `nxt = try_pop_buffer()`. For each `nxt`, push `buf` back and keep `nxt`. You end with the newest frame and every older one recycled.
  2. **Single-frame acquisitions:** this suits a scanner that triggers per capture. Use software or hardware trigger mode, `push` N buffers, `software_trigger()`, then `timeout_pop_buffer`, so there are never stale frames. Or call `cam.acquisition(timeout)` for one shot.
  3. Keep **2–3 buffers**, since each 20 MP Mono8 buffer is about 20 MB.

### Thread safety [V] (`docs/reference/aravis/thread-safety.md`)
- "Aravis is not thread safe ... one can not use the same object simultaneously from different threads, without using mutexes. But it is perfectly fine to use different aravis objects in different threads, with the exception of ArvInterface instances."
- Callbacks and signals run on the internal stream thread. In that context, **only** `push_buffer`, `pop_buffer`, `try_pop_buffer` and `timeout_pop_buffer` are safe. These four are thread safe anywhere. A blocking `Device` API in the driver should therefore hold its own lock around all `ArvDevice` and `ArvCamera` calls.
- PyGObject releases the GIL during C calls into introspected libraries. [I, standard PyGObject behaviour]

---

## 4. IDS peak GenTL producer

- **Current version:** IDS switched to year.month numbering. **IDS peak 26.06** is current, with the U3V Transport Layer at **2.0.0.0**. The previous release was 2.21 (U3V TL 1.22.0.0). [V] (https://en.ids-imaging.com/files/downloads/ids-peak/readme/ids-peak-linux-readme-26.06_EN.html and the `-windows-` equivalent)
- **Platforms:** Windows 10/11 64-bit, and Linux x86-64 and arm64 (tested on Ubuntu 26.04 and 25.10, Debian 12 and 13, Jetson and Raspberry Pi OS). There is **no macOS build**. [V]

### Linux
- Install from the IDS apt repo, which needs IDS webstore credentials in `/etc/apt/auth.conf.d/`: `deb [signed-by=/etc/apt/keyrings/ids-archive-keyring.gpg] https://repo.ids-imaging.com/ubuntu noble main`, then `sudo apt install ids-peak`. The other route is the `ids-peak_<ver>_<arch>.tgz` archive. [V] (26.06 readme)
- **CTI files** are `ids_u3vgentl.cti`, `ids_gevgentl.cti` and, with the uEye TL, `ids_ueyegentl.cti`. The archive layout is `<root>/lib/<arch>/ids-peak/cti/` in 26.06. Older archives used `lib/ids/cti`. [V] (readme file tree)
- The Debian package "has the same structure" under `/usr`. Community reports give `/usr/lib/ids/cti` (CVB forum, ids-peak 2.17; several GitHub repos). [I] Confirm with `dpkg -L ids-peak-u3v-gentl 2>/dev/null | grep cti` or `find /usr -name 'ids_u3vgentl.cti'`.
- **`GENICAM_GENTL64_PATH`:** for the archive, IDS's start scripts and CMake-generated scripts set it to the `cti` dir. The readme tells you to set it yourself for your own programs: `GENICAM_GENTL64_PATH=$GENICAM_GENTL64_PATH:/opt/ids_peak_<ver>_<arch>/lib/<arch>/ids-peak/cti`. [V] One user reports the Debian package updating it to `/usr/lib/ids/cti`. [I] (https://forum.commonvisionblox.com/t/camera-access-chunk-data-access-with-ids-camera/2437)
- The archive also needs `LD_LIBRARY_PATH` for IDS libs. Scripts in `share/ids-peak/scripts` cover the udev rule (`ids_install_udev_rule.sh`) and usbfs (`ids_set_usb_mem_size.sh`). [V]

### Windows
- The installer offers runtime, standard and extended setups. All of them include "USB3 Vision camera drivers" and the **U3VK** transport layer, described as "GenTL Producer for IDS USB3 Vision cameras ... **including a driver** to improve USB performance". [V] (26.06 Windows readme)
- **CTI path:** `C:\Program Files\IDS\ids_peak\ids_u3vgentl\64\ids_u3vgentlk.cti`. [V] IDS's own `ids-imaging/ids-peak-examples` repo prints this key (csharp/Reconnect/README.md), and it matches other users' DLL dumps.
- The installer adds the producer dir to `GENICAM_GENTL64_PATH`. [I] This is the GenTL convention, and the readme says the Proxy Producer "registers ... by adding its path to GENICAM_GENTL64_PATH". Verify with `echo %GENICAM_GENTL64_PATH%`.

### Non-IDS cameras on IDS's producer
- **No.** The IDS peak 2.0 release notes state that IDS peak "does not allow the use of third party cameras in combination with IDS transport layers". They also say third-party TLs aren't shown, because vendors lock functions or interpret GenTL differently. IDS peak 1.3 was the last version where third-party products could be used. [V via search-result excerpts of en.ids-imaging.com and ids-imaging.us release notes 1.3 and 2.0; the pages are behind Cloudflare and couldn't be fetched directly]
- The readme repeatedly says "GenTL Producer **for IDS** USB3 Vision cameras". [V]
- **Consequence:** a FLIR or other non-IDS U3V camera needs that vendor's own producer under the GenTL backend (Spinnaker's `FLIR_GenTL*.cti`, pylon's `ProducerU3V.cti`, and so on), or Aravis.

### Windows USB driver exclusivity
- U3VK installs an IDS kernel driver bound to IDS cameras (USB VID 1409). [V that a driver is installed; I that it binds by VID]
- On Windows only one driver can bind a USB device at a time. While IDS's driver owns the camera, a WinUSB/libusb-based stack can't open it, and neither can another vendor's driver-based producer. That covers Aravis on MSYS2, and Spinnaker/pylon producers if they even accepted the camera. Switching means rebinding the driver in Device Manager or Zadig. [I, general Windows USB driver model; IDS documents no shared-driver mode]
- Other vendors' producers that do open third-party U3V cameras (e.g. Stemmer CVB, MVTec) use their own driver installers for the same reason. [I]

### IDS-specific operational notes [V] (readmes)
- uEye XCP/XLE/XLS models **don't work on USB 2.0 at all**.
- **ASMedia USB controllers** have known issues with XCP. The workaround is to set DataStream node `U3vStreamChannelTransferRequestCount = 1` (default 6).
- If usbfs memory can't be raised, set `U3VStreamChannelTransferRequestMode = Manual` and lower `U3VStreamChannelTransferRequestCount`. These are data-stream (GenTL DS) nodes, so you can reach them through the GenTL DS nodemap but not through Aravis.

---

## 5. Harvesters vs. `genicam` directly

### Facts
- **`harvesters`** [V] (PyPI; GitHub):
  - Latest release is **1.4.3** (2024-05-16), a pure-Python `py3-none-any` wheel under Apache-2.0. It requires `genicam>=1.2` and `numpy`, with no `requires-python`.
  - The last commit on `master` was 2024-05-08, so there has been **no release or commit for about 2.5 years**. The repo isn't archived and issues keep arriving (open issues up to #508, August 2026), but the maintainer rarely answers.
  - Notable open issues:
    - **#508** (2026-08): `ImageAcquirer.fetch()`/`try_fetch()` **holds the GIL while waiting** and starves other threads. PySpin's equivalent call doesn't.
    - #507: `__version__` reports 1.4.2.
    - #502: memory growth on create/destroy cycles with some producers.
    - #487: SIGSEGV on Basler U3V producer init/deinit.
    - #475: `Harvester.update()` breaks existing connections.
- **`genicam`** (EMVA "official Python binding of GenICam", GenICam licence `LicenseRef-GenICam-1.6`) [V] (PyPI JSON):
  - Latest is **1.6.0** (2026-09-01). Earlier: 1.5.1 (2025-11), 1.5.0 (2025-08), 1.4.0 (2024-05).
  - Wheels for **cp310, cp311, cp312, cp313 and cp314** on **win_amd64, manylinux2014_x86_64, manylinux2014_aarch64, manylinux2014_armv7l and macosx_13_0_arm64**. There is no sdist, no musllinux build and no free-threaded build. That covers the 3.11–3.14 CI matrix on all three OSes. The macOS arm64 wheel is new; issue #497 asked for it. CI on Intel macOS runners would have no wheel. [I] (no macosx x86_64 wheel listed)
  - Its own docs say the `gentl` sub-module is "**[TO BE DOCUMENTED]**". The only documentation is Harvesters' source and the GenTL standard.
- **`genicam.gentl` API**, introspected from genicam 1.6.0 on this Mac [V]:
  - `GenTLProducer.create_producer()`, then `.open(cti_path)`, then `.create_system()`, which gives a `System` with `.open()`, `.update_interface_info_list(timeout_ms)` and `.interface_info_list`.
  - Each interface info gives `.create_interface()`, then `Interface.open()`, `.update_device_info_list(timeout_ms)` and `.device_info_list`.
  - `DeviceInfo` has `.vendor .model .serial_number .id_ .user_defined_name .access_status .create_device()`.
  - `Device` has `.open(DEVICE_ACCESS_FLAGS_LIST.DEVICE_ACCESS_EXCLUSIVE|CONTROL|READONLY)`, `.remote_port` (camera features), `.local_port` (TL device module), `.data_stream_ids`, `.create_data_stream()`, `.register_event()` and `.timestamp_frequency`.
  - `DataStream` has `.open(id)`, `.port` (DS nodemap, for StreamBufferHandlingMode and the IDS U3V nodes), `.payload_size`, `.defines_payload_size()`, `.buffer_announce_min`, `.announce_buffer(BufferToken(bytearray, ctx))`, `.queue_buffer(buf)`, `.start_acquisition(ACQ_START_FLAGS_LIST.ACQ_START_FLAGS_DEFAULT, -1)`, `.stop_acquisition(ACQ_STOP_FLAGS_LIST.ACQ_STOP_FLAGS_KILL)`, `.flush_buffer_queue(ACQ_QUEUE_TYPE_LIST.ACQ_QUEUE_ALL_DISCARD | ACQ_QUEUE_OUTPUT_DISCARD | ...)`, `.revoke_buffer()`, `.num_underrun`, `.num_delivered` and `.register_event(EVENT_TYPE_LIST.EVENT_NEW_BUFFER)`.
  - `EventManagerNewBuffer(event_token)` has `.update_event_data(timeout_ms)`, which raises `TimeoutException`, and `.buffer`, `.flush_event_queue()` and `.kill()`.
  - `Buffer` has `.raw_buffer .is_complete() .frame_id .timestamp_ns .timestamp .width .height .pixel_format .data_size .payload_type .parts`.
  - Exceptions are a `GenericException` hierarchy, including `TimeoutException`, `ResourceInUseException`, `NotAvailableException`, `AccessDeniedException`, `IoException` and `InvalidParameterException`, among others.
- **Feature access with genicam directly** [V] (Harvesters core.py):
  1. Read `port.url_info_list[0].url`.
  2. Parse `local:///file.zip;ADDR;SIZE` (hex). The URL can also be `file:` or `http:`.
  3. Read the XML or zip with `port.read(addr, size)`.
  4. Load it with `NodeMap().load_xml_from_zip_file/_from_file/_from_string`.
  5. Write a small `genapi.AbstractPort` subclass whose `read` and `write` forward to `gentl.Port`. Harvesters' `ConcretePort` is about 30 lines.
  6. Call `node_map.connect(concrete_port, port.name)`.
  Features are then reached as `node_map.ExposureTime.value`, `.min`, `.max` and `.inc`, with `.symbolics` for enum entries, `execute()` for commands and `get_access_mode()`.

### How hard is genicam directly?
Moderate. The code Harvesters actually uses for one-camera acquisition comes to about 250–400 lines:
- producer → system → interface → device enumeration
- device open
- XML retrieval and NodeMap wiring, covering `local:`, zipped and `file:` URLs
- announce, queue and start
- an event-wait loop
- cleanup in the correct reverse order: stop, flush, revoke, close stream, close device, close interface, close system, close producer

The hard part is the absence of documentation. You learn the API from Harvesters' source and the GenTL spec. [I]

### Recommendation: **use `genicam` directly and don't depend on Harvesters**
1. **Maintenance:** Harvesters hasn't released or committed since May 2024 and has unanswered crash and leak reports. `genicam` is actively released by EMVA (1.5.0, 1.5.1, 1.6.0 in the last 14 months), with wheels for cp311–cp314 on all three CI OSes.
2. **Fit with the project's design** (ADR 0001, blocking and framework-free): Harvesters adds its own background threads, callbacks, a `with ia.fetch()` ownership model and `update()` quirks (#475), which would have to be undone to fit the `Device` contract. Driving GenTL directly gives exact control over buffer count, newest-only behaviour (DS `StreamBufferHandlingMode` when the producer exposes it, or `flush_buffer_queue(ACQ_QUEUE_OUTPUT_DISCARD)` before a trigger), open and close order, and exclusive access.
3. **Fewer dependencies:** one compiled wheel (`genicam`) instead of `genicam` plus Harvesters' pinned behaviour. Harvesters only adds numpy, which is already a dependency.
4. **IDS-specific nodes:** the DS-module nodes IDS documents, such as `U3vStreamChannelTransferRequestCount`, need the DS port nodemap. That is simple with genicam directly; Harvesters exposes it only indirectly.
5. **Risk to plan for:** the GIL problem in #508 most likely sits in the `genicam` binding's `update_event_data` rather than in Harvesters' Python code [I]. Wait on events with **short timeouts in a loop** (e.g. 50–100 ms) so other threads get scheduled, and test it.
   - Use Harvesters' `core.py` (Apache-2.0) as the reference implementation, and credit it if any code is adapted. A thin optional "Harvesters-compatible" path isn't worth keeping.
   - Declare `genicam` as an extra (e.g. `[gentl]`) and import it lazily, as the project's conventions require. `genicam`'s licence is the GenICam licence, not OSI. Check that it's compatible with distributing under AGPLv3 as an optional dependency (installing it is the user's choice). [I]

---

## 6. USB3 Vision GenTL producers on macOS (quick check)

- **IDS peak has no macOS release** (Windows and Linux only). [V]
- **Basler pylon for macOS:** pylon 6.1.2 release notes added "a GenTL producer ... that allows using Basler cameras (GigE or USB 3.0)", and ProducerU3V.cti/ProducerGEV.cti fixes are mentioned. pylon 26.06 supports macOS Sonoma or newer. It is **Basler-cameras-only** and wouldn't open the IDS camera. [V for existence from search excerpts of Basler release notes and the pypylon PyPI page; I for exclusivity]
- **Aravis 0.9.0** (dev series) added an "experimental" GenTL producer and consumer. It is development only and unsuitable for the stable driver. [V] (Aravis NEWS.md)
- No other vendor (Stemmer CVB, MVTec, Euresys, Balluff/MATRIX VISION) is known to ship a third-party-capable USB3 Vision GenTL producer for macOS. [I] FLIR's Spinnaker does ship one for its own cameras (see Hardware runs, GenTL on macOS). [V] On macOS, **Aravis 0.8 is the only practical backend** for the IDS camera.

---

## Sources
- Aravis 0.8.36 source (headers, `docs/reference/aravis/{usb,thread-safety,building}.md`, `src/aravis.rules`, `src/arvuvstream.c`, `tests/python/*.py`): https://github.com/AravisProject/aravis/tree/0.8.36
- Aravis NEWS on main: https://github.com/AravisProject/aravis/blob/main/NEWS.md ; issues #887, #492, #999, #1092
- Homebrew formulae: https://github.com/Homebrew/homebrew-core/blob/HEAD/Formula/a/aravis.rb , .../p/pygobject3.rb , .../g/glib.rb ; https://formulae.brew.sh/api/formula/aravis.json
- PyGObject: https://pypi.org/project/PyGObject/ ; https://pygobject.gnome.org/getting_started.html ; pygobject-3.58.1 `meson.build`
- pycairo: https://pypi.org/project/pycairo/
- Ubuntu/Launchpad: https://launchpad.net/ubuntu/+source/aravis ; Debian packaging: https://sources.debian.org/src/aravis/0.8.36-2/debian/
- IDS peak readmes: https://en.ids-imaging.com/files/downloads/ids-peak/readme/ids-peak-linux-readme-26.06_EN.html , https://en.ids-imaging.com/files/downloads/ids-peak/readme/ids-peak-windows-readme-26.06_EN.html (also 2.21)
- IDS peak 2.0 / 1.3 release notes (third-party policy): https://en.ids-imaging.com/release-note/items/release-notes-ids-peak-2-0.html , https://en.ids-imaging.com/release-note/items/release-notes-ids-peak-1-3.html
- IDS examples (Windows cti path): https://github.com/ids-imaging/ids-peak-examples (csharp/Reconnect/README.md)
- CVB forum (Linux cti path report): https://forum.commonvisionblox.com/t/camera-access-chunk-data-access-with-ids-camera/2437
- Harvesters: https://pypi.org/project/harvesters/ ; https://github.com/genicam/harvesters (issues #475, #487, #497, #502, #503, #507, #508)
- genicam: https://pypi.org/project/genicam/
- Basler pylon macOS: https://www2.baslerweb.com/media/downloads/documents/release_notes/ReleaseNotes~6.txt ; https://pypi.org/project/pypylon/
