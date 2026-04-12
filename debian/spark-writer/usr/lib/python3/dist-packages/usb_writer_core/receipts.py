"""Receipt generation utilities for SparkWriter.

Provides helpers to canonicalize receipt payloads, compute content hashes,
produce keyed fingerprints, and sign receipts using Ed25519 keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

try:
    from nacl.signing import SigningKey  # type: ignore
except ImportError:  # pragma: no cover - handled in load_signing_key
    SigningKey = None  # type: ignore


class ReceiptError(Exception):
    """Base exception for receipt helpers."""


class ReceiptSigningError(ReceiptError):
    """Raised when receipt signing fails."""


def canonicalize_receipt(payload: Dict[str, Any]) -> str:
    """Return canonical JSON (sorted keys, no whitespace)."""

    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def compute_receipt_hash(canonical_json: str, algorithm: str = "sha256") -> str:
    """Compute a content hash for the canonical receipt JSON."""

    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:  # pragma: no cover - validated via tests
        raise ReceiptError(f"Unsupported hash algorithm: {algorithm}") from exc
    digest.update(canonical_json.encode("utf-8"))
    return digest.hexdigest()


def _encode_bytes(data: bytes, encoding: str) -> str:
    if encoding == "hex":
        return data.hex()
    if encoding in {"base64", "b64"}:
        return base64.b64encode(data).decode("ascii")
    if encoding in {"base64url", "b64url"}:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")
    raise ReceiptError(f"Unsupported encoding: {encoding}")


def _decode_private_key(key_material: str) -> bytes:
    candidate = key_material.strip()
    if candidate.startswith("ed25519:"):
        candidate = candidate.split(":", 1)[1]

    # Try strict base64 first
    try:
        key_bytes = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError):
        try:
            key_bytes = bytes.fromhex(candidate)
        except ValueError as exc:
            raise ReceiptSigningError("Invalid Ed25519 private key encoding") from exc

    if len(key_bytes) == 64:
        key_bytes = key_bytes[:32]

    if len(key_bytes) != 32:
        raise ReceiptSigningError("Ed25519 private key must be 32 or 64 bytes")

    return key_bytes


def load_signing_key(private_key: str) -> SigningKey:
    """Load an Ed25519 signing key from a base64 or hex string."""

    if SigningKey is None:  # pragma: no cover - depends on runtime environment
        raise ReceiptSigningError("PyNaCl is required for receipt signing (pip install pynacl)")

    key_bytes = _decode_private_key(private_key)
    return SigningKey(key_bytes)


def encode_public_key(signing_key: SigningKey, encoding: str = "base64") -> str:
    """Return encoded public key for the provided signing key."""

    return _encode_bytes(signing_key.verify_key.encode(), encoding)


def sign_with_key(
    signing_key: SigningKey,
    canonical_json: str,
    *,
    encoding: str = "base64",
) -> str:
    """Sign canonical receipt JSON with Ed25519 and encode the signature."""

    signature = signing_key.sign(canonical_json.encode("utf-8")).signature
    return _encode_bytes(signature, encoding)


def generate_nonce(byte_length: int = 16, encoding: str = "hex") -> str:
    """Generate a random nonce value."""

    data = secrets.token_bytes(byte_length)
    return _encode_bytes(data, encoding)


def current_timestamp() -> str:
    """Return current UTC timestamp in RFC3339 format."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hmac_fingerprint(
    key_material: str,
    value: str,
    *,
    algorithm: str = "sha256",
    encoding: str = "hex",
) -> str:
    """Compute keyed fingerprint for the provided value."""

    try:
        digestmod = getattr(hashlib, algorithm)
    except AttributeError as exc:  # pragma: no cover - validated in tests
        raise ReceiptError(f"Unsupported HMAC algorithm: {algorithm}") from exc

    mac = hmac.new(key_material.encode("utf-8"), str(value).encode("utf-8"), digestmod)
    return _encode_bytes(mac.digest(), encoding)