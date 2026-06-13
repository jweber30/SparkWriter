"""Validation helpers for downloaded plugin manifests."""

import json
from typing import Any


def parse_downloaded_manifest(
    content: bytes,
    *,
    content_type: str | None = None,
    final_url: str | None = None,
) -> dict[str, Any]:
    """Parse a downloaded manifest and provide actionable format errors."""
    location = f"\n\nFinal URL: {final_url}" if final_url else ""
    media_type = (content_type or "unknown").split(";", 1)[0].strip()

    if not content.strip():
        raise ValueError(
            "The manifest URL returned an empty response. "
            "Check that the link is correct and publicly accessible."
            + location
        )

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"The manifest response is not UTF-8 JSON "
            f"(Content-Type: {media_type})."
            + location
        ) from exc

    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        if text.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise ValueError(
                "The manifest URL returned an HTML page instead of JSON. "
                "The link may require sign-in or browser-session authentication; "
                "use a publicly accessible or token-authenticated manifest URL."
                + location
            ) from exc

        raise ValueError(
            f"The manifest response is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}; Content-Type: {media_type})."
            + location
        ) from exc

    if not isinstance(manifest, dict):
        raise ValueError("The manifest JSON must contain an object at its top level." + location)

    return manifest
