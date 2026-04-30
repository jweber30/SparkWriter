"""Well-known profile field identifiers for SparkPlug manifests."""

from __future__ import annotations

from typing import Final

STANDARD_FIELDS: Final[set[str]] = {
    "user.name",
    "user.email",
    "user.ssh_public_keys",
    "network.hostname",
    "network.wifi.ssid",
    "network.wifi.password",
    "locale.timezone",
    "locale.keyboard",
}


def is_standard_field(value: str | None) -> bool:
    """Return True when a manifest field uses a known semantic identifier."""

    return bool(value and value in STANDARD_FIELDS)
