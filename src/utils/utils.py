"""Small helpers shared across the narration package."""
from __future__ import annotations

import os
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


def load_env(env_path: str) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no dependency).

    Existing environment variables win, so a shell export overrides the file.

    Args:
        env_path (str): path to the .env file (ignored if it doesn't exist).
    """
    path = Path(env_path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
