"""UI-neutral Source download helpers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import requests


@dataclass(frozen=True)
class DownloadEvent:
    progress: float
    speed_kbps: float = 0.0
    state: str = ""
    peers: Optional[int] = None


ProgressCallback = Callable[[DownloadEvent], None]


class DownloadCancelled(RuntimeError):
    """Raised when a download is cancelled by the caller."""


class CancelToken:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def download_source_image(
    *,
    url: str,
    download_dir: Path,
    save_name: str,
    acquire_kind: Optional[str] = None,
    artifact: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    cancel_token: Optional[CancelToken] = None,
) -> Path:
    """Download a Source image and return the selected ISO path."""

    download_dir.mkdir(parents=True, exist_ok=True)
    kind = _resolve_kind(url, acquire_kind)
    if kind == "direct":
        return _download_http(
            url=url,
            download_dir=download_dir,
            save_name=save_name,
            progress_callback=progress_callback,
            cancel_token=cancel_token,
        )
    if kind in {"torrent", "magnet"}:
        return _download_torrent(
            url=url,
            download_dir=download_dir,
            artifact=artifact,
            progress_callback=progress_callback,
            cancel_token=cancel_token,
        )
    raise ValueError(f"Unsupported download kind: {kind}")


def _resolve_kind(url: str, acquire_kind: Optional[str]) -> str:
    if acquire_kind:
        return acquire_kind
    lowered = url.lower()
    if lowered.startswith("magnet:"):
        return "magnet"
    if lowered.endswith(".torrent"):
        return "torrent"
    return "direct"


def _download_http(
    *,
    url: str,
    download_dir: Path,
    save_name: str,
    progress_callback: Optional[ProgressCallback],
    cancel_token: Optional[CancelToken],
) -> Path:
    parsed = urlparse(url)
    filename = Path(unquote(parsed.path)).name or save_name
    if not filename.lower().endswith(".iso"):
        filename = f"{filename}.iso"
    target = download_dir / filename

    with requests.get(url, stream=True, timeout=10) as response:
        response.raise_for_status()
        total_length = int(response.headers.get("content-length", 0))
        downloaded = 0
        with target.open("wb") as output:
            for chunk in response.iter_content(chunk_size=8192):
                if cancel_token and cancel_token.cancelled:
                    raise DownloadCancelled("Download cancelled")
                if not chunk:
                    continue
                downloaded += len(chunk)
                output.write(chunk)
                if total_length and progress_callback:
                    progress_callback(
                        DownloadEvent(
                            progress=(downloaded / total_length) * 100,
                            state="Downloading",
                        )
                    )

    if progress_callback:
        progress_callback(DownloadEvent(progress=100.0, state="Complete"))
    return target


def _download_torrent(
    *,
    url: str,
    download_dir: Path,
    artifact: Optional[str],
    progress_callback: Optional[ProgressCallback],
    cancel_token: Optional[CancelToken],
) -> Path:
    try:
        import libtorrent as lt
    except ImportError as exc:
        raise RuntimeError("libtorrent is required for torrent downloads") from exc

    session = lt.session()
    session.listen_on(6881, 6891)
    params = {
        "save_path": str(download_dir),
        "storage_mode": lt.storage_mode_t(2),
    }

    if url.startswith("magnet:"):
        handle = lt.add_magnet_uri(session, url, params)
    else:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        info = lt.torrent_info(response.content)
        handle = session.add_torrent({"ti": info, "save_path": str(download_dir)})

    try:
        while True:
            if cancel_token and cancel_token.cancelled:
                raise DownloadCancelled("Download cancelled")

            status = handle.status()
            if progress_callback:
                progress_callback(
                    DownloadEvent(
                        progress=float(status.progress or 0) * 100,
                        speed_kbps=float(status.download_rate or 0) / 1000,
                        state=str(status.state),
                        peers=getattr(status, "num_peers", None),
                    )
                )

            if status.is_seeding:
                torrent_info = handle.torrent_file()
                if not torrent_info:
                    raise RuntimeError("Torrent metadata unavailable after download")
                selected = resolve_torrent_artifact(
                    torrent_info=torrent_info,
                    download_dir=download_dir,
                    artifact=artifact,
                )
                if selected.exists():
                    return selected
                raise RuntimeError(f"Selected torrent artifact is missing: {selected}")

            time.sleep(1)
    finally:
        try:
            session.remove_torrent(handle)
        except Exception:
            pass


def resolve_torrent_artifact(*, torrent_info: object, download_dir: Path, artifact: Optional[str]) -> Path:
    """Select the ISO file produced by a completed torrent."""

    download_dir = download_dir.resolve()
    iso_paths = _torrent_iso_paths(torrent_info, download_dir)
    if artifact:
        requested = artifact.strip().replace("\\", "/")
        for path in iso_paths:
            rel = path.relative_to(download_dir).as_posix()
            if rel == requested or path.name == requested:
                return path
        raise RuntimeError(f"Torrent artifact '{artifact}' was not found")

    if len(iso_paths) == 1:
        return iso_paths[0]
    if not iso_paths:
        raise RuntimeError("Torrent did not contain an ISO file")
    raise RuntimeError(
        "Torrent contains multiple ISO files; set source.acquire.artifact in the manifest"
    )


def _torrent_iso_paths(torrent_info: object, download_dir: Path) -> list[Path]:
    storage = torrent_info.files()
    paths: list[Path] = []
    for idx in range(storage.num_files()):
        rel_path = storage.file_path(idx)
        candidate = (download_dir / rel_path).resolve()
        if candidate.suffix.lower() == ".iso" and not os.path.isdir(candidate):
            paths.append(candidate)
    return paths
