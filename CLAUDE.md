# CLAUDE.md

## Project

`lxd-indicator.py` is a GTK tray indicator for LXD, packaged as a snap
(`snap/snapcraft.yaml`, core24 / Python 3.12). Unit tests live in `tests/`.

## Formatting: black

All Python code must be formatted with [black](https://black.readthedocs.io/).
CI (`.github/workflows/main.yml`, job `black`) runs `black --check` and fails
on any unformatted file. Configuration is in `pyproject.toml`.

Before committing any Python change, run:

```bash
pip install 'black~=26.0'
black .
black --check .
```

## Tests

```bash
pip install -r tests/requirements.txt
python -m unittest discover -s tests -v
```
