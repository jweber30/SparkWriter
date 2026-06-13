"""OCI builder trust-boundary tests."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from spark_writer.builders.runner import (
    BUILTIN_PROXMOX_IMAGE,
    LEGACY_BUILTIN_PROXMOX_IMAGE,
    LEGACY_BUILTIN_PROXMOX_IMAGES,
    BuilderIdentity,
    OciBuilderRunner,
)


def _identity() -> BuilderIdentity:
    return BuilderIdentity(
        builder_id="test-builder",
        image="example.test/builder:latest",
        digest="a" * 64,
        network=False,
    )


def _write_result(outputs: Path, artifact_value="/artifacts/output.iso", **changes):
    artifact = outputs / "output.iso"
    artifact.write_bytes(b"image")
    payload = {
        "resultVersion": "1",
        "artifact": artifact_value,
        "sha256": hashlib.sha256(b"image").hexdigest(),
        "mediaType": "application/x-iso9660-image",
        "builder": "test-builder",
        "builderVersion": "1.2.3",
    }
    payload.update(changes)
    (outputs / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return artifact


def test_runtime_prefers_podman(monkeypatch):
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"podman", "docker"} else None,
    )
    assert OciBuilderRunner.detect_runtime() == "podman"


def test_runtime_falls_back_to_docker(monkeypatch):
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    assert OciBuilderRunner.detect_runtime() == "docker"


def test_bundled_proxmox_image_includes_iso_tooling():
    context = OciBuilderRunner.bundled_proxmox_context()
    containerfile = (context / "Containerfile").read_text(encoding="utf-8")
    builder = (context / "builder.py").read_text(encoding="utf-8")
    assert "proxmox-auto-install-assistant xorriso" in containerfile
    assert '"--tmp",\n        "/tmp",' in builder


def test_builder_apt_proxy_is_passed_as_build_argument(monkeypatch):
    runner = OciBuilderRunner(runtime="podman")
    operations = []
    monkeypatch.setenv(
        "SPARK_WRITER_APT_PROXY",
        "http://apt-proxy.lan:3142/",
    )
    monkeypatch.setattr(runner, "_inspect_digest", lambda image: None)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda command, operation: operations.append((command, operation)),
    )

    runner._ensure_builtin_image(BUILTIN_PROXMOX_IMAGE)

    command, operation = operations[0]
    assert operation == "build bundled Proxmox builder"
    assert "--build-arg=APT_PROXY=http://apt-proxy.lan:3142" in command


@pytest.mark.parametrize(
    "value",
    [
        "ftp://apt-proxy.lan:3142",
        "http://user:password@apt-proxy.lan:3142",
        "http://apt-proxy.lan:3142/cache",
        'http://apt-proxy.lan:3142/"',
    ],
)
def test_builder_apt_proxy_rejects_unsafe_urls(monkeypatch, value):
    monkeypatch.setenv("SPARK_WRITER_APT_PROXY", value)
    with pytest.raises(RuntimeError, match="SPARK_WRITER_APT_PROXY"):
        OciBuilderRunner._builder_apt_proxy()


@pytest.mark.parametrize(
    "image",
    [
        BUILTIN_PROXMOX_IMAGE,
        f"localhost/{BUILTIN_PROXMOX_IMAGE}",
        LEGACY_BUILTIN_PROXMOX_IMAGE,
        f"localhost/{LEGACY_BUILTIN_PROXMOX_IMAGE}",
        "spark-writer/proxmox-auto-install:bookworm-v2",
        "localhost/spark-writer/proxmox-auto-install:bookworm-v2",
    ],
)
def test_bundled_proxmox_image_aliases_are_local(image):
    assert OciBuilderRunner._builtin_proxmox_image(image) is not None


def test_legacy_bundled_image_is_rebuilt_instead_of_pulled(monkeypatch):
    runner = OciBuilderRunner(runtime="podman")
    operations = []
    image = next(iter(LEGACY_BUILTIN_PROXMOX_IMAGES))
    OciBuilderRunner._refreshed_legacy_images.discard(image)
    monkeypatch.setattr(runner, "_inspect_digest", lambda image: "a" * 64)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda command, operation: operations.append((command, operation)),
    )

    identity = runner.resolve_identity(
        builder_id="proxmox-auto-install",
        image=image,
        network=False,
    )

    assert identity.digest == "a" * 64
    assert [operation for _, operation in operations] == [
        "build bundled Proxmox builder"
    ]
    assert operations[0][0][1] == "build"


def test_legacy_bundled_image_is_refreshed_only_once_per_process(monkeypatch):
    runner = OciBuilderRunner(runtime="podman")
    operations = []
    image = next(iter(LEGACY_BUILTIN_PROXMOX_IMAGES))
    OciBuilderRunner._refreshed_legacy_images.discard(image)
    monkeypatch.setattr(runner, "_inspect_digest", lambda image: "a" * 64)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda command, operation: operations.append((command, operation)),
    )

    for _ in range(2):
        runner.resolve_identity(
            builder_id="proxmox-auto-install",
            image=image,
            network=False,
        )

    assert [operation for _, operation in operations] == [
        "build bundled Proxmox builder"
    ]


def test_result_is_independently_verified(tmp_path):
    _write_result(tmp_path)
    result = OciBuilderRunner._load_result(tmp_path, _identity())
    try:
        assert result.sha256 == hashlib.sha256(b"image").hexdigest()
        assert result.artifact.read_bytes() == b"image"
    finally:
        result.artifact.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"resultVersion": "2"}, "resultVersion"),
        ({"builder": "other"}, "identity"),
        ({"builderVersion": ""}, "builderVersion"),
        ({"mediaType": "application/octet-stream"}, "media type"),
        ({"sha256": "A" * 64}, "lowercase"),
        ({"extra": True}, "unknown"),
    ],
)
def test_result_rejects_invalid_contract(tmp_path, changes, message):
    _write_result(tmp_path, **changes)
    with pytest.raises(RuntimeError, match=message):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_result_rejects_artifact_path_escape(tmp_path):
    _write_result(tmp_path, artifact_value="/tmp/output.iso")
    with pytest.raises(RuntimeError, match="direct child"):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_result_rejects_artifact_symlink(tmp_path):
    target = tmp_path / "target.iso"
    target.write_bytes(b"image")
    os.symlink(target, tmp_path / "output.iso")
    payload = {
        "resultVersion": "1",
        "artifact": "/artifacts/output.iso",
        "sha256": hashlib.sha256(b"image").hexdigest(),
        "mediaType": "application/x-iso9660-image",
        "builder": "test-builder",
        "builderVersion": "1",
    }
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="regular file"):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_result_rejects_incorrect_checksum(tmp_path):
    _write_result(tmp_path, sha256="0" * 64)
    with pytest.raises(RuntimeError, match="does not match"):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_result_rejects_malformed_json(tmp_path):
    (tmp_path / "result.json").write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid result.json"):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_result_rejects_missing_artifact(tmp_path):
    _write_result(tmp_path)
    (tmp_path / "output.iso").unlink()
    with pytest.raises(RuntimeError, match="artifact is missing"):
        OciBuilderRunner._load_result(tmp_path, _identity())


def test_run_uses_hardened_container_flags(monkeypatch, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"source")
    config = tmp_path / "answer.toml"
    config.write_text("[global]\n", encoding="utf-8")
    captured = {}

    runner = OciBuilderRunner(runtime="podman")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def fake_run(command, operation):
        captured["command"] = command
        output_arg = next(
            arg
            for arg in command
            if arg.startswith("--volume=") and ":/artifacts:rw" in arg
        )
        output_dir = Path(
            output_arg.removeprefix("--volume=").split(":/artifacts:rw", 1)[0]
        )
        _write_result(output_dir)

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run(
        identity=_identity(),
        source_iso=source,
        artifacts={"answer-file": (config, False)},
    )
    try:
        command = captured["command"]
        assert "--read-only" in command
        assert "--cap-drop=ALL" in command
        assert "--security-opt=no-new-privileges" in command
        assert "--network=none" in command
        assert any(arg.startswith("--user=") for arg in command)
        assert "--memory=3g" in command
        assert "--memory-swap=3g" in command
        assert "--userns=keep-id" in command
        assert not any(arg.startswith("--tmpfs=/tmp") for arg in command)
        assert any(arg.endswith(":/inputs:ro,Z") for arg in command)
        assert any(arg.endswith(":/artifacts:rw,Z") for arg in command)
        assert any(arg.endswith(":/tmp:rw,Z") for arg in command)
        assert not any("/dev" in arg or "docker.sock" in arg for arg in command)
    finally:
        result.artifact.unlink(missing_ok=True)


def test_run_uses_portable_docker_volume_options(monkeypatch, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"source")
    captured = {}
    runner = OciBuilderRunner(runtime="docker")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def fake_run(command, operation):
        captured["command"] = command
        output_arg = next(
            arg
            for arg in command
            if arg.startswith("--volume=") and ":/artifacts:rw" in arg
        )
        output_dir = Path(
            output_arg.removeprefix("--volume=").split(":/artifacts:rw", 1)[0]
        )
        _write_result(output_dir)

    monkeypatch.setattr(runner, "_run", fake_run)
    result = runner.run(identity=_identity(), source_iso=source, artifacts={})
    try:
        command = captured["command"]
        assert "--userns=keep-id" not in command
        assert "--memory=3g" in command
        assert "--memory-swap=3g" in command
        assert not any(arg.startswith("--tmpfs=/tmp") for arg in command)
        assert any(arg.endswith(":/inputs:ro") for arg in command)
        assert any(arg.endswith(":/artifacts:rw") for arg in command)
        assert any(arg.endswith(":/tmp:rw") for arg in command)
    finally:
        result.artifact.unlink(missing_ok=True)


def test_builder_workspace_uses_disk_cache_instead_of_system_tmp(monkeypatch, tmp_path):
    cache_home = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    workspace = OciBuilderRunner._workspace_dir()

    assert workspace == cache_home / "spark-writer" / "builders"
    assert workspace.is_dir()


def test_builder_rejects_insufficient_workspace_capacity(monkeypatch, tmp_path):
    source = tmp_path / "source.iso"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "spark_writer.builders.runner.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=10, used=9, free=1),
    )

    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        OciBuilderRunner._check_workspace_capacity(tmp_path, source)
