"""Test manifest command approval and execution security.

This test suite validates that:
1. Plugin-specific commands require user approval before execution
2. Approved commands execute as expected
3. Unapproved commands are blocked with clear error messages
4. Approval state persists across plugin reloads
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add src to path for imports
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import JsonSparkPlug


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_plugin_dir(tmp_path):
    """Create a temporary directory for plugin manifests and approval files."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    return plugin_dir


def make_manifest(
    plugin_id="test-plugin",
    plugin_name="Test Plugin",
    commands=None,
    actions=None,
    **overrides
):
    """Factory for creating valid test manifests.
    
    Args:
        plugin_id: Unique plugin identifier
        plugin_name: Display name
        commands: List of command specs (each with 'name' and optional 'allow_plugin_specific')
        actions: List of action objects
        **overrides: Additional top-level manifest fields
    
    Returns:
        Dict representing a valid SparkPlug v1.0 manifest
    """
    if commands is None:
        commands = []
    
    manifest = {
        "version": "1.0",
        "metadata": {
            "id": plugin_id,
            "name": plugin_name,
        },
        "requires": {
            "commands": commands
        }
    }
    
    if actions:
        manifest["actions"] = actions
    
    manifest.update(overrides)
    return manifest


def write_approval(plugin_dir, plugin_id, approved_commands):
    """Write an approval file for a plugin.
    
    Args:
        plugin_dir: Directory containing plugins
        plugin_id: Plugin ID
        approved_commands: List of approved command names
    """
    approval_file = plugin_dir / f".{plugin_id}.approval"
    approval_data = {
        "plugin_id": plugin_id,
        "approved_commands": approved_commands
    }
    with open(approval_file, "w") as f:
        json.dump(approval_data, f)


# ============================================================================
# Tests: Basic Approval Logic
# ============================================================================

def test_plugin_loads_with_no_approval_file(temp_plugin_dir):
    """Plugin can load even if no approval file exists (backward compatibility)."""
    manifest = make_manifest(
        plugin_id="minimal",
        commands=[]
    )
    
    manifest_file = temp_plugin_dir / "minimal.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == set()


@patch("shutil.which")
def test_plugin_loads_and_reads_approval_file(mock_which, temp_plugin_dir):
    """Plugin correctly reads and loads approved commands from approval file."""
    mock_which.return_value = "/usr/bin/tool"  # Pretend all commands exist
    
    manifest = make_manifest(
        plugin_id="with-approval",
        commands=[
            {"name": "mkpasswd", "allow_plugin_specific": True},
            {"name": "custom-tool", "allow_plugin_specific": True},
        ]
    )
    
    manifest_file = temp_plugin_dir / "with-approval.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Write approval file with user's approved commands
    write_approval(temp_plugin_dir, "with-approval", ["mkpasswd", "custom-tool"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == {"mkpasswd", "custom-tool"}


@patch("shutil.which")
def test_plugin_approval_file_not_found_logs_debug(mock_which, temp_plugin_dir, caplog):
    """Missing approval file doesn't crash; plugin loads with empty approval set."""
    mock_which.return_value = "/usr/bin/tool"
    
    manifest = make_manifest(
        plugin_id="no-approval-file",
        commands=[{"name": "some-tool", "allow_plugin_specific": True}]
    )
    
    manifest_file = temp_plugin_dir / "no-approval-file.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Don't write approval file
    
    with caplog.at_level("DEBUG"):
        plugin = JsonSparkPlug(str(manifest_file))
    
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == set()
    # Check that debug log mentions no approval file
    assert any("No approval file found" in record.message for record in caplog.records)


def test_plugin_corrupted_approval_file_logs_error(temp_plugin_dir, caplog):
    """Corrupted approval file is skipped; plugin still loads."""
    manifest = make_manifest(plugin_id="bad-approval")
    
    manifest_file = temp_plugin_dir / "bad-approval.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Write corrupted approval file
    approval_file = temp_plugin_dir / ".bad-approval.approval"
    with open(approval_file, "w") as f:
        f.write("not valid json {")
    
    with caplog.at_level("ERROR"):
        plugin = JsonSparkPlug(str(manifest_file))
    
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == set()
    # Check that error log mentions failure to load
    assert any("Failed to load approval file" in record.message for record in caplog.records)


# ============================================================================
# Tests: Command Execution with Approval
# ============================================================================

@patch("subprocess.run")
@patch("shutil.which")
def test_run_approved_command_succeeds(mock_which, mock_run, temp_plugin_dir):
    """Execution of approved command succeeds."""
    mock_which.return_value = "/usr/bin/mkpasswd"
    mock_run.return_value = MagicMock(returncode=0, stdout="hashed_password", stderr="")
    
    manifest = make_manifest(
        plugin_id="passwd-plugin",
        commands=[{"name": "mkpasswd", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "hash-password",
                    "type": "run_command",
                    "command": ["mkpasswd", "-m", "sha-512", "test"],
                    "output_var": "password_hash"
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "passwd-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Write approval
    write_approval(temp_plugin_dir, "passwd-plugin", ["mkpasswd"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    
    # Execute on_write_complete actions
    action = manifest.get("actions", {}).get("on_write_complete", [])[0]
    plugin._execute_action(
        action,
        ui_values={},
        preset={"name": "Ubuntu"},
        device_path="/dev/sdb"
    )
    
    # Verify subprocess was called
    mock_run.assert_called_once()


@patch("shutil.which")
def test_run_unapproved_command_raises_error(mock_which, temp_plugin_dir):
    """Execution of unapproved command raises RuntimeError."""
    mock_which.return_value = "/usr/bin/proxmox-auto-install-assistant"
    
    manifest = make_manifest(
        plugin_id="proxmox-plugin",
        commands=[{"name": "proxmox-auto-install-assistant", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-proxmox",
                    "type": "run_command",
                    "command": ["proxmox-auto-install-assistant", "--help"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "proxmox-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Don't write approval file — command should not be approved
    
    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    
    # Try to execute action
    action = manifest["actions"]["on_write_complete"][0]
    
    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(
            action,
            ui_values={},
            preset={},
            device_path="/dev/sdb"
        )
    
    assert "not allowed" in str(exc_info.value).lower()
    assert "proxmox-auto-install-assistant" in str(exc_info.value)


@patch("shutil.which")
def test_run_unapproved_command_with_helpful_error_message(mock_which, temp_plugin_dir):
    """Unapproved command error lists approved commands if any exist."""
    mock_which.return_value = "/usr/bin/unauthorized"
    
    manifest = make_manifest(
        plugin_id="multi-cmd-plugin",
        commands=[
            {"name": "cmd1", "allow_plugin_specific": True},
            {"name": "cmd2", "allow_plugin_specific": True},
            {"name": "unauthorized", "allow_plugin_specific": True},
        ],
        actions={
            "on_write_complete": [
                {
                    "id": "run-unauthorized",
                    "type": "run_command",
                    "command": ["unauthorized"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "multi-cmd-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Approve only cmd1 and cmd2, not unauthorized
    write_approval(temp_plugin_dir, "multi-cmd-plugin", ["cmd1", "cmd2"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]
    
    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(
            action,
            ui_values={},
            preset={}
        )
    
    error_msg = str(exc_info.value)
    assert "unauthorized" in error_msg.lower()
    assert "cmd1" in error_msg
    assert "cmd2" in error_msg
    assert "Approved commands" in error_msg


@patch("shutil.which")
def test_run_unapproved_command_without_prior_approvals(mock_which, temp_plugin_dir):
    """Unapproved command error when no approvals exist is helpful too."""
    mock_which.return_value = "/usr/bin/some-tool"
    
    manifest = make_manifest(
        plugin_id="no-approvals-plugin",
        commands=[{"name": "some-tool", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-tool",
                    "type": "run_command",
                    "command": ["some-tool"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "no-approvals-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # No approval file written
    
    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]
    
    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(
            action,
            ui_values={},
            preset={}
        )
    
    error_msg = str(exc_info.value)
    assert "not allowed" in error_msg.lower()
    assert "Reinstall the plugin" in error_msg


# ============================================================================
# Tests: Command Not Found vs. Not Approved
# ============================================================================

@patch("shutil.which")
def test_command_not_approved_error_before_path_check(mock_which, temp_plugin_dir):
    """Approval check happens before PATH lookup (better error message)."""
    mock_which.return_value = None  # Command doesn't exist in PATH
    
    manifest = make_manifest(
        plugin_id="missing-cmd-plugin",
        commands=[{"name": "missing-tool", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-missing",
                    "type": "run_command",
                    "command": ["missing-tool"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "missing-cmd-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Don't write approval
    
    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]
    
    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(action, ui_values={}, preset={})
    
    # Should fail on approval, not PATH check
    assert "not allowed" in str(exc_info.value).lower()


@patch("subprocess.run")
@patch("shutil.which")
def test_approved_command_not_in_path_fails_with_clear_error(
    mock_which, mock_run, temp_plugin_dir
):
    """If command is approved but missing from PATH, error is clear."""
    # When we call which() first time for approval check, return True
    # When we call which() again for PATH validation, return False
    mock_which.side_effect = ["/usr/bin/tool", None]
    
    manifest = make_manifest(
        plugin_id="missing-approved-plugin",
        commands=[{"name": "missing-approved-tool", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "run-missing-approved",
                    "type": "run_command",
                    "command": ["missing-approved-tool"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "missing-approved-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    write_approval(temp_plugin_dir, "missing-approved-plugin", ["missing-approved-tool"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]
    
    with pytest.raises(RuntimeError) as exc_info:
        plugin._execute_action(action, ui_values={}, preset={})
    
    # Error should be about PATH, not approval
    assert "not found in PATH" in str(exc_info.value)


# ============================================================================
# Tests: Approval File Persistence
# ============================================================================

def test_approval_persists_across_plugin_reloads(temp_plugin_dir):
    """Approval is maintained when plugin is reloaded."""
    manifest = make_manifest(
        plugin_id="persistent-plugin",
        commands=[{"name": "persistent-cmd", "allow_plugin_specific": True}]
    )
    
    manifest_file = temp_plugin_dir / "persistent-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    write_approval(temp_plugin_dir, "persistent-plugin", ["persistent-cmd"])
    
    # First load
    plugin1 = JsonSparkPlug(str(manifest_file))
    assert "persistent-cmd" in plugin1._plugin_allowed_commands
    
    # Simulate plugin reload
    plugin2 = JsonSparkPlug(str(manifest_file))
    assert "persistent-cmd" in plugin2._plugin_allowed_commands


# ============================================================================
# Tests: allow_plugin_specific Flag
# ============================================================================

def test_command_with_allow_plugin_specific_false_not_in_approval(
    temp_plugin_dir
):
    """Commands with allow_plugin_specific=false should not be in approval."""
    manifest = make_manifest(
        plugin_id="mixed-commands-plugin",
        commands=[
            {"name": "required-tool", "allow_plugin_specific": False},
            {"name": "optional-tool", "allow_plugin_specific": True},
        ]
    )
    
    manifest_file = temp_plugin_dir / "mixed-commands-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # Simulate install: only allow_plugin_specific=true commands get approved
    write_approval(temp_plugin_dir, "mixed-commands-plugin", ["optional-tool"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    assert "optional-tool" in plugin._plugin_allowed_commands
    assert "required-tool" not in plugin._plugin_allowed_commands


# ============================================================================
# Tests: Multiple Commands in Single Action
# ============================================================================

@patch("subprocess.run")
@patch("shutil.which")
def test_piped_commands_only_first_checked_for_approval(
    mock_which, mock_run, temp_plugin_dir
):
    """Command approval checks the executable name (first element)."""
    mock_which.return_value = "/usr/bin/approved-cmd"
    mock_run.return_value = MagicMock(returncode=0, stdout="result", stderr="")
    
    manifest = make_manifest(
        plugin_id="piped-plugin",
        commands=[{"name": "approved-cmd", "allow_plugin_specific": True}],
        actions={
            "on_write_complete": [
                {
                    "id": "piped-action",
                    "type": "run_command",
                    "command": ["approved-cmd", "arg1", "arg2"]
                }
            ]
        }
    )
    
    manifest_file = temp_plugin_dir / "piped-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    write_approval(temp_plugin_dir, "piped-plugin", ["approved-cmd"])
    
    plugin = JsonSparkPlug(str(manifest_file))
    action = manifest["actions"]["on_write_complete"][0]
    
    # Should succeed (first arg is approved)
    plugin._execute_action(action, ui_values={}, preset={})
    mock_run.assert_called_once()


# ============================================================================
# Integration: Empty Manifest (No Commands)
# ============================================================================

def test_manifest_with_no_commands_requires_no_approval(temp_plugin_dir):
    """Plugin with no commands doesn't need an approval file."""
    manifest = make_manifest(
        plugin_id="simple-plugin",
        commands=[]
    )
    
    manifest_file = temp_plugin_dir / "simple-plugin.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)
    
    # No approval file
    plugin = JsonSparkPlug(str(manifest_file))
    assert plugin.is_available is True
    assert plugin._plugin_allowed_commands == set()
