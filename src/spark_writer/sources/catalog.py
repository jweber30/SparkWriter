"""Installation Source normalization and local manifest discovery."""

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
    sparkplug_name: Optional[str] = None
    manifest_origin: Optional[str] = None
    can_write_usb: bool = True
    can_export_iso: bool = True

    @property
    def display_label(self) -> str:
        """Identify the owning manifest in Source selectors."""
        if self.manifest_origin:
            manifest_name = self.sparkplug_name or self.sparkplug_id or "Installed manifest"
            return f"{manifest_name} - {self.manifest_origin}"
        if self.sparkplug_name:
            return f"{self.sparkplug_name} - {self.name}"
        return self.name

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
        sparkplug_name = str(raw.get("sparkplug_name", "")).strip() or None
        manifest_origin = str(raw.get("manifest_origin", "")).strip() or None

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
            sparkplug_name=sparkplug_name,
            manifest_origin=manifest_origin,
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
        if self.sparkplug_name:
            payload["sparkplug_name"] = self.sparkplug_name
        if self.manifest_origin:
            payload["manifest_origin"] = self.manifest_origin
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
    """Load Sources from the locally installed manifest directory."""

    def __init__(self, installed_dir: Optional[Path] = None) -> None:
        self._installed_dir = installed_dir or (
            Path(__file__).resolve().parents[1] / "plugins" / "installed"
        )

    def list_sources(self) -> List[Source]:
        if not self._installed_dir.is_dir():
            return []

        sources: List[Source] = []
        for manifest_path in sorted(self._installed_dir.glob("*.json")):
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)

            if not isinstance(manifest, dict):
                continue

            records = self._manifest_sources(manifest)
            sources.extend(Source.from_dict(record) for record in records)

        return sorted(sources, key=lambda source: (source.name.lower(), source.id))

    @staticmethod
    def _manifest_sources(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
        metadata = manifest.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        owner_id = str(metadata.get("id") or metadata.get("name") or "").strip()

        outputs = manifest.get("outputs", {})
        if not isinstance(outputs, dict):
            outputs = {}

        source = manifest.get("source")
        if isinstance(source, dict) and source.get("id"):
            normalized = dict(source)
            normalized.setdefault("sparkplug_id", owner_id)
            normalized.setdefault("outputs", outputs)
            return [normalized]

        records: List[Dict[str, Any]] = []
        presets = manifest.get("presets", [])
        if not isinstance(presets, list):
            return records

        for preset in presets:
            if not isinstance(preset, dict) or not preset.get("id"):
                continue

            preset_metadata = preset.get("metadata", {})
            if not isinstance(preset_metadata, dict):
                preset_metadata = {}

            records.append(
                {
                    "id": preset["id"],
                    "name": preset.get("name", preset["id"]),
                    "url": preset.get("url", ""),
                    "family": preset.get("family") or preset.get("distro", ""),
                    "version": preset.get("version", ""),
                    "sha256": preset.get("sha256", ""),
                    "installer_scheme": preset.get("installer_scheme")
                    or preset_metadata.get("installer_scheme", ""),
                    "capabilities": preset.get("capabilities")
                    or preset_metadata.get("capabilities", []),
                    "sparkplug_id": owner_id,
                    "outputs": outputs,
                }
            )

        return records
