"""Host-owned receipt assembly for Source + SparkPlug workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from usb_writer_core.receipts import current_timestamp

from .sources import Source


def _hash_file(path: Path, algorithm: str = "sha256") -> Optional[str]:
    import hashlib

    if not path.exists():
        return None

    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return f"{algorithm}:{digest.hexdigest()}"


def _sparkplug_identity(plugin: Any) -> Dict[str, Any]:
    metadata = getattr(plugin, "manifest", {}).get("metadata", {})
    payload: Dict[str, Any] = {
        "id": getattr(plugin, "plugin_id", getattr(plugin, "name", "unknown")),
        "name": getattr(plugin, "name", "Unknown SparkPlug"),
    }
    if metadata.get("version"):
        payload["version"] = metadata["version"]
    return payload


def build_receipt_payload(
    *,
    source: Source,
    sparkplugs: Iterable[Any],
    spark_writer_version: str = "unknown",
    original_iso_path: Optional[str] = None,
    processed_iso_path: Optional[str] = None,
    device_info: Optional[Dict[str, Any]] = None,
    observed_environment: Optional[Dict[str, Any]] = None,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the host-owned receipt payload for one run."""

    source_section: Dict[str, Any] = {
        "id": source.id,
        "name": source.name,
        "family": source.family,
        "acquire": {"url": source.url},
        "verification": {"sha256": source.sha256},
    }
    if source.version:
        source_section["version"] = source.version
    if source.acquire_kind:
        source_section["acquire"]["kind"] = source.acquire_kind
    if source.installer_scheme:
        source_section["installer_scheme"] = source.installer_scheme
    if source.capabilities:
        source_section["capabilities"] = list(source.capabilities)

    final_artifacts: Dict[str, Any] = {}
    if original_iso_path:
        original_hash = _hash_file(Path(original_iso_path).expanduser())
        if original_hash:
            final_artifacts["original_iso_sha256"] = original_hash
    if processed_iso_path:
        processed_hash = _hash_file(Path(processed_iso_path).expanduser())
        if processed_hash:
            final_artifacts["processed_iso_sha256"] = processed_hash

    device_write: Dict[str, Any] = {}
    if device_info:
        for key in ("path", "model", "serial", "size"):
            if device_info.get(key):
                device_write[key] = device_info[key]
    if started_at:
        device_write["started_at"] = started_at
    if completed_at:
        device_write["completed_at"] = completed_at

    payload: Dict[str, Any] = {
        "identity": {
            "receipt_format_version": "1.0",
            "spark_writer_version": spark_writer_version,
            "generated_at": completed_at or current_timestamp(),
        },
        "source": source_section,
        "sparkplugs": [_sparkplug_identity(plugin) for plugin in sparkplugs],
    }
    if final_artifacts:
        payload["final_artifacts"] = final_artifacts
    if device_write:
        payload["device_write"] = device_write
    if observed_environment:
        payload["observed_environment"] = observed_environment
    return payload
