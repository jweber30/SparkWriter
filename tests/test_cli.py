"""Headless CLI manifest write tests."""

import argparse
import json
from pathlib import Path

import pytest

from spark_writer import cli


def _write_manifest(tmp_path: Path, *, fields=None, actions=None, requires=None) -> Path:
    iso = tmp_path / "source.iso"
    iso.write_bytes(b"iso")
    manifest = {
        "version": "1.4",
        "metadata": {
            "id": "cli-test",
            "name": "CLI Test",
            "version": "1.0.0",
        },
        "requires": {"commands": requires or []},
        "source": {
            "id": "clisource",
            "name": "CLI Source",
            "family": "test",
            "url": str(iso),
        },
        "config_fields": fields or [],
        "actions": actions or {},
    }
    path = tmp_path / "cli-test.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_write_uses_manifest_defaults(monkeypatch, tmp_path):
    manifest_path = _write_manifest(
        tmp_path,
        fields=[
            {
                "id": "hostname",
                "label": "Hostname",
                "type": "text",
                "default": "recovery-host",
                "required": True,
            }
        ],
    )
    writes = []

    def fake_write(iso_path, target, progress_callback=None):
        writes.append((iso_path, target))

    monkeypatch.setattr(cli, "write_iso_to_device", fake_write)

    cli.run_write(
        argparse.Namespace(
            manifest=str(manifest_path),
            target="/dev/test",
            accept_defaults=True,
        )
    )

    assert writes == [(tmp_path / "source.iso", "/dev/test")]


def test_write_rejects_manifest_fields_without_defaults(monkeypatch, tmp_path):
    manifest_path = _write_manifest(
        tmp_path,
        fields=[
            {
                "id": "hostname",
                "label": "Hostname",
                "type": "text",
                "required": True,
            }
        ],
    )
    monkeypatch.setattr(cli, "write_iso_to_device", lambda *args, **kwargs: None)

    with pytest.raises(cli.CliError) as exc_info:
        cli.run_write(
            argparse.Namespace(
                manifest=str(manifest_path),
                target="/dev/test",
                accept_defaults=True,
            )
        )

    assert "missing: hostname" in str(exc_info.value)


def test_write_prompts_and_persists_runtime_command_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = _write_manifest(
        tmp_path,
        fields=[
            {
                "id": "hostname",
                "label": "Hostname",
                "type": "text",
                "default": "recovery-host",
            }
        ],
        requires=[
            {
                "name": "true",
                "description": "test command",
                "install_hint": "provided by coreutils",
                "allow_plugin_specific": True,
            }
        ],
        actions={
            "on_write_complete": [
                {
                    "id": "run_true",
                    "type": "run_command",
                    "command": ["true"],
                }
            ]
        },
    )

    monkeypatch.setattr(cli, "write_iso_to_device", lambda *args, **kwargs: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    cli.run_write(
        argparse.Namespace(
            manifest=str(manifest_path),
            target="/dev/test",
            accept_defaults=True,
        )
    )

    approval_file = tmp_path / "state" / "spark-writer" / "approvals" / ".cli-test.approval"
    payload = json.loads(approval_file.read_text(encoding="utf-8"))
    assert payload["approval_model"] == "invocation-v2"
    assert payload["approved_commands"] == ["true"]
