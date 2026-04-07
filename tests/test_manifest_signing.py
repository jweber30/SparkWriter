"""Tests for GitHub manifest signature verification policy."""

from unittest.mock import patch

from spark_writer.plugins.signing import (
    extract_github_username_from_url,
    is_github_manifest_url,
    verify_github_signed_manifest,
)


def _base_manifest(username: str = "alice"):
    return {
        "version": "1.0",
        "metadata": {
            "id": "example-plugin",
            "name": "Example Plugin",
            "github_username": username,
            "signature": {
                "algorithm": "openssh-ssh-ed25519",
                "openssh": "-----BEGIN SSH SIGNATURE-----\nfake\n-----END SSH SIGNATURE-----\n",
            },
        },
        "requires": {"commands": []},
    }


def test_extract_username_from_raw_url():
    username = extract_github_username_from_url(
        "https://raw.githubusercontent.com/MetalStrapper/plugin/main/manifest.json"
    )
    assert username == "metalstrapper"


def test_extract_username_from_gist_url():
    username = extract_github_username_from_url(
        "https://gist.githubusercontent.com/JWeber/abc123/raw/manifest.json"
    )
    assert username == "jweber"


def test_extract_username_from_pages_subdomain():
    username = extract_github_username_from_url("https://alice.github.io/plugin/manifest.json")
    assert username == "alice"


def test_extract_username_fails_when_not_derivable():
    username = extract_github_username_from_url("https://github.io/plugin/manifest.json")
    assert username is None


def test_is_github_manifest_url_true_for_supported_hosts():
    assert is_github_manifest_url("https://raw.githubusercontent.com/a/b/c.json") is True
    assert is_github_manifest_url("https://gist.githubusercontent.com/a/b/raw/c.json") is True
    assert is_github_manifest_url("https://a.github.io/plugin.json") is True


def test_is_github_manifest_url_false_for_non_github_hosts():
    assert is_github_manifest_url("https://gitlab.com/group/plugin.json") is False


def test_verify_fails_when_username_mismatch():
    manifest = _base_manifest(username="bob")
    ok, reason = verify_github_signed_manifest(
        manifest,
        "https://raw.githubusercontent.com/alice/repo/main/plugin.json",
    )
    assert ok is False
    assert "does not match URL owner" in reason


def test_verify_fails_when_username_not_derivable():
    manifest = _base_manifest(username="alice")
    ok, reason = verify_github_signed_manifest(
        manifest,
        "https://github.io/plugin.json",
    )
    assert ok is False
    assert "Could not derive GitHub username" in reason


def test_verify_fails_when_github_username_missing():
    manifest = _base_manifest()
    manifest["metadata"].pop("github_username")
    ok, reason = verify_github_signed_manifest(
        manifest,
        "https://raw.githubusercontent.com/alice/repo/main/plugin.json",
    )
    assert ok is False
    assert "metadata.github_username is required" in reason


@patch("spark_writer.plugins.signing._verify_openssh_signature")
@patch("spark_writer.plugins.signing._fetch_github_signing_keys")
def test_verify_success_path(mock_fetch_keys, mock_verify):
    manifest = _base_manifest(username="alice")
    mock_fetch_keys.return_value = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample"]
    mock_verify.return_value = True

    ok, reason = verify_github_signed_manifest(
        manifest,
        "https://raw.githubusercontent.com/alice/repo/main/plugin.json",
    )

    assert ok is True
    assert reason == ""
    mock_fetch_keys.assert_called_once_with("alice")
    assert mock_verify.called is True


@patch("spark_writer.plugins.signing._verify_openssh_signature")
@patch("spark_writer.plugins.signing._fetch_github_signing_keys")
def test_verify_fails_when_signature_invalid(mock_fetch_keys, mock_verify):
    manifest = _base_manifest(username="alice")
    mock_fetch_keys.return_value = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample"]
    mock_verify.return_value = False

    ok, reason = verify_github_signed_manifest(
        manifest,
        "https://raw.githubusercontent.com/alice/repo/main/plugin.json",
    )

    assert ok is False
    assert reason == "Manifest signature verification failed"
