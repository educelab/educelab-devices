# Product Definition

## educelab-devices

Python APIs for EduceLab's imaging hardware (cameras, lights, stages, USB hubs), installed as `educelab.devices`. This package is the basis for every hardware-interfacing project in the lab.

## Problem

Device code has been written separately in each project, coupled to that project's framework: Qt signals and `QThread`s in `pgs-scan` (`dev`), tkinter and daemon threads in the spectral prototype (`pgs-scan` `spectral-processing-module`). The copies drift, each carries its own bugs (`pgs-scan` #71, #83, #84, #90), and what the lab has learned about the hardware is scattered across docstrings, chat history, and side repositories.

## Users

- **Primary:** lab developers building acquisition applications. `pgs-scan` is the first consumer; future rigs follow.
- **Secondary:** operators at a rig who need to check that a device is alive, using the `el-devices` CLI.

## Key goals

1. One blocking, framework-free API per hardware type that any consumer can wrap (ADR 0001).
2. Port the spectral devices first (MISHA lights, Foldio360 turntable and halo, gphoto2 DSLR with focus drive), then the `pgs-scan` `dev` devices (XLC lights, X-Carve, Spinnaker), then the Acroname USB hub.
3. Simulated devices and a shared contract test suite, so consumers develop without hardware and simulations can't drift from real drivers.
4. Hardware reference docs that record everything known about each device, published with the API docs on GitHub Pages.
5. `pgs-scan` migrated onto the package as its first full consumer.

## Roadmap

Planned tracks, in order. Create each with `/conductor:new-track` when it starts.

| # | Track | Notes |
|---|-------|-------|
| 1 | Foundation | `Device` base, role ABCs (Camera, Light, Stage, Hub, Focus drive), errors, discovery types, contract suite, simulated devices, `el-devices` skeleton, mypy/coverage wiring |
| 2 | MISHA lights | Serial driver; firmware moved in from `usb-dslr-capture-demo/misha/MISHA` |
| 3 | Foldio360 | Stage + halo light; client, protocol docs, and tests moved in from `~/source/foldio360`, which is then archived |
| 4 | gphoto2 camera | Camera files, settings, live view, keep-alive, recovery, focus drive (Nikon Z7 II first; Canon later) |
| 5 | XLC lights and X-Carve | Serial drivers from `pgs-scan` `dev`; firmware moved in; fixes #71 and #84 |
| 6 | Spinnaker camera | `SpinnakerSystem` + per-camera driver; fixes #83 and #90 |
| 7 | Acroname hub | `brainstem`-based Hub |
| — | Docs site | MkDocs Material + mkdocstrings on GitHub Pages; can run alongside any track |

## Out of scope

- Qt (or any framework) wrappers; these stay in consumers until two consumers need the same one.
- Rigs, capture sequencing, and workflow-level recovery; these belong to consumers.
- Persisting anything (addresses, origins, settings).
- Decoding raw camera files.
