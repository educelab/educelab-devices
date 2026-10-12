# EduceLab Python Module

`educelab-pymodule` is a template project for creating Python modules in the 
`educelab` namespace.

# Getting started
1. Rename the `module` directory in `src/educelab` to your module's name.
2. Update `pyproject.toml`, `LICENSE`, and `NOTICE` with your project's 
   metadata. In `pyproject.toml`, also update the `[project.scripts]` entry 
   point paths.
3. Update the distribution name passed to `version()` in your module's 
   `__init__.py`.

# Development
Either pip or [uv](https://docs.astral.sh/uv/) can be used.

With pip (25.1+):
```shell
python -m venv .venv
source .venv/bin/activate
pip install -e . --group dev
pytest
ruff check . && ruff format .
```

With uv:
```shell
uv run pytest
uv run ruff check . && uv run ruff format .
```

# Project organization
- All module files and apps go in `src/educelab/[MODULE NAME]`.
- `src/educelab` must not contain any files, including `__init__.py` files.
- Add runtime dependencies to `dependencies` in `pyproject.toml` and 
  development tools to `[dependency-groups]`.
- Tests go in `tests/`.
