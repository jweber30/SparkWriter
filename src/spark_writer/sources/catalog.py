"""Installation Source normalization.

The active SparkWriter catalog is the installed manifest set. This module keeps
the small runtime Source value object and a legacy JSON catalog loader used by
older tests/tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Source:
    """Normalized host-owned Source definition."""

    id: str
    name: str
    url: str
    family: str
    version: Optional[str] = None
    sha256: str = ""
    acquire_kind: Optional[str] = None
    acquire_artifact: Optional[str] = None
    installer_scheme: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    sparkplug_id: Optional[str] = None
    can_write_usb: bool = True
    can_export_iso: bool = True

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Source":
        source_id = str(raw.get("id", "")).strip()
        name = str(raw.get("name", "")).strip()
        url = str(raw.get("url") or raw.get("acquire", {}).get("url", "")).strip()
        family = str(raw.get("family") or raw.get("distro", "")).strip()
        version = str(raw.get("version", "")).strip() or None
        sha256 = str(raw.get("sha256") or raw.get("verify", {}).get("sha256", "")).strip()

        acquire = raw.get("acquire", {})
        acquire_kind = str(acquire.get("kind", "")).strip() or None
        acquire_artifact = str(acquire.get("artifact", "")).strip() or None
        installer_scheme = str(raw.get("installer_scheme", "")).strip() or None
        sparkplug_id = str(raw.get("sparkplug_id", "")).strip() or None

        outputs = raw.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}
        can_write_usb = bool(outputs.get("usb", raw.get("can_write_usb", True)))
        can_export_iso = bool(outputs.get("iso", raw.get("can_export_iso", True)))

        capabilities_raw = raw.get("capabilities", [])
        capabilities = [str(item).strip() for item in capabilities_raw if str(item).strip()]

        if not source_id:
            raise ValueError("Source id is required")
        if not name:
            raise ValueError(f"Source {source_id}: name is required")
        if not url:
            raise ValueError(f"Source {source_id}: url is required")
        if not family:
            raise ValueError(f"Source {source_id}: family is required")

        return cls(
            id=source_id,
            name=name,
            url=url,
            family=family,
            version=version,
            sha256=sha256,
            acquire_kind=acquire_kind,
            acquire_artifact=acquire_artifact,
            installer_scheme=installer_scheme,
            capabilities=capabilities,
            sparkplug_id=sparkplug_id,
            can_write_usb=can_write_usb,
            can_export_iso=can_export_iso,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return compatibility/runtime mapping for current plugin APIs."""

        payload: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "family": self.family,
            "distro": self.family,
            "sha256": self.sha256,
            "source_id": self.id,
            "source_name": self.name,
            "source_family": self.family,
            "source_url": self.url,
            "capabilities": list(self.capabilities),
            "source_capabilities": list(self.capabilities),
            "can_write_usb": self.can_write_usb,
            "can_export_iso": self.can_export_iso,
        }
        if self.sparkplug_id:
            payload["sparkplug_id"] = self.sparkplug_id
        if self.version:
            payload["version"] = self.version
            payload["source_version"] = self.version
        if self.acquire_kind:
            payload["acquire"] = {"url": self.url, "kind": self.acquire_kind}
            payload["source_acquire_kind"] = self.acquire_kind
            if self.acquire_artifact:
                payload["acquire"]["artifact"] = self.acquire_artifact
                payload["source_acquire_artifact"] = self.acquire_artifact
        if self.installer_scheme:
            payload["installer_scheme"] = self.installer_scheme
        return payload


class SourceCatalog:
    """Load host-owned Source records from built-in JSON."""

    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        self._catalog_path = catalog_path or Path(__file__).with_name("catalog.json")

    def list_sources(self) -> List[Source]:
        if not self._catalog_path.exists():
            return []

        with self._catalog_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        if isinstance(payload, dict):
            records = payload.get("sources", [])
        else:
            records = payload

        sources = [Source.from_dict(record) for record in records]
        return sorted(sources, key=lambda source: (source.name.lower(), source.id))
