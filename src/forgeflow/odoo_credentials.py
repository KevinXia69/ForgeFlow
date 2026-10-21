"""Locate and read the Odoo API key without hard-coding one path.

The benchmark image writes the admin key to ``/etc/odoo/api_key``. The
permission profile gives the agent a read-only key instead and points
``FORGEFLOW_ODOO_KEY_FILE`` at it, while the root verifier and executor keep
using the admin path. One helper keeps every reader consistent and makes the
choice visible in the environment rather than in five duplicated literals.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_KEY_PATH = "/etc/odoo/api_key"
ENV_NAME = "FORGEFLOW_ODOO_KEY_FILE"


def key_path() -> Path:
    """Return the configured key file, defaulting to the benchmark admin key."""
    value = os.environ.get(ENV_NAME)
    return Path(value) if value else Path(DEFAULT_KEY_PATH)


def read_key() -> str:
    """Read the key, stripping one trailing newline and nothing else."""
    return key_path().read_text(encoding="utf-8").strip()
