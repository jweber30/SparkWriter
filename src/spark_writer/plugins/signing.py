"""Manifest signature verification helpers for GitHub-hosted manifests."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


GITHUB_MANIFEST_HOSTS = frozenset({
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "github.io",
})


def is_github_manifest_url(url: str) -> bool:
    """Return True when URL points to a GitHub-hosted manifest source."""
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    for trusted in GITHUB_MANIFEST_HOSTS:
        if hostname == trusted or hostname.endswith("." + trusted):
            return True
    return False


def extract_github_username_from_url(url: str) -> Optional[str]:
    """Extract expected GitHub username from manifest URL when possible."""
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path_parts = [p for p in parsed.path.split("/") if p]

    if hostname == "raw.githubusercontent.com":
        if len(path_parts) >= 1:
            return path_parts[0].lower()
        return None

    if hostname == "gist.githubusercontent.com":
        if len(path_parts) >= 1:
            return path_parts[0].lower()
        return None

    if hostname.endswith(".github.io"):
        labels = hostname.split(".")
        if len(labels) >= 3:
            return labels[0].lower()
        return None

    return None


def _canonical_manifest_payload(manifest: Dict[str, Any]) -> bytes:
    """Canonicalize manifest payload for signature verification.

    Canonical payload excludes metadata.signature and uses deterministic JSON.
    """
    canonical = copy.deepcopy(manifest)
    metadata = canonical.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("signature", None)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return payload.encode("utf-8")


def _fetch_github_signing_keys(username: str, timeout: int = 10) -> List[str]:
    """Fetch GitHub SSH signing public keys for a user."""
    endpoint = f"https://api.github.com/users/{username}/ssh_signing_keys"
    req = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "spark-writer",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API request failed ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub API returned invalid JSON") from exc

    if not isinstance(data, list):
        raise RuntimeError("GitHub API returned unexpected response format")

    keys: List[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if isinstance(key, str) and key.strip():
            keys.append(key.strip())

    return keys


def _verify_openssh_signature(payload: bytes, openssh_signature: str, username: str, keys: List[str]) -> bool:
    """Verify OpenSSH armored signature with ssh-keygen against allowed signers."""
    if not shutil.which("ssh-keygen"):
        raise RuntimeError("ssh-keygen is required for manifest signature verification")

    with tempfile.TemporaryDirectory(prefix="spark-sign-") as tmp_dir:
        sig_path = f"{tmp_dir}/manifest.sig"
        allowed_path = f"{tmp_dir}/allowed_signers"

        with open(sig_path, "w", encoding="utf-8") as sig_file:
            sig_file.write(openssh_signature)

        with open(allowed_path, "w", encoding="utf-8") as allowed:
            for key in keys:
                allowed.write(f"{username} {key}\n")

        result = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                allowed_path,
                "-I",
                username,
                "-n",
                "file",
                "-s",
                sig_path,
            ],
            input=payload,
            capture_output=True,
            check=False,
        )
        return result.returncode == 0


def verify_github_signed_manifest(manifest: Dict[str, Any], manifest_url: str) -> Tuple[bool, str]:
    """Verify GitHub-hosted manifest identity and OpenSSH signature.

    Returns:
        (True, "") on success, otherwise (False, reason).
    """
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        return False, "Manifest metadata is missing"

    declared_username = metadata.get("github_username")
    if not isinstance(declared_username, str) or not declared_username.strip():
        return False, "metadata.github_username is required for GitHub-hosted manifests"
    declared_username = declared_username.strip().lower()

    expected_username = extract_github_username_from_url(manifest_url)
    if not expected_username:
        return False, "Could not derive GitHub username from manifest URL"
    if expected_username != declared_username:
        return (
            False,
            f"Manifest github_username '{declared_username}' does not match URL owner '{expected_username}'",
        )

    signature = metadata.get("signature")
    if not isinstance(signature, dict):
        return False, "metadata.signature is required for GitHub-hosted manifests"

    openssh_signature = signature.get("openssh")
    if not isinstance(openssh_signature, str) or not openssh_signature.strip():
        return False, "metadata.signature.openssh is required"

    algorithm = signature.get("algorithm", "")
    if algorithm and algorithm != "openssh-ssh-ed25519":
        return False, "Only openssh-ssh-ed25519 signatures are supported"

    try:
        keys = _fetch_github_signing_keys(declared_username)
    except RuntimeError as exc:
        return False, str(exc)

    if not keys:
        return False, "No SSH signing keys found for GitHub user"

    payload = _canonical_manifest_payload(manifest)

    try:
        verified = _verify_openssh_signature(payload, openssh_signature, declared_username, keys)
    except RuntimeError as exc:
        return False, str(exc)

    if not verified:
        return False, "Manifest signature verification failed"

    return True, ""