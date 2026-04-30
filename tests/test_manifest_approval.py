"""Tests for invocation-time command approval and disclosure contracts.

These tests intentionally encode the target behavior for a migration where:
1. Command approvals are requested at invocation time (not install time)
2. Prompts happen as a batch per lifecycle phase
3. Existing legacy .approval files are ignored and do not grant execution
4. Install-time UI disclosure relies on schema-provided command metadata
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import (
    APPROVAL_MODEL_VERSION,
    JsonSparkPlug,
    RuntimeApprovalRequiredError,
)


@pytest.fixture
def temp_plugin_dir(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    return plugin_dir


def make_manifest(plugin_id="test-plugin", commands=None, actions=None):
    if commands is None:
        commands = []

    manifest = {
        "version": "1.0",
        "metadata": {
            "id": plugin_id,
            "name": "Test Plugin",
        },
        "requires": {
            "commands": commands,
        },
    }

    if actions:
        manifest["actions"] = actions

    return manifest


def write_manifest(plugin_dir: Path, plugin_id: str, manifest: dict) -> Path:
    manifest_file = plugin_dir / f"{plugin_id}.json"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return manifest_file


def write_legacy_approval(plugin_dir: Path, plugin_id: str, approved_commands: list[str]) -> Path:
    approval_file = plugin_dir / f".{plugin_id}.approval"
    with open(approval_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "plugin_id": plugin_id,
                "approved_commands": approved_commands,
            },
            f,
            indent=2,
        )
    return approval_file


# ---------------------------------------------------------------------------
# Baseline behavior for invocation-time migration
# ---------------------------------------------------------------------------

@patch("shutil.which")
def test_plugin_loads_with_no_approval_file(mock_which, temp_plugin_dir):
    mock_which.return_value = "/usr/bin/some-tool"

    manifest = make_manifest(
        plugin_id="minimal",
        commands=[{"name": "some-tool", "allow_plugin_specific": True}],
    )
    manifest_file = write_manifest(temp_plugin_dir, "minimal", manifest)

    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == set()


@patch("subprocess.run")
@patch("shutil.which")
def test_legacy_approval_file_is_ignored_and_reprompt_required(
    mock_which, mock_run, temp_plugin_dir
):
    """Security reset contract: legacy approvals should not auto-authorize execution."""
    mock_which.return_value = "/usr/bin/proxmox-auto-install-assistant"
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

    manifest = make_manifest(
        plugin_id="legacy-reset",
        commands=[{"name": "proxmox-auto-install-assistant", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-assistant",
                    "type": "run_command",
                    "command": ["proxmox-auto-install-assistant", "--help"],
                }
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "legacy-reset", manifest)
    write_legacy_approval(temp_plugin_dir, "legacy-reset", ["proxmox-auto-install-assistant"])

    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]

    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(action, ui_values={}, preset={}, device_path="/dev/sdb")

    msg = str(exc_info.value).lower()
    assert "runtime approval" in msg
    assert "reinstall" not in msg


@patch("shutil.which")
def test_unapproved_command_error_requests_runtime_approval_not_reinstall(
    mock_which, temp_plugin_dir
):
    """Runtime gating message should direct user to approve now, not reinstall."""
    mock_which.return_value = "/usr/bin/some-tool"

    manifest = make_manifest(
        plugin_id="runtime-approval-msg",
        commands=[{"name": "some-tool", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-tool",
                    "type": "run_command",
                    "command": ["some-tool"],
                }
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "runtime-approval-msg", manifest)

    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]

    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(action, ui_values={}, preset={})

    msg = str(exc_info.value).lower()
    assert "runtime approval" in msg
    assert "reinstall" not in msg


@patch("shutil.which")
def test_on_iso_ready_requires_batch_phase_approval_context(mock_which, temp_plugin_dir):
    """First denial in a phase should report the full batch of phase commands."""
    mock_which.return_value = "/usr/bin/tool"

    manifest = make_manifest(
        plugin_id="batch-iso",
        commands=[
            {"name": "cmd-a", "allow_plugin_specific": True},
            {"name": "cmd-b", "allow_plugin_specific": True},
        ],
        actions={
            "on_iso_ready": [
                {"id": "step-a", "type": "run_command", "command": ["cmd-a", "--x"]},
                {"id": "step-b", "type": "run_command", "command": ["cmd-b", "--y"]},
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "batch-iso", manifest)

    plugin = JsonSparkPlug(str(manifest_file))

    with pytest.raises(RuntimeError) as exc_info:
        plugin.on_iso_ready(
            iso_path="/tmp/test.iso",
            preset={"id": "demo", "name": "Demo"},
            ui_values={},
        )

    msg = str(exc_info.value)
    assert "phase" in msg.lower()
    assert "cmd-a" in msg
    assert "cmd-b" in msg


@patch("shutil.which")
def test_runtime_approval_error_exposes_structured_phase_data(mock_which, temp_plugin_dir):
    mock_which.return_value = "/usr/bin/tool"

    manifest = make_manifest(
        plugin_id="structured-approval",
        commands=[{"name": "cmd-a", "allow_plugin_specific": True}],
        actions={
            "on_iso_ready": [
                {"id": "step-a", "type": "run_command", "command": ["cmd-a", "--x"]},
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "structured-approval", manifest)

    plugin = JsonSparkPlug(str(manifest_file))

    with pytest.raises(RuntimeApprovalRequiredError) as exc_info:
        plugin.on_iso_ready(
            iso_path="/tmp/test.iso",
            preset={"id": "demo", "name": "Demo"},
            ui_values={},
        )

    err = exc_info.value
    assert err.plugin_id == "structured-approval"
    assert err.pending.phase_name == "on_iso_ready"
    assert err.pending.commands == ["cmd-a"]


def test_runtime_approval_is_persisted_and_reloaded_in_user_state(temp_plugin_dir, monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_dir))

    manifest = make_manifest(plugin_id="persist-approval")
    manifest_file = write_manifest(temp_plugin_dir, "persist-approval", manifest)

    plugin = JsonSparkPlug(str(manifest_file))
    plugin.approve_runtime_commands(["cmd-a", "cmd-b", "cmd-a"])

    approval_file = state_dir / "spark-writer" / "approvals" / ".persist-approval.approval"
    assert approval_file.exists()

    with open(approval_file, "r", encoding="utf-8") as f:
        approval_data = json.load(f)

    assert approval_data["plugin_id"] == "persist-approval"
    assert approval_data["approval_model"] == APPROVAL_MODEL_VERSION
    assert sorted(approval_data["approved_commands"]) == ["cmd-a", "cmd-b"]

    reloaded = JsonSparkPlug(str(manifest_file))
    assert reloaded._plugin_allowed_commands == {"cmd-a", "cmd-b"}


@patch("shutil.which")
def test_get_pending_phase_approval_returns_commands_for_preflight(mock_which, temp_plugin_dir):
    mock_which.return_value = "/usr/bin/tool"

    manifest = make_manifest(
        plugin_id="preflight-check",
        commands=[{"name": "cmd-a", "allow_plugin_specific": True}],
        actions={
            "on_iso_ready": [
                {"id": "step-a", "type": "run_command", "command": ["cmd-a", "--x"]},
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "preflight-check", manifest)

    plugin = JsonSparkPlug(str(manifest_file))
    pending = plugin.get_pending_phase_approval("on_iso_ready")

    assert pending is not None
    assert pending.phase_name == "on_iso_ready"
    assert pending.commands == ["cmd-a"]


def test_post_write_only_plugin_still_supports_local_iso_save(temp_plugin_dir):
    manifest = make_manifest(
        plugin_id="local-save-only",
        actions={
            "on_write_complete": [
                {
                    "id": "write-marker",
                    "type": "write_partition_files",
                    "partition_label": "CIDATA",
                    "files": {
                        "meta-data": "instance-id: demo\n"
                    },
                }
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "local-save-only", manifest)

    plugin = JsonSparkPlug(str(manifest_file))

    assert plugin.requires_processing() is False
    assert plugin.supports_save_iso() is True


def test_built_in_ubuntu_live_persistence_plugin_supports_local_iso_save(
    ubuntu_live_persistence_plugin,
):
    assert ubuntu_live_persistence_plugin.requires_processing() is False
    assert ubuntu_live_persistence_plugin.supports_save_iso() is True


def test_built_in_ubuntu_live_persistence_plugin_has_no_runtime_command_approval(
    ubuntu_live_persistence_plugin,
):
    pending = ubuntu_live_persistence_plugin.get_pending_phase_approval("on_write_complete")

    assert pending is None


def test_proxmox_wrapper_participates_in_phase_approval(proxmox_plugin):
    pending = proxmox_plugin.get_pending_phase_approval("on_iso_ready")

    assert pending is not None
    assert pending.phase_name == "on_iso_ready"
    assert pending.commands == ["proxmox-auto-install-assistant"]


def test_missing_template_variable_error_identifies_variable_name(temp_plugin_dir):
    manifest = make_manifest(plugin_id="template-errors")
    manifest_file = write_manifest(temp_plugin_dir, "template-errors", manifest)

    plugin = JsonSparkPlug(str(manifest_file))
    action = {
        "id": "store-secret",
        "type": "store_ephemeral_secret",
        "key": "root-password",
        "value": "{{_generated_root_password_plaintext}}",
    }

    with pytest.raises(ValueError) as exc_info:
        plugin._execute_action(action, ui_values={}, preset={"id": "demo", "name": "Demo"})

    msg = str(exc_info.value)
    assert msg == "'_generated_root_password_plaintext' is undefined"


@patch("shutil.which")
def test_on_write_complete_requires_batch_phase_approval_context(mock_which, temp_plugin_dir):
    """Write-complete phase should surface all pending command approvals at once."""
    mock_which.return_value = "/usr/bin/tool"

    manifest = make_manifest(
        plugin_id="batch-write",
        commands=[
            {"name": "cmd-a", "allow_plugin_specific": True},
            {"name": "cmd-b", "allow_plugin_specific": True},
        ],
        actions={
            "on_write_complete": [
                {"id": "step-a", "type": "run_command", "command": ["cmd-a", "--x"]},
                {"id": "step-b", "type": "run_command", "command": ["cmd-b", "--y"]},
            ]
        },
    )
    manifest_file = write_manifest(temp_plugin_dir, "batch-write", manifest)

    plugin = JsonSparkPlug(str(manifest_file))

    with pytest.raises(RuntimeError) as exc_info:
        plugin.on_write_complete(
            device_path="/dev/sdb",
            preset={"id": "demo", "name": "Demo"},
            ui_values={},
        )

    msg = str(exc_info.value)
    assert "phase" in msg.lower()
    assert "cmd-a" in msg
    assert "cmd-b" in msg


# ---------------------------------------------------------------------------
# Schema + install-time disclosure contract
# ---------------------------------------------------------------------------

def test_schema_requires_install_disclosure_fields_for_commands():
    """Every command should require enough metadata for install-time user disclosure."""
    schema_path = (
        PACKAGE_ROOT
        / "src"
        / "spark_writer"
        / "plugins"
        / "schema"
        / "sparkplug_manifest.schema.json"
    )

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    command_schema = schema["properties"]["requires"]["properties"]["commands"]["items"]
    required = set(command_schema.get("required", []))

    assert "name" in required
    assert "description" in required
    assert "install_hint" in required
    assert "allow_plugin_specific" in required


def test_schema_declares_wizard_pages():
    schema_path = (
        PACKAGE_ROOT
        / "src"
        / "spark_writer"
        / "plugins"
        / "schema"
        / "sparkplug_manifest.schema.json"
    )

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    wizard = schema["properties"]["wizard"]
    page = schema["definitions"]["wizard_page"]

    assert "pages" in wizard["properties"]
    assert {"id", "title", "fields"}.issubset(set(page["required"]))


def test_manifest_commands_include_disclosure_metadata(proxmox_manifest):
    """Install-time command warning UI needs command description and install guidance."""
    commands = proxmox_manifest.get("requires", {}).get("commands", [])

    assert commands, "Expected at least one command requirement"
    for cmd in commands:
        assert cmd.get("name")
        assert cmd.get("description")
        assert cmd.get("install_hint")
