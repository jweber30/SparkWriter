import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.return_delivery import (
    build_return_delivery_payload,
    deliver_return_payload,
    is_secure_return_url,
)


def test_is_secure_return_url_allows_https_and_localhost_http():
    assert is_secure_return_url("https://ops.example.com/return")
    assert is_secure_return_url("http://localhost:8765/return")
    assert is_secure_return_url("http://127.0.0.1/return")
    assert is_secure_return_url("http://[::1]/return")
    assert not is_secure_return_url("http://ops.example.com/return")
    assert not is_secure_return_url("https:///missing-host")


def test_build_return_delivery_payload_includes_receipt_context():
    payload = build_return_delivery_payload(
        sparkplugs=[{"id": "demo", "name": "Demo"}],
        secrets={"demo": {"admin_password": "secret"}},
        receipt={"identity": {"receipt_format_version": "1.0"}},
        source={"id": "ubuntu"},
        device={"path": "/dev/sdb"},
        generated_at="2026-06-10T00:00:00Z",
    )

    assert payload["return_format_version"] == "1.0"
    assert payload["sparkplugs"][0]["id"] == "demo"
    assert payload["secrets"]["demo"]["admin_password"] == "secret"
    assert payload["receipt"]["identity"]["receipt_format_version"] == "1.0"
    assert payload["source"]["id"] == "ubuntu"
    assert payload["device"]["path"] == "/dev/sdb"


@patch("spark_writer.return_delivery.requests.post")
def test_deliver_return_payload_posts_json_with_bearer_token(mock_post):
    mock_post.return_value = MagicMock(status_code=202)
    payload = {"secrets": {"demo": {"admin_password": "secret"}}}

    result = deliver_return_payload(
        endpoint_url="https://ops.example.com/return",
        payload=payload,
        bearer_token="token-123",
    )

    assert result.success is True
    mock_post.assert_called_once()
    kwargs = mock_post.call_args.kwargs
    assert kwargs["json"] == payload
    assert kwargs["headers"]["Authorization"] == "Bearer token-123"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_deliver_return_payload_rejects_non_https_url():
    with pytest.raises(ValueError):
        deliver_return_payload(
            endpoint_url="http://ops.example.com/return",
            payload={},
        )


@patch("spark_writer.return_delivery.requests.post")
def test_deliver_return_payload_allows_localhost_http(mock_post):
    mock_post.return_value = MagicMock(status_code=204)

    result = deliver_return_payload(
        endpoint_url="http://localhost:8765/return",
        payload={},
    )

    assert result.success is True
    assert mock_post.call_args.args[0] == "http://localhost:8765/return"


@patch("spark_writer.return_delivery.requests.post")
def test_deliver_return_payload_converts_network_error_to_warning_result(mock_post):
    mock_post.side_effect = requests.Timeout("timed out")

    result = deliver_return_payload(
        endpoint_url="https://ops.example.com/return",
        payload={},
    )

    assert result.success is False
    assert "timed out" in result.message


@patch("spark_writer.return_delivery.requests.post")
def test_deliver_return_payload_converts_http_error_to_warning_result(mock_post):
    mock_post.return_value = MagicMock(status_code=503)

    result = deliver_return_payload(
        endpoint_url="https://ops.example.com/return",
        payload={},
    )

    assert result.success is False
    assert result.status_code == 503
    assert "HTTP 503" in result.message
