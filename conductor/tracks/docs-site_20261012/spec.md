# Specification: Documentation Site and Machine Setup

**Track ID:** docs-site_20261012
**Type:** Feature
**Created:** 2026-10-12 (setup scope merged in 2026-10-23)
**Status:** Draft

## Summary

Getting a machine ready for a backend needs much more than `pip install`: vendor SDKs, system libraries, udev rules, usbfs memory, environment variables and OS permissions. This track adds setup guides for each OS, ships the setup files we can write ourselves (udev rules, usbfs config), and adds `el-devices setup` to check a machine and install those files. It then publishes everything, along with the hardware docs and API reference, as a MkDocs Material site on GitHub Pages.

## Motivation

The setup steps are spread across `README.md`, `docs/guide.md` and each hardware doc. Some are missing entirely, and they were written as research notes, not instructions. When a step is skipped, the failure is often silent:
- Aravis leaves out USB cameras it can't open.
- The BrainStem SDK reports "No USBHub3p hubs found." when it lacks permissions.
- pycairo fails with "can't find Python" when the headers are missing.

Bringing up one Fedora rig (2026-10-22/23) hit all three, plus the Aravis linker path. Every new rig, and every new person, pays this cost again.

Separately, the hardware docs are meant to be the lab's reference for every device. Markdown in the repo is hard to browse and search, and a site fixes that. This follows the `sample-mounts` pattern (MkDocs Material on GitHub Pages).

## User Story

As a lab developer or operator setting up a rig machine, I want one guide for my OS that tells me exactly what to install for the devices I use, plus a command that tells me what's still missing, so that a new machine works the first time without anyone digging through hardware notes or chat history.

## Acceptance Criteria

### Setup guides
- [ ] `docs/setup/` has one walkthrough per platform: macOS arm64, Fedora (x86_64 and Asahi arm64), Ubuntu LTS and Windows 10/11.
- [ ] Each walkthrough is organized by backend: Spinnaker, Aravis, GenTL (IDS peak and Spinnaker's producer), BrainStem, gphoto2, serial (MISHA, XLC, X-Carve) and BLE (Foldio360). Each section says what to install, which `el-devices` command proves it works, and what the failure looks like when a step is missing.
- [ ] `docs/setup/index.md` has a backend × OS matrix: supported or not, which Python versions, and when it was last checked on a machine.
- [ ] Install steps live only in the setup guides. The hardware docs keep the facts and the reasons, and link to the guides. `README.md` and `docs/guide.md` point to the guides instead of carrying their own install steps.
- [ ] Each walkthrough has been followed end to end on a real machine of that platform, and the date is recorded.

### Shipped setup files
- [ ] The package ships a udev rules file covering the lab's USB vendors: FLIR `1e10`, IDS `1409`, Acroname `24ff`, the other USB3 Vision vendors Aravis lists, and the lab's gphoto2 cameras. The rules are written by us, not copied from LGPL or vendor files.
- [ ] The package ships a `modprobe.d` file that raises `usbfs_memory_mb` to 1000.
- [ ] The files are package data, so they're available from a pip install without a checkout.

### `el-devices setup`
- [ ] `el-devices setup check [--backend NAME]` reports, per backend, what's present and what's missing. It covers:
  - Python package import and version, and whether the Python version is right for the backend.
  - Native library load (Aravis typelib and library, BrainStem).
  - GenTL producers found on `GENICAM_GENTL64_PATH`, and which of them load.
  - Attached USB devices from known vendors that the current user can't open.
  - The `usbfs_memory_mb` value.
  - Serial port group membership.
  - `ptpcamerad` running on macOS.

  Each problem names the guide section that fixes it. The command exits non-zero when a requested backend isn't ready.
- [ ] `el-devices setup install-rules` installs the shipped rules and usbfs files on Linux and reloads udev. Without root it prints the exact `sudo` command instead of escalating itself.
- [ ] `el-devices discover` mentions `el-devices setup check` when an attached device from a known vendor can't be opened, instead of leaving it silently out of the list.
- [ ] Check logic is tested against fake sysfs trees, environments and import failures. Nothing in the tests needs root or a real device.

### Site
- [ ] `mkdocs.yml` with the Material theme and this navigation: Home, Setup, Guide, Hardware (index and per-device docs), Decisions (ADRs), API reference.
- [ ] A `docs/index.md` home page, and a link from the README to the published site.
- [ ] An API reference generated with mkdocstrings (Google-style docstrings) for the public API.
- [ ] A `docs` dependency group (`mkdocs-material`, `mkdocstrings[python]`, `mkdocs<2`) that builds with pip, with no uv required.
- [ ] `.github/workflows/docs.yml` builds with `mkdocs build --strict` on PRs and deploys to GitHub Pages from `main`, using current (non-Node-20) action versions.
- [ ] `CLAUDE.md` documents:
  - how to build and preview the docs;
  - that a driver change isn't done until its setup steps are in the guides.

## Dependencies

- **usb3-vision-camera_20261021** (PR #12) must merge first. It holds the Aravis, GenTL and Fedora findings the guides are written from.
- **foundation_20261012**, for `docs/guide.md`, `docs/hardware/README.md` and the public API (complete).

## Out of Scope

- **Downloading or running vendor SDK installers** (Spinnaker, IDS peak). These need accounts and licence acceptance. The guides link to them, and `setup check` verifies the result.
- **Building or packaging Aravis.** The guides document the source build. They don't script it.
- **Windows USB driver rebinding** (Zadig, libusb on Windows).
- **Intel macOS.** It has no `genicam` wheel and no lab machine.
- **Versioned docs** (one set per release). Revisit when the package reaches PyPI.
- **Read the Docs hosting.**
- **Fixing the SDKs' silent failures inside the drivers.** `setup check` and the `discover` hint surface them; the drivers' error paths are unchanged.

## Technical Notes

- **Where the files live.** The files are package data, under `src/educelab/devices/setup/` or `src/educelab/devices/app/_files/` (decided in Phase 1). They must stay importable without any vendor SDK.
- **udev permission style.** `MODE="0666"` works over SSH and for services. `TAG+="uaccess"` works only for the logged-in seat, which rigs run headless may not have. The rigs' usage decides between them, or whether to use both, in Phase 1.
- **Detecting devices the user can't open.**
  - **Linux:** walk `/sys/bus/usb/devices/*/idVendor` and test access to the matching `/dev/bus/usb/BBB/DDD` node. That needs neither libusb nor root.
  - **macOS and Windows:** skip the check.
- **gphoto2 rules.** libgphoto2 can generate per-camera rules (`print-camera-list udev-rules`). A rule per vendor for the lab's bodies (Nikon `04b0`, later Canon `04a9`) may be simpler. Decide in Phase 1.
- **Risks.**
  - A strict build fails on broken links, so the hardware docs may need link fixes.
  - GitHub Pages must be enabled with "GitHub Actions" as its source. That's a maintainer action.
  - mkdocstrings needs the package to import, so vendor SDKs must stay lazily imported.
  - Checking the walkthroughs on four platforms needs the maintainer at each machine.

---

_Generated by Conductor. Review and edit as needed._
