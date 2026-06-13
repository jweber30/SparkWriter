"""Verified image creation and final-write checks."""

import hashlib
from pathlib import Path

import pytest

from spark_writer.core.verified_image import verify_image
from usb_writer_core.models import VerifiedImage
from usb_writer_core.writer import USBWriteError, write_iso_to_device


def test_verify_image_computes_identity_hash(tmp_path):
    path = tmp_path / "image.iso"
    path.write_bytes(b"image")
    image = verify_image(path, provenance="test")
    assert image.sha256 == hashlib.sha256(b"image").hexdigest()
    assert image.size_bytes == 5


def test_verify_image_rejects_upstream_hash_mismatch(tmp_path):
    path = tmp_path / "image.iso"
    path.write_bytes(b"image")
    with pytest.raises(RuntimeError, match="mismatch"):
        verify_image(path, expected_sha256="0" * 64, provenance="test")


def test_verify_image_rejects_symlink(tmp_path):
    target = tmp_path / "target.iso"
    target.write_bytes(b"image")
    link = tmp_path / "image.iso"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="regular file"):
        verify_image(link, provenance="test")


def test_writer_refuses_bare_path(tmp_path):
    path = tmp_path / "image.iso"
    path.write_bytes(b"image")
    device = tmp_path / "device"
    device.touch()
    with pytest.raises(USBWriteError, match="VerifiedImage"):
        write_iso_to_device(path, str(device))


def test_writer_detects_mutation_before_wipe(tmp_path, monkeypatch):
    path = tmp_path / "image.iso"
    path.write_bytes(b"image")
    image = verify_image(path, provenance="test")
    path.write_bytes(b"changed")
    device = tmp_path / "device"
    device.touch()
    wiped = []
    monkeypatch.setattr("usb_writer_core.writer.wipe_device", lambda target: wiped.append(target))

    with pytest.raises(USBWriteError, match="size changed|checksum changed"):
        write_iso_to_device(image, str(device))
    assert wiped == []


def test_verified_image_contract_is_required(tmp_path):
    assert VerifiedImage.__dataclass_fields__.keys() == {
        "path",
        "sha256",
        "size_bytes",
        "media_type",
        "provenance",
    }
