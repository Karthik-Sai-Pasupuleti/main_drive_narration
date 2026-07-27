"""Small helpers shared across the narration package."""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib
    import tomli as tomllib


def load_config(config_path: str) -> dict:
    """Load a TOML config file and return it as a dict.

    Args:
        config_path (str): path to the TOML file.

    Returns:
        dict: the parsed configuration.
    """
    return tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
