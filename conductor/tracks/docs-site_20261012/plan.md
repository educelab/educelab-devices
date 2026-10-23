# Implementation Plan: Documentation Site and Machine Setup

**Track ID:** docs-site_20261012
**Spec:** [spec.md](./spec.md)
**Created:** 2026-10-12 (setup scope merged in 2026-10-23)
**Status:** [ ] Not Started

## Overview

First the setup steps scattered through the docs are collected into one inventory, and the open design questions are settled. Then the shipped files and `el-devices setup` are built test-first. The guides are written against them, and each is followed on a real machine. Last, the site is built and published, with the guides as a section. Starts after PR #12 (usb3-vision-camera_20261021) merges.

## Phase 1: Inventory and design

Collect every setup step that exists today and settle the spec's open questions.

### Tasks

- [ ] Task 1.1: Inventory the setup steps for each backend and OS from `README.md`, `docs/guide.md`, `conductor/tech-stack.md` and every `docs/hardware/*.md`. For each step, record what it is, why it's needed, how a missing step shows up, and whether it has been verified. Keep the inventory in the track as `setup-inventory.md`.
- [ ] Task 1.2: Draft the `docs/setup/` layout: an index with the backend × OS matrix, one page per platform, and a common shape for each backend's section.
- [ ] Task 1.3: Decide, with the maintainer:
  - where the shipped files live in the package;
  - udev `MODE="0666"` vs `TAG+="uaccess"`;
  - gphoto2 rules per vendor vs generated per model;
  - the full vendor ID list.

  Record the decisions in this plan.
- [ ] Task 1.4: Design the `el-devices setup` interface: the subcommands, what a check reports, the exit codes, and how a check names the guide section that fixes it.

### Verification

- [ ] The maintainer has reviewed the inventory, the layout and the decisions.

## Phase 2: Shipped setup files

### Tasks

- [ ] Task 2.1: Write tests that the rules file covers every listed vendor ID and parses as udev rules, and that the `modprobe.d` file sets `usbfs_memory_mb=1000`.
- [ ] Task 2.2: Add the udev rules and `modprobe.d` files as package data, and confirm they're in a built wheel.
- [ ] Task 2.3: Write tests for `el-devices setup install-rules`:
  - it writes the files and reloads udev as root;
  - without root it prints the `sudo` command and writes nothing;
  - it refuses on macOS and Windows.

  Use a fake filesystem root and a fake command runner.
- [ ] Task 2.4: Implement `install-rules`.

### Verification

- [ ] Suite, Ruff and format pass. `install-rules` works on a Linux machine, and the devices it covers open without root.

## Phase 3: `el-devices setup check`

### Tasks

- [ ] Task 3.1: Write tests for each check against fakes:
  - a fake import of each SDK, which succeeds, fails or comes with the wrong Python version;
  - Aravis typelib and library loading;
  - GenTL producers on `GENICAM_GENTL64_PATH`, including one that fails to load;
  - a fake sysfs tree with readable and unreadable devices from known vendors;
  - `usbfs_memory_mb`;
  - serial group membership;
  - `ptpcamerad` running on macOS.
- [ ] Task 3.2: Implement the checks and `setup check [--backend NAME]`, with the report and exit codes from Task 1.4.
- [ ] Task 3.3: Write tests that `el-devices discover` mentions `setup check` when a device from a known vendor can't be opened, and stays quiet when everything is readable. Then implement it.

### Verification

- [ ] Suite, Ruff and format pass. On the Fedora machine, `setup check` reports the missing rule before `install-rules` runs, and reports nothing missing after.

## Phase 4: Setup guides

### Tasks

- [ ] Task 4.1: Write `docs/setup/index.md`: the backend × OS matrix, which page to read, and `el-devices setup check` as the first step.
- [ ] Task 4.2: Write the macOS arm64 guide.
- [ ] Task 4.3: Write the Fedora guide, for x86_64 and Asahi arm64.
- [ ] Task 4.4: Write the Ubuntu LTS guide.
- [ ] Task 4.5: Write the Windows 10/11 guide.
- [ ] Task 4.6: Take the install steps out of the hardware docs, `README.md`, `docs/guide.md` and `conductor/tech-stack.md`, and link to the guides instead. The hardware docs keep the facts and the reasons.
- [ ] Task 4.7: Update `CLAUDE.md`: setup steps go in `docs/setup/`, and a driver change isn't done until its setup steps are there.

### Verification

- [ ] The maintainer has reviewed the guides. No install step is left outside `docs/setup/`.

## Phase 5: Platform walkthroughs

Manual, with the maintainer at each machine. Each run follows a guide end to end on a machine without that setup, ends with `el-devices setup check` clean and each backend's `el-devices` check passing, and puts a last-checked date in the matrix. Problems found are fixed in the guide, or in `setup check` when it missed them.

### Tasks

- [ ] Task 5.1: Walk through the macOS arm64 guide.
- [ ] Task 5.2: Walk through the Fedora guide (x86_64 and/or Asahi arm64).
- [ ] Task 5.3: Walk through the Ubuntu LTS guide.
- [ ] Task 5.4: Walk through the Windows 10/11 guide.

### Verification

- [ ] Every row of the matrix has a last-checked date or is marked unchecked, with the reason.

## Phase 6: Site

### Tasks

- [ ] Task 6.1: Add the `docs` dependency group.
- [ ] Task 6.2: Write `mkdocs.yml` (Material theme, navigation with Setup, mkdocstrings) and `docs/index.md`.
- [ ] Task 6.3: Add API reference pages for each subpackage.
- [ ] Task 6.4: Fix any strict-build warnings in the existing docs.

### Verification

- [ ] `mkdocs build --strict` passes locally, and the maintainer has reviewed `mkdocs serve`.

## Phase 7: Publish

### Tasks

- [ ] Task 7.1: Write `.github/workflows/docs.yml`: build on PRs, deploy from `main`.
- [ ] Task 7.2: The maintainer enables GitHub Pages with the Actions source.
- [ ] Task 7.3: Link the site from `README.md`, and document the docs commands in `CLAUDE.md`.

### Verification

- [ ] The site is live and the deploy job is green.

## Final Verification

- [ ] All acceptance criteria met
- [ ] Tests passing
- [ ] Documentation updated
- [ ] Ready for review

---

_Generated by Conductor. Tasks will be marked [~] in progress and [x] complete._
