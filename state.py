"""Where the plugin keeps its cached Doubao device identity.

The identity is an anonymous device registration, so it is runtime data rather
than user configuration: it lives in the plugin's data directory, next to the
other plugin-owned state, and survives plugin updates.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_ID = "doubao-ime"
IDENTITY_FILENAME = "doubao-ime-identity.json"


def data_dir() -> Path:
    """Return the plugin's data directory, creating it when needed."""
    directory = _host_data_dir() or _fallback_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def identity_path() -> Path:
    """Return the path of the cached device identity."""
    return data_dir() / IDENTITY_FILENAME


def _host_data_dir() -> Path | None:
    # Newer Hermes hands plugins a managed, profile-scoped directory. Older
    # releases have no such helper and use the documented layout below.
    try:
        from hermes_cli.plugins import plugin_data_dir  # type: ignore[attr-defined]
    except Exception:
        return None
    try:
        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        return None


def _fallback_data_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "plugin-data" / PLUGIN_ID
