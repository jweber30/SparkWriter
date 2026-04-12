import base64
import json
import sys
import unittest
from pathlib import Path

import pytest

try:
    from nacl.signing import SigningKey
except ImportError as exc:  # pragma: no cover - depends on test environment
    raise unittest.SkipTest("PyNaCl is required for receipt signing tests") from exc

pytestmark = pytest.mark.experimental

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from usb_writer_core import receipts as receipt_utils


def test_canonicalize_receipt_deterministic():
    payload = {
        "run_metadata": {"nonce": "123", "timestamp": "2024-01-01T00:00:00Z"},
        "identity": {"sparkplug_id": "demo", "receipt_format_version": "1.0"},
        "artifacts": {"iso_sha256": "abc"},
    }
    reordered = json.loads(json.dumps(payload))

    first = receipt_utils.canonicalize_receipt(payload)
    second = receipt_utils.canonicalize_receipt(reordered)

    assert first == second
    assert first == json.dumps(payload, separators=(",", ":"), sort_keys=True)


def test_sign_with_key_base64_roundtrip():
    original_key = SigningKey.generate()
    seed_b64 = base64.b64encode(original_key.encode()).decode("ascii")

    signing_key = receipt_utils.load_signing_key(seed_b64)
    assert signing_key.verify_key == original_key.verify_key

    payload = {"identity": {"sparkplug_id": "demo", "receipt_format_version": "1.0"}}
    canonical_json = receipt_utils.canonicalize_receipt(payload)

    public_key = receipt_utils.encode_public_key(signing_key, "base64")
    signature = receipt_utils.sign_with_key(signing_key, canonical_json, encoding="base64")

    verify_key = signing_key.verify_key
    verify_key.verify(canonical_json.encode("utf-8"), base64.b64decode(signature))

    assert len(public_key) > 0


def test_hmac_fingerprint_hex():
    fingerprint = receipt_utils.hmac_fingerprint("secret", "value", algorithm="sha256", encoding="hex")
    other = receipt_utils.hmac_fingerprint("secret", "value", algorithm="sha256", encoding="hex")
    assert fingerprint == other

    with pytest.raises(receipt_utils.ReceiptError):
        receipt_utils.hmac_fingerprint("secret", "value", algorithm="bogus")