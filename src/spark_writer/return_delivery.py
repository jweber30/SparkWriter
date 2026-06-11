"""Best-effort post-write return delivery for SparkPlug outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

import requests


@dataclass(frozen=True)
class ReturnDeliveryResult:
    success: bool
    message: str
    status_code: Optional[int] = None


def is_secure_return_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme == "https" and bool(parsed.netloc):
        return True
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def is_https_url(url: str) -> bool:
    return is_secure_return_url(url)


def build_return_delivery_payload(
    *,
    sparkplugs: Iterable[Dict[str, Any]],
    secrets: Dict[str, Dict[str, str]],
    receipt: Optional[Dict[str, Any]],
    source: Optional[Dict[str, Any]],
    device: Optional[Dict[str, Any]],
    generated_at: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "return_format_version": "1.0",
        "generated_at": generated_at,
        "sparkplugs": list(sparkplugs),
        "secrets": secrets,
    }
    if receipt:
        payload["receipt"] = receipt
    if source:
        payload["source"] = source
    if device:
        payload["device"] = device
    return payload


def deliver_return_payload(
    *,
    endpoint_url: str,
    payload: Dict[str, Any],
    bearer_token: str = "",
    timeout: tuple[float, float] = (5.0, 15.0),
) -> ReturnDeliveryResult:
    endpoint_url = str(endpoint_url or "").strip()
    if not is_secure_return_url(endpoint_url):
        raise ValueError("Return delivery endpoint must be HTTPS or localhost HTTP")

    headers = {"Content-Type": "application/json"}
    token = str(bearer_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.post(
            endpoint_url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return ReturnDeliveryResult(False, f"Return delivery failed: {exc}")

    if 200 <= response.status_code < 300:
        return ReturnDeliveryResult(
            True,
            "Return delivery succeeded",
            status_code=response.status_code,
        )

    return ReturnDeliveryResult(
        False,
        f"Return delivery failed with HTTP {response.status_code}",
        status_code=response.status_code,
    )
