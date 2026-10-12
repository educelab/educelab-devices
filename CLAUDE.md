# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a template for Python modules in the `educelab` namespace package. Each module built from it installs as `educelab.<name>` alongside other EduceLab modules. Licensed under AGPLv3.

## Commands

Both pip and uv are supported. Don't introduce anything that requires uv.

```bash
# pip (25.1+ for --group)
pip install -e . --group dev
pytest                         # all tests
pytest tests/test_main.py::test_main   # single test
ruff check . && ruff format .

# uv equivalents
uv run pytest
uv run ruff check . && uv run ruff format .

uv build                       # or: python -m build
```

## Architecture and conventions

- **Namespace package:** `src/educelab/` must stay empty: no files, and no `__init__.py`. That is what lets several `educelab.*` distributions share the namespace. Packages are discovered with `namespaces = true` in `[tool.setuptools.packages.find]`, so subpackages such as `app/` work without an `__init__.py`.
- **Module code:** Everything lives in `src/educelab/<module>/`. The placeholder name is `module`. To rename it, update the directory name, the `[project.scripts]` paths, and the distribution name passed to `version()` in `__init__.py`.
- **Packaging:** All metadata lives in `pyproject.toml` (PEP 621, setuptools backend). There is no `setup.cfg` or `requirements.txt`.
- **Dependencies:** Runtime dependencies go in `[project] dependencies`. Dev tools go in `[dependency-groups] dev` (PEP 735), which both pip and uv read.
- **Versioning:** The version is set statically in `pyproject.toml`. `__version__` reads it back through `importlib.metadata`.
- **CLI apps:** Put them in `src/educelab/<module>/app/` with a `main()` function, and register each one under `[project.scripts]`. Use the `el-` prefix for script names.
- **Tests:** Tests run under pytest with `--import-mode=importlib`, so `tests/` needs no `__init__.py`. Tests import from the installed package, so do an editable install first.
- **Typing:** The module ships a `py.typed` marker, included through `[tool.setuptools.package-data]`. Keep that entry if you rename the module.
- **CI:** `.github/workflows/tests.yml` runs Ruff, then pytest on Linux, macOS, and Windows for every supported Python version. CI uses pip rather than uv on purpose, so it also confirms the project doesn't require uv. When you change the supported Python versions, update the matrix along with `requires-python` and the classifiers.
- **Python:** Requires 3.11 or newer.
