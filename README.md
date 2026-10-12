# EduceLab Devices

`educelab-devices` provides Python APIs for the imaging hardware used across 
EduceLab projects: cameras, lights, turntables, gantries, and similar devices.
Device APIs are organized by hardware type (e.g. `educelab.devices.cameras`, 
`educelab.devices.lights`) and are kept as simple and dependency-light as 
possible so they can be wrapped by any consumer (CLI scripts, Qt applications, 
etc.).

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
- All package code goes in `src/educelab/devices`, grouped into subpackages by
  hardware type.
- `src/educelab` must not contain any files, including `__init__.py` files.
- Add runtime dependencies to `dependencies` in `pyproject.toml` and 
  development tools to `[dependency-groups]`.
- Tests go in `tests/`.
