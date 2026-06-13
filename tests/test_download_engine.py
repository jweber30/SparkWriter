"""Tests for UI-neutral Source download helpers."""

from pathlib import Path

import pytest

from spark_writer.core.download_engine import _resolve_kind, resolve_torrent_artifact


class FakeStorage:
    def __init__(self, files):
        self._files = files

    def num_files(self):
        return len(self._files)

    def file_path(self, idx):
        return self._files[idx]


class FakeTorrentInfo:
    def __init__(self, files):
        self._storage = FakeStorage(files)

    def files(self):
        return self._storage


@pytest.mark.parametrize(
    ("url", "declared_kind", "expected"),
    [
        ("https://example.com/image.iso.torrent", "direct", "torrent"),
        ("https://example.com/image.iso.torrent?download=1", "direct", "torrent"),
        ("magnet:?xt=urn:btih:abc", "direct", "magnet"),
        ("https://example.com/image.iso", "direct", "direct"),
    ],
)
def test_resolve_kind_does_not_treat_torrent_metadata_as_an_iso(
    url, declared_kind, expected
):
    assert _resolve_kind(url, declared_kind) == expected


def test_resolve_torrent_artifact_uses_single_iso(tmp_path):
    (tmp_path / "SHA256SUMS").write_text("hash", encoding="utf-8")
    iso = tmp_path / "ubuntu.iso"
    iso.write_bytes(b"iso")

    result = resolve_torrent_artifact(
        torrent_info=FakeTorrentInfo(["SHA256SUMS", "ubuntu.iso"]),
        download_dir=tmp_path,
        artifact=None,
    )

    assert result == iso.resolve()


def test_resolve_torrent_artifact_uses_manifest_artifact_path(tmp_path):
    iso_a = tmp_path / "desktop.iso"
    iso_b = tmp_path / "server.iso"
    iso_a.write_bytes(b"a")
    iso_b.write_bytes(b"b")

    result = resolve_torrent_artifact(
        torrent_info=FakeTorrentInfo(["desktop.iso", "server.iso"]),
        download_dir=tmp_path,
        artifact="server.iso",
    )

    assert result == iso_b.resolve()


def test_resolve_torrent_artifact_rejects_ambiguous_multi_iso_torrent(tmp_path):
    (tmp_path / "desktop.iso").write_bytes(b"a")
    (tmp_path / "server.iso").write_bytes(b"b")

    with pytest.raises(RuntimeError) as exc_info:
        resolve_torrent_artifact(
            torrent_info=FakeTorrentInfo(["desktop.iso", "server.iso"]),
            download_dir=tmp_path,
            artifact=None,
        )

    assert "source.acquire.artifact" in str(exc_info.value)
