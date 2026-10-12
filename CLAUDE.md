# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`educelab-devices` is the basis for all hardware-interfacing projects at EduceLab. It consolidates the Python APIs for the lab's imaging hardware (cameras, lights, turntables, gantries, USB hubs) that were previously duplicated across projects such as `pgs-scan`. It installs as `educelab.devices` in the `educelab` namespace package. Licensed under AGPLv3.

Device APIs must stay simple and low-dependency so any consumer can wrap them (e.g. `pgs-scan` wraps devices in `QObject`s for signals/slots). Don't add Qt or other framework dependencies here; see `docs/adr/0001-framework-free-blocking-device-apis.md`.

Domain vocabulary lives in `CONTEXT.md` and design decisions in `docs/adr/`. Use the glossary's terms in code and docs.

## Commands

Both pip and uv are supported. Don't introduce anything that requires uv.

```bash
# pip (25.1+ for --group)
pip install -e . --group dev
pytest                         # all tests
pytest tests/test_version.py::test_version   # single test
ruff check . && ruff format .

# uv equivalents
uv run pytest
uv run ruff check . && uv run ruff format .

uv build                       # or: python -m build
```

## Architecture and conventions

- **Namespace package:** `src/educelab/` must stay empty: no files, and no `__init__.py`. That is what lets several `educelab.*` distributions share the namespace. Packages are discovered with `namespaces = true` in `[tool.setuptools.packages.find]`, so subpackages such as `app/` work without an `__init__.py`.
- **Package layout:** Everything lives in `src/educelab/devices/`, organized into subpackages by hardware type (e.g. `educelab.devices.cameras`, `educelab.devices.lights`). Never organize by vendor or consuming project at the top level.
- **Packaging:** All metadata lives in `pyproject.toml` (PEP 621, setuptools backend). There is no `setup.cfg` or `requirements.txt`.
- **Dependencies:** Runtime dependencies go in `[project] dependencies`. Dev tools go in `[dependency-groups] dev` (PEP 735), which both pip and uv read.
- **Versioning:** The version is set statically in `pyproject.toml`. `__version__` reads it back through `importlib.metadata`.
- **Vendor dependencies:** Import vendor SDKs (PySpin, gphoto2, bleak, pyserial, brainstem) lazily inside their backend module, so importing any subpackage works without them. Declare each PyPI-hosted SDK as an optional extra named for its backend (`[gphoto2]`, `[foldio360]`, `[serial]`, `[acroname]`, `[all]`); PySpin is not on PyPI and is installed manually.
- **CLI:** There is a single console script, `el-devices`, with one subcommand per manual hardware check (e.g. `el-devices foldio`). Its code lives in `src/educelab/devices/app/`. Don't register additional scripts; add subcommands instead.
- **Hardware docs:** Everything known about a piece of hardware goes in `docs/hardware/<model>.md`. A driver change isn't done until its hardware doc reflects what was learned.
- **Tests:** Tests run under pytest with `--import-mode=importlib`, so `tests/` needs no `__init__.py`. Tests import from the installed package, so do an editable install first. Each role has a contract suite in `tests/contracts/` (importable as `contracts` through pytest's `pythonpath`). Every driver's tests subclass the contract for each role the driver offers, and so does every simulated device's. `docs/guide.md` describes the behavior every device must have; update it when a role or the `Device` base changes in a way callers could notice.
- **Typing:** The package ships a `py.typed` marker, included through `[tool.setuptools.package-data]`.
- **CI:** `.github/workflows/tests.yml` runs Ruff and an advisory mypy check, then pytest on Linux, macOS, and Windows for every supported Python version. CI uses pip rather than uv on purpose, so it also confirms the project doesn't require uv. When you change the supported Python versions, update the matrix along with `requires-python` and the classifiers.
- **Python:** Requires 3.11 or newer. PySpin (Spinnaker 4.x) only ships cp310 and cp312 wheels, so the Spinnaker driver works on 3.12 only; other drivers support every version in the CI matrix.
