"""Creation and validation of image identities passed to usb-writer-core."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Callable, Optional

from usb_writer_core.models import VerifiedImage


ISO_MEDIA_TYPE = "application/x-iso9660-image"


def sha256_file(path: Path, progress_callback: Optional[Callable[[int, int], None]] = None) -> str:
    """Compute SHA-256 hash of a file with optional progress reporting.
    
    Args:
        path: Path to file to hash
        progress_callback: Optional callable(bytes_processed, total_bytes) for progress updates
        
    Returns:
        Lowercase hex digest string
    """
    digest = hashlib.sha256()
    try:
        total_bytes = path.stat().st_size
    except OSError:
        total_bytes = 0
    
    bytes_processed = 0
    chunk_size = 1024 * 1024
    
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
            bytes_processed += len(chunk)
            if progress_callback and total_bytes > 0:
                progress_callback(bytes_processed, total_bytes)
    
    return digest.hexdigest()


def verify_image(
    path: Path | str,
    *,
    expected_sha256: Optional[str] = None,
    media_type: str = ISO_MEDIA_TYPE,
    provenance: str,
    compute_hash: bool = True,
) -> VerifiedImage:
    """Validate and wrap an image file in a VerifiedImage contract.
    
    Args:
        path: Path to the image file
        expected_sha256: If provided, verify the image matches this SHA-256 hash
        media_type: MIME type of the image
        provenance: Source/origin description (e.g., "source:ubuntu-24.04")
        compute_hash: If False and no expected_sha256, skip hashing (optimization for Crostini)
                      Only skip if the hash is not needed for audit/verification.
    
    Returns:
        VerifiedImage contract with validated path and optional sha256
        
    Raises:
        RuntimeError: If file is missing, invalid, empty, or hash mismatch
    """
    candidate = Path(path).expanduser()
    try:
        file_stat = os.lstat(candidate)
    except OSError as exc:
        raise RuntimeError(f"Image is unavailable: {candidate}: {exc}") from exc

    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"Image must be a regular file, not a link: {candidate}")
    if file_stat.st_size <= 0:
        raise RuntimeError(f"Image is empty: {candidate}")

    # Only compute hash if we have an external verification source or if explicitly requested
    expected = str(expected_sha256 or "").strip()
    actual = ""
    
    if expected:
        # Verification required: hash and compare against expected value
        actual = sha256_file(candidate)
        if actual != expected:
            raise RuntimeError(
                f"Image SHA-256 mismatch: expected {expected}, computed {actual}"
            )
    elif compute_hash:
        # No verification source, but caller wants the hash (e.g., for receipts/audit)
        actual = sha256_file(candidate)
    # else: skip hashing entirely for unverified downloads on resource-constrained systems

    return VerifiedImage(
        path=candidate.resolve(),
        sha256=actual,  # Empty string if hashing was skipped
        size_bytes=file_stat.st_size,
        media_type=media_type,
        provenance=provenance,
    )
