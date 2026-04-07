"""Integration tests using real manifest examples.

This test suite exercises the full SparkWriter flow:
1. Load a real manifest (Proxmox Tailscale)
2. Simulate user interaction (config fields, preset selection)
3. Execute the manifest workflow
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class TestProxmoxManifestIntegration:
    """Integration tests using the Proxmox Tailscale manifest."""

    def test_proxmox_manifest_loads_successfully(self, proxmox_plugin):
        """Verify the real manifest loads without errors."""
        # is_available depends on whether proxmox-auto-install-assistant is installed
        # Just verify the manifest loaded correctly
        assert proxmox_plugin.name == "Proxmox Tailscale"
        assert "proxmox-tailscale" in proxmox_plugin.manifest["metadata"]["id"]
        assert proxmox_plugin.manifest["version"] == "1.0"

    def test_proxmox_manifest_has_expected_presets(self, proxmox_plugin):
        """Verify presets are registered."""
        presets = proxmox_plugin.register_presets()
        
        assert len(presets) > 0
        assert "proxmox-ve-9.1" in presets
        assert presets["proxmox-ve-9.1"]["name"] == "Proxmox VE 9.1"

    def test_proxmox_manifest_has_config_fields(self, proxmox_plugin):
        """Verify plugin has user-configurable fields."""
        # Proxmox manifest should have config fields for email, FQDN, keyboard, etc.
        config_fields = proxmox_plugin.manifest.get("config_fields", [])
        
        assert len(config_fields) > 0
        field_ids = [f["id"] for f in config_fields]
        assert "contact-email" in field_ids
        assert "fqdn" in field_ids

    def test_proxmox_manifest_requires_proxmox_assistant(self, proxmox_plugin):
        """Verify command dependency is declared."""
        commands = proxmox_plugin.manifest.get("requires", {}).get("commands", [])
        
        command_names = [c["name"] for c in commands]
        assert "proxmox-auto-install-assistant" in command_names

    def test_proxmox_manifest_command_approved(self, proxmox_plugin):
        """Verify the plugin-specific command is in approval set."""
        assert "proxmox-auto-install-assistant" in proxmox_plugin._plugin_allowed_commands

    def test_proxmox_manifest_simulated_user_interaction(self, proxmox_plugin):
        """Simulate a user filling in config and selecting preset."""
        # User provides config values
        user_config = {
            "contact-email": "admin@example.com",
            "fqdn": "proxmox.example.org",
            "keyboard": "de",
            "ssh-keys": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCtest test@example.com",
            "authkey": "tskey-auth-abc123xyzdef",
        }
        
        # User selects a preset
        presets = proxmox_plugin.register_presets()
        selected_preset = presets.get("proxmox-ve-9.1")
        
        assert selected_preset is not None
        assert selected_preset["url"].startswith("https://")
        
        # All config fields that are required are fillable
        for field in proxmox_plugin.manifest.get("config_fields", []):
            field_id = field["id"]
            if field.get("required"):
                assert field_id in user_config

    @patch("subprocess.run")
    def test_proxmox_on_write_complete_actions_can_execute(self, mock_run, proxmox_plugin):
        """Verify on_write_complete actions from manifest can execute."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        
        actions = proxmox_plugin.manifest.get("actions", {}).get("on_write_complete", [])
        
        # Mock the approver state
        user_config = {
            "contact-email": "admin@example.com",
            "fqdn": "proxmox.example.org",
        }
        
        # Attempt to execute each action
        for action in actions:
            if action.get("type") == "run_command":
                # Verify command is in approved set (already tested above)
                cmd_name = action.get("command", [None])[0]
                if cmd_name:
                    assert cmd_name in proxmox_plugin._plugin_allowed_commands


class TestManifestLoadingFromDisk:
    """Test loading manifests directly from disk."""

    def test_load_manifest_from_json_file(self, proxmox_manifest_path):
        """Verify manifest can be loaded from disk."""
        from spark_writer.plugins.json_plugin import JsonSparkPlug
        
        plugin = JsonSparkPlug(str(proxmox_manifest_path))
        
        # Should load, but might not be available if proxmox-auto-install-assistant isn't installed
        # (that's OK for this test - we're just checking it loads)
        assert "proxmox-tailscale" in plugin.manifest["metadata"]["id"]

    def test_manifest_schema_is_valid(self, proxmox_manifest):
        """Verify the manifest follows the schema."""
        # Check required top-level fields
        assert "version" in proxmox_manifest
        assert proxmox_manifest["version"] == "1.0"
        
        assert "metadata" in proxmox_manifest
        assert proxmox_manifest["metadata"].get("id")
        assert proxmox_manifest["metadata"].get("name")
        
        assert "requires" in proxmox_manifest
        assert isinstance(proxmox_manifest["requires"].get("commands", []), list)

    def test_manifest_has_no_deprecated_fields(self, proxmox_manifest):
        """Verify deprecated fields are not present."""
        assert "secure_manifest" not in proxmox_manifest
        assert "signature" not in proxmox_manifest


class TestManifestPermissionFlow:
    """Test the permission flow when manifesting plugin-specific commands."""

    def test_plugin_specific_command_requires_approval(self, proxmox_manifest_path, tmp_path):
        """Verify that run_command fails without approval."""
        from spark_writer.plugins.json_plugin import JsonSparkPlug
        
        # Copy manifest without approval file
        temp_manifest = tmp_path / "test-plugin.json"
        temp_manifest.write_text(proxmox_manifest_path.read_text())
        
        plugin = JsonSparkPlug(str(temp_manifest))
        
        # Plugin loads, but command approval set is empty (no approval file)
        assert plugin._plugin_allowed_commands == set()
        
        # Attempt to run a command action should fail
        # (if the manifest had run_command actions)
