"""JSON Schema validation for SparkWriter manifests."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator, FormatChecker


LOCKED_SCHEMA_VERSION = "1.6"
SUPPORTED_SCHEMA_VERSIONS = (LOCKED_SCHEMA_VERSION,)
SCHEMA_PATH = Path(__file__).with_name("schema") / "sparkplug_manifest.schema.json"


@lru_cache(maxsize=1)
def _manifest_validator() -> Draft7Validator:
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)

    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def validate_manifest_schema(manifest: dict[str, Any]) -> None:
    """Validate a manifest against the bundled locked JSON Schema."""

    errors = sorted(
        _manifest_validator().iter_errors(manifest),
        key=lambda error: list(error.absolute_path),
    )
    if not errors:
        return

    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path)
    if not location:
        location = "<root>"
    raise ValueError(f"{location}: {first.message}")
