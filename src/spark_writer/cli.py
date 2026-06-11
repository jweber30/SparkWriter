"""Headless SparkWriter command-line workflows."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from usb_writer_core.writer import write_iso_to_device

from .core.download_engine import DownloadEvent, download_source_image
from .plugins.json_plugin import JsonSparkPlug
from .plugins.manifest_assets import discover_template_sidecars, resolve_sidecar_url
from .plugins.manifest_schema import validate_manifest_schema
from .plugins.signing import (
    build_manifest_download_request,
    is_github_manifest_url,
    normalize_github_manifest_url,
    verify_github_signed_manifest,
)
from .plugins.trust import evaluate_trust
from .sources import Source


class CliError(RuntimeError):
    """User-facing CLI failure."""


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "write":
            run_write(args)
            return 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spark-writer")
    subparsers = parser.add_subparsers(dest="command")

    write_parser = subparsers.add_parser(
        "write",
        help="Write a manifest-owned Source to a target device",
    )
    write_parser.add_argument(
        "manifest",
        help="Installed manifest id, local manifest path, file:// URL, or HTTPS manifest URL",
    )
    write_parser.add_argument(
        "--target",
        required=True,
        help="Target block device path, for example /dev/sda",
    )
    write_parser.add_argument(
        "--accept-defaults",
        action="store_true",
        help="Use manifest-declared defaults for every config field",
    )
    return parser


def run_write(args: argparse.Namespace) -> None:
    if not args.accept_defaults:
        raise CliError("write requires --accept-defaults for headless manifest configuration")

    target = str(args.target).strip()
    if not target:
        raise CliError("--target must not be empty")

    with tempfile.TemporaryDirectory(prefix="spark-writer-cli-") as temp_dir:
        temp_root = Path(temp_dir)
        plugin = _load_manifest_reference(str(args.manifest), temp_root)
        if not plugin.is_available:
            raise CliError(plugin.unavailable_reason or "manifest is unavailable")
        if not plugin.supports_usb_write():
            raise CliError(f"manifest '{plugin.plugin_id}' does not support USB writes")

        ui_values = _collect_declared_defaults(plugin)
        source = _select_manifest_source(plugin)
        source_data = source.to_dict()

        _require_runtime_approvals(plugin, ("on_iso_ready", "on_write_complete"))

        print(f"Using manifest: {plugin.name} ({plugin.plugin_id})")
        print(f"Using source: {source.name}")
        print(f"Target device: {target}")

        iso_path = _download_source(source, temp_root / "downloads")
        processed_path = plugin.on_iso_ready(str(iso_path), source_data, ui_values)

        print(f"Writing {processed_path} to {target}")
        write_iso_to_device(Path(processed_path), target, progress_callback=_progress_callback)

        plugin.on_write_complete(target, source_data, ui_values)

        secrets = plugin.get_ephemeral_secrets()
        if secrets:
            print("Ephemeral secrets:")
            for key, value in secrets.items():
                print(f"{key}: {value}")

        print("Write complete")


def _load_manifest_reference(reference: str, temp_root: Path) -> JsonSparkPlug:
    parsed = urllib.parse.urlparse(reference)
    if parsed.scheme in {"http", "https", "file"}:
        return _load_manifest_url(reference, temp_root)

    local_path = Path(reference).expanduser()
    if local_path.exists() or local_path.suffix == ".json":
        if not local_path.exists():
            raise CliError(f"manifest file not found: {local_path}")
        return JsonSparkPlug(str(local_path))

    manifest_path = _find_installed_manifest(reference)
    if manifest_path is None:
        raise CliError(f"installed manifest not found: {reference}")
    return JsonSparkPlug(str(manifest_path))


def _load_manifest_url(url: str, temp_root: Path) -> JsonSparkPlug:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        manifest_path = Path(urllib.request.url2pathname(parsed.path)).expanduser()
        if not manifest_path.exists():
            raise CliError(f"manifest file not found: {manifest_path}")
        return JsonSparkPlug(str(manifest_path))

    allowed, prompt = evaluate_trust(url, allow_insecure=False)
    if not allowed:
        raise CliError(prompt or "manifest URL is not trusted")
    if prompt:
        print(f"warning: {prompt}", file=sys.stderr)

    normalized_url = normalize_github_manifest_url(url)
    manifest_dir = temp_root / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"

    request = build_manifest_download_request(normalized_url)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            manifest_path.write_bytes(response.read())
    except Exception as exc:
        raise CliError(f"failed to download manifest: {exc}") from exc

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError(f"manifest is not valid JSON: {exc}") from exc

    if is_github_manifest_url(normalized_url):
        verified, reason = verify_github_signed_manifest(manifest, normalized_url)
        if not verified:
            raise CliError(f"GitHub manifest authorization failed: {reason}")

    legacy_secure_keys = [key for key in ("secure_manifest", "signature") if key in manifest]
    if legacy_secure_keys:
        raise CliError(
            "unsupported manifest fields: "
            + ", ".join(sorted(legacy_secure_keys))
            + ". Secure manifest keys are deprecated."
        )

    try:
        validate_manifest_schema(manifest)
    except ValueError as exc:
        raise CliError(f"manifest schema validation failed: {exc}") from exc

    _download_template_sidecars(normalized_url, manifest, manifest_dir)
    return JsonSparkPlug(str(manifest_path))


def _download_template_sidecars(manifest_url: str, manifest: dict[str, Any], manifest_dir: Path) -> None:
    for sidecar_ref in discover_template_sidecars(manifest):
        sidecar_url = resolve_sidecar_url(manifest_url, sidecar_ref)
        target = manifest_dir / sidecar_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        request = build_manifest_download_request(sidecar_url)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                target.write_bytes(response.read())
        except Exception as exc:
            raise CliError(f"failed to download manifest sidecar '{sidecar_ref}': {exc}") from exc


def _find_installed_manifest(reference: str) -> Optional[Path]:
    for manifest_path in _installed_manifest_paths(reference):
        if manifest_path.exists():
            return manifest_path

    wanted = reference.strip().lower()
    for plugin_dir in _plugin_dirs():
        if not plugin_dir.exists():
            continue
        for manifest_path in plugin_dir.glob("*.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            metadata = manifest.get("metadata", {})
            candidates = {
                str(metadata.get("id", "")).strip().lower(),
                str(metadata.get("name", "")).strip().lower(),
                manifest_path.stem.lower(),
            }
            if wanted in candidates:
                return manifest_path
    return None


def _installed_manifest_paths(reference: str) -> Iterable[Path]:
    safe_name = Path(reference).name
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    for plugin_dir in _plugin_dirs():
        yield plugin_dir / safe_name


def _plugin_dirs() -> list[Path]:
    dirs = []
    try:
        package = importlib.import_module("spark_writer.plugins.installed")
        dirs.extend(Path(path) for path in package.__path__)
    except Exception:
        pass

    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    dirs.append(data_home / "spark-writer" / "plugins")
    return dirs


def _collect_declared_defaults(plugin: JsonSparkPlug) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    missing: list[str] = []
    for field in plugin.get_config_fields():
        field_id = str(field.get("id") or field.get("key") or "").strip()
        if not field_id:
            continue
        if "default" not in field:
            missing.append(field_id)
            continue
        defaults[field_id] = field.get("default")

    if missing:
        raise CliError(
            "--accept-defaults requires defaults for every config field; missing: "
            + ", ".join(missing)
        )
    return defaults


def _select_manifest_source(plugin: JsonSparkPlug) -> Source:
    raw_sources = plugin.register_sources()
    if not raw_sources:
        raise CliError(f"manifest '{plugin.plugin_id}' does not declare a Source")
    if len(raw_sources) > 1:
        raise CliError(
            f"manifest '{plugin.plugin_id}' declares multiple Sources; CLI write needs exactly one"
        )
    try:
        source = Source.from_dict(raw_sources[0])
    except Exception as exc:
        raise CliError(f"manifest Source is invalid: {exc}") from exc
    if not source.can_write_usb:
        raise CliError(f"Source '{source.id}' does not support USB writes")
    return source


def _require_runtime_approvals(plugin: JsonSparkPlug, phases: Iterable[str]) -> None:
    for phase in phases:
        phase_pending = plugin.get_pending_phase_approval(phase)
        if not phase_pending:
            continue

        commands = ", ".join(phase_pending.commands)
        print(
            f"Manifest '{plugin.name}' needs runtime command approval for {phase}: {commands}",
            file=sys.stderr,
        )
        response = input("Approve and remember these commands? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            raise CliError(f"runtime command approval declined for {phase}")

        try:
            plugin.approve_runtime_commands(phase_pending.commands)
        except Exception as exc:
            raise CliError(f"failed to persist runtime approval for {phase}: {exc}") from exc


def _download_source(source: Source, download_dir: Path) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(source.url)

    if parsed.scheme in {"", "file"}:
        path = Path(urllib.request.url2pathname(parsed.path) if parsed.scheme == "file" else source.url)
        path = path.expanduser()
        if not path.exists():
            raise CliError(f"source image not found: {path}")
        return path

    if parsed.scheme == "magnet":
        pass
    elif parsed.scheme not in {"http", "https"}:
        raise CliError(f"unsupported Source URL scheme for CLI write: {parsed.scheme}")
    try:
        return download_source_image(
            url=source.url,
            download_dir=download_dir,
            save_name=source.id,
            acquire_kind=source.acquire_kind,
            artifact=source.acquire_artifact,
            progress_callback=_download_progress_callback,
        )
    except Exception as exc:
        raise CliError(f"failed to download Source image: {exc}") from exc


def _download_progress_callback(event: DownloadEvent) -> None:
    speed = f" - {event.speed_kbps:.1f} kB/s" if event.speed_kbps else ""
    peers = f" - {event.peers} peers" if event.peers is not None else ""
    state = f" - {event.state}" if event.state else ""
    print(f"Downloading: {int(event.progress)}%{speed}{peers}{state}")


def _progress_callback(bytes_written: int, total_bytes: int) -> None:
    if total_bytes <= 0:
        return
    percent = int((bytes_written / total_bytes) * 100)
    print(f"Writing: {percent}%")
