"""Config file loading (TOML or JSON). Keys are the long option names, e.g. max-edge or max_edge."""

from __future__ import annotations

import json
from pathlib import Path


class ConfigError(Exception):
    pass


def _load_toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ConfigError("Reading TOML config files needs Python 3.11+ "
                              "or `pip install tomli` (or use a .json config).")
    return tomllib.loads(text)


def load_config(path: Path) -> dict:
    """Return the config as a dict with keys normalised to snake_case."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}")
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else _load_toml(text)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001 - parse errors differ per parser
        raise ConfigError(f"Config file {path} is not valid: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain key/value settings.")
    return {str(k).strip().lower().replace("-", "_"): v for k, v in data.items()}
