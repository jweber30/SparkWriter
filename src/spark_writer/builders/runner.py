"""Hardened OCI builder runner and result validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional
from urllib.parse import urlparse

from spark_writer.core.verified_image import ISO_MEDIA_TYPE, sha256_file


RESULT_FIELDS = {
    "resultVersion",
    "artifact",
    "sha256",
    "mediaType",
    "builder",
    "builderVersion",
}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
BUILTIN_PROXMOX_IMAGE = "spark-writer/proxmox-auto-install:trixie-v2"
LEGACY_BUILTIN_PROXMOX_IMAGE = "spark-writer/proxmox-auto-install:bookworm"
LEGACY_BUILTIN_PROXMOX_IMAGES = {
    LEGACY_BUILTIN_PROXMOX_IMAGE,
    "spark-writer/proxmox-auto-install:bookworm-v2",
    "spark-writer/proxmox-auto-install:trixie-v1",
}
BUILDER_MEMORY_LIMIT = "3g"
MIN_BUILDER_HEADROOM = 2 * 1024**3


@dataclass(frozen=True)
class BuilderIdentity:
    builder_id: str
    image: str
    digest: str
    network: bool

    @property
    def approval_key(self) -> str:
        return "|".join(
            (
                self.builder_id,
                self.image,
                self.digest,
                "network" if self.network else "no-network",
                "mounts=/inputs:ro,/artifacts:rw",
            )
        )

    @property
    def display(self) -> str:
        network = "network enabled" if self.network else "network disabled"
        return (
            f"{self.builder_id}: {self.image} "
            f"({self.digest}, {network}, /inputs read-only, /artifacts writable)"
        )


@dataclass(frozen=True)
class BuilderResult:
    artifact: Path
    sha256: str
    media_type: str
    builder: str
    builder_version: str
    identity: BuilderIdentity


class OciBuilderRunner:
    _legacy_refresh_lock = threading.Lock()
    _refreshed_legacy_images: set[str] = set()

    def __init__(self, runtime: Optional[str] = None):
        self.runtime = runtime or self.detect_runtime()

    @staticmethod
    def detect_runtime() -> str:
        for candidate in ("podman", "docker"):
            if shutil.which(candidate):
                return candidate
        raise RuntimeError(
            "No OCI container runtime found. Install rootless Podman or Docker."
        )

    @staticmethod
    def bundled_proxmox_context() -> Path:
        return Path(__file__).with_name("proxmox")

    def resolve_identity(
        self,
        *,
        builder_id: str,
        image: str,
        network: bool,
    ) -> BuilderIdentity:
        builtin_image = self._builtin_proxmox_image(image)
        if builtin_image is not None:
            if builtin_image in LEGACY_BUILTIN_PROXMOX_IMAGES:
                self._refresh_legacy_builtin_image(image)
            else:
                self._ensure_builtin_image(image)
        else:
            self._run([self.runtime, "pull", image], "pull builder image")

        digest = self._inspect_digest(image)
        if not digest or not SHA256_RE.fullmatch(digest):
            raise RuntimeError(f"Unable to resolve immutable digest for builder image: {image}")
        return BuilderIdentity(builder_id, image, digest, network)

    def _refresh_legacy_builtin_image(self, image: str) -> None:
        """Refresh a legacy alias once, keeping its approval identity stable afterward."""
        with self._legacy_refresh_lock:
            if image in self._refreshed_legacy_images:
                return
            self._ensure_builtin_image(image, force_rebuild=True)
            self._refreshed_legacy_images.add(image)

    def lookup_identity(
        self,
        *,
        builder_id: str,
        image: str,
        network: bool,
    ) -> Optional[BuilderIdentity]:
        """Return an identity only when the image already exists locally."""

        digest = self._inspect_digest(image)
        if not digest or not SHA256_RE.fullmatch(digest):
            return None
        return BuilderIdentity(builder_id, image, digest, network)

    def run(
        self,
        *,
        identity: BuilderIdentity,
        source_iso: Path,
        artifacts: Mapping[str, tuple[Path, bool]],
    ) -> BuilderResult:
        workspace = self._workspace_dir()
        self._check_workspace_capacity(workspace, source_iso)
        with tempfile.TemporaryDirectory(
            prefix="spark-builder-",
            dir=workspace,
        ) as temp_dir:
            root = Path(temp_dir)
            inputs_dir = root / "inputs"
            outputs_dir = root / "artifacts"
            scratch_dir = root / "scratch"
            inputs_dir.mkdir(mode=0o700)
            outputs_dir.mkdir(mode=0o700)
            scratch_dir.mkdir(mode=0o700)

            source_target = inputs_dir / "source.iso"
            try:
                os.link(source_iso, source_target)
            except OSError:
                shutil.copyfile(source_iso, source_target)
            request_inputs: dict[str, str] = {}
            for role, (path, executable) in artifacts.items():
                file_name = f"{role}-{path.name}"
                target = inputs_dir / file_name
                shutil.copyfile(path, target)
                os.chmod(target, 0o755 if executable else 0o644)
                request_inputs[role] = f"/inputs/{file_name}"

            request = {
                "requestVersion": "1",
                "builder": identity.builder_id,
                "source": "/inputs/source.iso",
                "inputs": request_inputs,
                "artifactsDirectory": "/artifacts",
            }
            (inputs_dir / "request.json").write_text(
                json.dumps(request, sort_keys=True), encoding="utf-8"
            )

            command = [
                self.runtime,
                "run",
                "--rm",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--network=bridge" if identity.network else "--network=none",
                f"--user={os.getuid()}:{os.getgid()}",
                f"--memory={BUILDER_MEMORY_LIMIT}",
                f"--memory-swap={BUILDER_MEMORY_LIMIT}",
            ]
            volume_label = ""
            if Path(self.runtime).name == "podman":
                command.append("--userns=keep-id")
                volume_label = ",Z"
            command.extend(
                [
                    f"--volume={inputs_dir}:/inputs:ro{volume_label}",
                    f"--volume={outputs_dir}:/artifacts:rw{volume_label}",
                    f"--volume={scratch_dir}:/tmp:rw{volume_label}",
                    identity.image,
                ]
            )
            self._run(command, "run builder")
            return self._load_result(
                outputs_dir,
                identity,
                destination_dir=workspace,
            )

    @staticmethod
    def _workspace_dir() -> Path:
        cache_home = Path(
            os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
        ).expanduser()
        workspace = cache_home / "spark-writer" / "builders"
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

    @staticmethod
    def _check_workspace_capacity(workspace: Path, source_iso: Path) -> None:
        source_size = source_iso.stat().st_size
        required = (source_size * 2) + MIN_BUILDER_HEADROOM
        available = shutil.disk_usage(workspace).free
        if available < required:
            required_gib = required / 1024**3
            available_gib = available / 1024**3
            raise RuntimeError(
                "Insufficient disk space for ISO builder workspace: "
                f"{required_gib:.1f} GiB required, {available_gib:.1f} GiB available"
            )

    @staticmethod
    def _builtin_proxmox_image(image: str) -> Optional[str]:
        normalized = image.removeprefix("localhost/")
        if normalized == BUILTIN_PROXMOX_IMAGE or normalized in LEGACY_BUILTIN_PROXMOX_IMAGES:
            return normalized
        return None

    def _ensure_builtin_image(self, image: str, *, force_rebuild: bool = False) -> None:
        if not force_rebuild and self._inspect_digest(image):
            return
        context = self.bundled_proxmox_context()
        if not (context / "Containerfile").is_file():
            raise RuntimeError("Bundled Proxmox builder context is missing")
        command = [
            self.runtime,
            "build",
            "--tag",
            image,
            "--file",
            str(context / "Containerfile"),
        ]
        apt_proxy = self._builder_apt_proxy()
        if apt_proxy:
            command.append(f"--build-arg=APT_PROXY={apt_proxy}")
        command.append(str(context))
        self._run(
            command,
            "build bundled Proxmox builder",
        )

    @staticmethod
    def _builder_apt_proxy() -> Optional[str]:
        value = os.environ.get("SPARK_WRITER_APT_PROXY", "").strip()
        if not value:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or any(char in value for char in {'"', "\n", "\r"})
        ):
            raise RuntimeError(
                "SPARK_WRITER_APT_PROXY must be an HTTP(S) proxy URL "
                "without credentials, a path, query, fragment, or control characters"
            )
        return value.rstrip("/")

    def _inspect_digest(self, image: str) -> Optional[str]:
        result = subprocess.run(
            [self.runtime, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        if value.startswith("sha256:"):
            value = value.removeprefix("sha256:")
        return value or None

    @staticmethod
    def _load_result(
        outputs_dir: Path,
        identity: BuilderIdentity,
        *,
        destination_dir: Optional[Path] = None,
    ) -> BuilderResult:
        result_path = outputs_dir / "result.json"
        try:
            result_stat = os.lstat(result_path)
            if stat.S_ISLNK(result_stat.st_mode) or not stat.S_ISREG(result_stat.st_mode):
                raise RuntimeError("Builder result.json must be a regular file")
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Builder emitted invalid result.json: {exc}") from exc

        if not isinstance(raw, dict):
            raise RuntimeError("Builder result.json must contain an object")
        unknown = set(raw) - RESULT_FIELDS
        missing = RESULT_FIELDS - set(raw)
        if unknown or missing:
            detail = []
            if missing:
                detail.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                detail.append(f"unknown {', '.join(sorted(unknown))}")
            raise RuntimeError("Invalid builder result fields: " + "; ".join(detail))
        if raw["resultVersion"] != "1":
            raise RuntimeError("Unsupported builder resultVersion")
        if raw["builder"] != identity.builder_id:
            raise RuntimeError("Builder result identity does not match requested builder")
        if not isinstance(raw["builderVersion"], str) or not raw["builderVersion"].strip():
            raise RuntimeError("Builder result requires a nonempty builderVersion")
        if raw["mediaType"] != ISO_MEDIA_TYPE:
            raise RuntimeError(f"Unsupported builder media type: {raw['mediaType']}")
        if not isinstance(raw["sha256"], str) or not SHA256_RE.fullmatch(raw["sha256"]):
            raise RuntimeError("Builder result sha256 must be lowercase hexadecimal")

        container_path = PurePosixPath(str(raw["artifact"]))
        if not container_path.is_absolute() or container_path.parent != PurePosixPath("/artifacts"):
            raise RuntimeError("Builder artifact must be a direct child of /artifacts")
        artifact = outputs_dir / container_path.name
        try:
            artifact_stat = os.lstat(artifact)
        except OSError as exc:
            raise RuntimeError(f"Builder artifact is missing: {exc}") from exc
        if stat.S_ISLNK(artifact_stat.st_mode) or not stat.S_ISREG(artifact_stat.st_mode):
            raise RuntimeError("Builder artifact must be a regular file")
        if artifact_stat.st_size <= 0:
            raise RuntimeError("Builder artifact is empty")
        actual = sha256_file(artifact)
        if actual != raw["sha256"]:
            raise RuntimeError("Builder artifact SHA-256 does not match result.json")

        # The temporary staging directory is about to be removed. Preserve the
        # validated output in a host-owned temporary file.
        destination_dir = destination_dir or OciBuilderRunner._workspace_dir()
        fd, final_name = tempfile.mkstemp(
            prefix="spark-built-",
            suffix=".iso",
            dir=destination_dir,
        )
        os.close(fd)
        final_path = Path(final_name)
        final_path.unlink()
        shutil.move(artifact, final_path)
        if sha256_file(final_path) != actual:
            final_path.unlink(missing_ok=True)
            raise RuntimeError("Builder artifact changed while being imported")
        return BuilderResult(
            artifact=final_path,
            sha256=actual,
            media_type=raw["mediaType"],
            builder=raw["builder"],
            builder_version=raw["builderVersion"].strip(),
            identity=identity,
        )

    @staticmethod
    def _run(command: list[str], operation: str) -> None:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"Failed to {operation}: {detail}")
