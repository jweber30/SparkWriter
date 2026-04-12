"""Helpers for manifest-referenced sidecar assets.

Phase-1 policy:
- Only template sidecars (templates.<name>.file) are considered.
- Sidecar references must be relative paths (no absolute paths/URLs/traversal).
- Resolved sidecar URLs must stay on the manifest origin and directory subtree.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse


def normalize_sidecar_ref(file_ref: str) -> str:
    """Validate and normalize a template sidecar reference.

    Returns a normalized POSIX-style relative path suitable for URL resolution.
    """
    if not isinstance(file_ref, str):
        raise ValueError("Template file reference must be a string")

    ref = file_ref.strip()
    if not ref:
        raise ValueError("Template file reference cannot be empty")

    if "\\" in ref:
        raise ValueError("Template file reference must use '/' path separators")

    parsed = urlparse(ref)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"Template file reference must be relative: '{ref}'")

    rel_path = PurePosixPath(ref)
    if rel_path.is_absolute():
        raise ValueError(f"Template file reference cannot be absolute: '{ref}'")

    if any(part == ".." for part in rel_path.parts):
        raise ValueError(f"Template file reference cannot traverse parent directories: '{ref}'")

    normalized = str(rel_path)
    if normalized in {"", "."}:
        raise ValueError("Template file reference cannot resolve to current directory")

    return normalized


def discover_template_sidecars(manifest: dict[str, Any]) -> list[str]:
    """Collect normalized template sidecar refs from manifest templates."""
    templates = manifest.get("templates", {})
    if not isinstance(templates, dict):
        return []

    assets = manifest.get("assets", {})
    if assets is None:
        assets = {}
    if not isinstance(assets, dict):
        raise ValueError("Manifest assets section must be an object")

    discovered: list[str] = []
    seen: set[str] = set()
    for _template_name, template_value in templates.items():
        if not isinstance(template_value, dict):
            continue
        if "file" in template_value:
            normalized = normalize_sidecar_ref(template_value["file"])
        elif "asset" in template_value:
            normalized = resolve_asset_path(assets, template_value["asset"])
        else:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        discovered.append(normalized)

    return discovered


def resolve_asset_path(assets: dict[str, Any], asset_name: str) -> str:
    """Resolve an asset name from manifest assets into a normalized relative path."""
    if not isinstance(asset_name, str) or not asset_name.strip():
        raise ValueError("Template asset reference must be a non-empty string")

    asset_spec = assets.get(asset_name)
    if not isinstance(asset_spec, dict):
        raise ValueError(f"Unknown asset reference: '{asset_name}'")

    path_value = asset_spec.get("path")
    if path_value is None:
        raise ValueError(f"Asset '{asset_name}' is missing required path")

    return normalize_sidecar_ref(path_value)


def resolve_sidecar_url(manifest_url: str, sidecar_ref: str) -> str:
    """Resolve sidecar_ref against manifest_url with strict origin/path policy."""
    normalized_ref = normalize_sidecar_ref(sidecar_ref)

    manifest_parsed = urlparse(manifest_url)
    manifest_dir = manifest_parsed.path.rsplit("/", 1)[0] + "/"

    resolved = urljoin(manifest_url, normalized_ref)
    resolved_parsed = urlparse(resolved)

    if (
        resolved_parsed.scheme != manifest_parsed.scheme
        or resolved_parsed.netloc != manifest_parsed.netloc
    ):
        raise ValueError(
            f"Template sidecar '{normalized_ref}' resolves outside manifest origin"
        )

    if not resolved_parsed.path.startswith(manifest_dir):
        raise ValueError(
            f"Template sidecar '{normalized_ref}' resolves outside manifest directory subtree"
        )

    return resolved