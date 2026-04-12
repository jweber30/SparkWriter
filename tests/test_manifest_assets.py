"""Tests for manifest sidecar discovery and URL resolution policy."""

import pytest

from spark_writer.plugins.manifest_assets import (
    discover_template_sidecars,
    normalize_sidecar_ref,
    resolve_sidecar_url,
)


def test_discover_template_sidecars_deduplicates_and_normalizes():
    manifest = {
        "assets": {
            "asset_script": {"path": "scripts/setup.sh"},
        },
        "templates": {
            "inline": "echo hi",
            "sidecar_a": {"file": "scripts/setup.sh"},
            "sidecar_b": {"asset": "asset_script"},
            "sidecar_c": {"file": "configs/answer.toml"},
        }
    }

    refs = discover_template_sidecars(manifest)
    assert refs == ["scripts/setup.sh", "configs/answer.toml"]


def test_discover_template_sidecars_rejects_unknown_asset():
    manifest = {
        "assets": {},
        "templates": {
            "bad": {"asset": "missing"},
        },
    }

    with pytest.raises(ValueError, match="Unknown asset reference"):
        discover_template_sidecars(manifest)


@pytest.mark.parametrize(
    "bad_ref",
    [
        "",
        "   ",
        "/etc/passwd",
        "../secret.txt",
        "sub/../../escape.sh",
        "https://example.com/a.sh",
        "scripts\\windows-style.bat",
    ],
)
def test_normalize_sidecar_ref_rejects_unsafe_refs(bad_ref):
    with pytest.raises(ValueError):
        normalize_sidecar_ref(bad_ref)


def test_resolve_sidecar_url_allows_same_origin_and_subtree():
    manifest_url = "https://example.com/plugins/demo/manifest.json"
    resolved = resolve_sidecar_url(manifest_url, "assets/firstboot.sh")
    assert resolved == "https://example.com/plugins/demo/assets/firstboot.sh"


@pytest.mark.parametrize(
    "sidecar_ref",
    [
        "https://evil.example/attack.sh",
        "//evil.example/attack.sh",
    ],
)
def test_resolve_sidecar_url_rejects_cross_origin(sidecar_ref):
    manifest_url = "https://example.com/plugins/demo/manifest.json"
    with pytest.raises(ValueError):
        resolve_sidecar_url(manifest_url, sidecar_ref)
