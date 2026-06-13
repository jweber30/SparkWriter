"""Filesystem helpers for installing downloaded manifest plugins."""

import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def create_plugin_stage_dir(destination_dir: str, plugin_id: str) -> str:
    """Create private staging beside installed plugins for atomic replacement."""
    Path(destination_dir).mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(
        prefix=f".spark-plugin-{plugin_id}-",
        dir=destination_dir,
    )


def record_install_origin(manifest: dict[str, Any], manifest_url: str) -> None:
    """Record a display-safe URL in the locally installed manifest snapshot."""
    parsed = urlsplit(str(manifest_url or "").strip())
    sanitized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if not sanitized:
        return

    metadata = manifest.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["installed_from"] = sanitized
