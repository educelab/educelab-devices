# Tech Stack

## Language and packaging

- Python >=3.11. CI tests 3.11–3.14 on Ubuntu, macOS, and Windows. The Spinnaker driver works on 3.12 only: as of Spinnaker SDK 4.4.0 (the oldest the driver accepts), PySpin ships cp310 and cp312 wheels only, and the cp312 wheel requires numpy>=2.
- setuptools with all metadata in `pyproject.toml` (PEP 621). Namespace package: `src/educelab/` stays empty.
- pip and uv are both supported; nothing may require uv.

## Runtime dependencies

| Dependency | Scope | Notes |
|------------|-------|-------|
| numpy | core | Frames and preview images. `>=2`, which PySpin 4.4.0's cp312 wheel also requires |
| gphoto2, Pillow | `[gphoto2]` | gphoto2 has no Windows support; declared with `sys_platform != "win32"` |
| bleak | `[foldio360]` | BLE |
| pyserial | `[serial]` | MISHA, XLC, X-Carve |
| brainstem | `[acroname]` | Raises `ImportError` at import on unrecognized Linux distros |
| genicam | `[gentl]` | EMVA's GenICam binding, for GenTL producers. Wheels only (cp310–cp314), none for Intel macOS. GenICam licence, not OSI |
| — | `[all]` | All of the above |
| PySpin | manual | Not on PyPI. Install the Spinnaker SDK (4.4.0 or newer), then its bundled wheel; versions must match exactly |
| Aravis 0.8, PyGObject | manual | Aravis from Homebrew or apt; PyGObject (with pycairo first) builds against it. See `docs/hardware/usb3-vision.md` |

All vendor SDKs are imported lazily inside their backend module.

## Development tooling

- **pytest** with `--import-mode=importlib`; `@pytest.mark.hardware` tests are skipped unless opted in.
- **Ruff** check and format, blocking in CI.
- **pytest-cov**, reported, no threshold.
- **mypy**, advisory in CI.
- **MkDocs Material + mkdocstrings** in a `docs` dependency group.

## Infrastructure

- GitHub Actions: tests and lint (pip-based), docs build and deploy to GitHub Pages.
- Distribution: consumers install from git tags (`educelab-devices @ git+https://github.com/educelab/educelab-devices@vX.Y.Z`). 0.x semver until the API settles; PyPI later.

## Interface surface

- Library: `educelab.devices.{cameras, lights, stages, hubs}`, one module per backend.
- CLI: one console script, `el-devices`, with a subcommand per manual hardware check.
