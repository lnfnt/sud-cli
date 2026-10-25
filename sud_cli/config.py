"""Application state directory configuration."""

import os
from pathlib import Path


ENV_VAR = "SUD_CLI_STATE_DIR"
DEFAULT_DIR_NAME = "sud-cli"


def get_state_dir() -> Path:
    """Return the sud-cli state directory.

    Resolution order:
      1. SUD_CLI_STATE_DIR environment variable.
      2. ~/.local/share/sud-cli
    """
    if ENV_VAR in os.environ:
        return Path(os.environ[ENV_VAR]).expanduser().resolve()
    return Path.home() / ".local" / "share" / DEFAULT_DIR_NAME
