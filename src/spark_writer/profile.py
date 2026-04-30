"""Local profile values used for wizard auto-fill."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


class ProfileStore:
    """Small JSON-backed profile store for well-known installer fields."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self._default_path()

    def _default_path(self) -> Path:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            root = Path(config_home)
        else:
            root = Path.home() / ".config"
        return root / "spark-writer" / "profile.json"

    def load_values(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        fields = payload.get("fields", payload)
        if not isinstance(fields, dict):
            return {}
        return dict(fields)

    def save_values(self, values: Dict[str, Any]) -> None:
        if not values:
            return

        existing = self.load_values()
        for key, value in values.items():
            if key and value not in (None, ""):
                existing[str(key)] = value

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump({"fields": existing}, handle, indent=2)
