"""Integration tests using real manifest examples.

This test suite exercises the full SparkWriter flow:
1. Load a real manifest (Proxmox Tailscale)
2. Simulate user interaction (config fields, preset selection)
3. Execute the manifest workflow
"""

import sys
from pathlib import Path
from unittest.mock import patch

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

    def test_proxmox_manifest_command_not_preapproved_at_load(self, proxmox_plugin):
        """Invocation-time model should not auto-approve at plugin load."""
        assert proxmox_plugin._plugin_allowed_commands == set()

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

    @patch("shutil.which")
    def test_proxmox_on_write_complete_requires_runtime_approval_message(self, mock_which, proxmox_plugin):
        """Unapproved commands should instruct runtime approval, not reinstall."""
        mock_which.return_value = "/usr/bin/proxmox-auto-install-assistant"

        actions = proxmox_plugin.manifest.get("actions", {}).get("on_write_complete", [])
        run_action = next((a for a in actions if a.get("type") == "run_command"), None)
        if not run_action:
            pytest.skip("Manifest has no run_command actions in on_write_complete")

        with pytest.raises(RuntimeError) as exc_info:
            proxmox_plugin._execute_action(
                action=run_action,
                ui_values={"contact-email": "admin@example.com", "fqdn": "proxmox.example.org"},
                preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
                device_path="/dev/sdb",
            )

        msg = str(exc_info.value)
        assert "runtime approval" in msg.lower()
        assert "reinstall" not in msg.lower()

    def test_generate_ephemeral_password_action_populates_output_var(self, proxmox_plugin):
        """Verify generate_ephemeral_password stores output in action vars."""
        action = {
            "id": "generate_test_password",
            "type": "generate_ephemeral_password",
            "length": 24,
            "output_var": "generated_password",
        }

        result = proxmox_plugin._execute_action(
            action=action,
            ui_values={},
            preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
            iso_path="/tmp/test.iso",
        )

        assert isinstance(result, str)
        assert len(result) == 24
        assert proxmox_plugin._action_vars["generated_password"] == result

    def test_generate_root_password_action_from_manifest_sets_expected_var(self, proxmox_plugin):
        """Verify real manifest action creates _generated_root_password_plaintext."""
        actions = proxmox_plugin.manifest.get("actions", {}).get("on_iso_ready", [])
        generate_action = next(a for a in actions if a.get("id") == "generate_root_password")

        proxmox_plugin._execute_action(
            action=generate_action,
            ui_values={"root-password": ""},
            preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
            iso_path="/tmp/test.iso",
        )

        generated = proxmox_plugin._action_vars.get("_generated_root_password_plaintext")
        assert isinstance(generated, str)
        assert len(generated) == int(generate_action.get("length", 20))

    def test_firstboot_template_renders_without_optional_apt_proxy(self, proxmox_plugin):
        """Optional apt-proxy can be omitted without template rendering failures."""
        actions = proxmox_plugin.manifest.get("actions", {}).get("on_iso_ready", [])
        render_action = next(a for a in actions if a.get("id") == "render_firstboot_script")

        result = proxmox_plugin._execute_action(
            action=render_action,
            ui_values={
                "authkey": "tskey-auth-abc123xyzdef",
                "hostname": "pve-test",
                "tailscale-domain": "",
            },
            preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
            iso_path="/tmp/test.iso",
        )

        assert isinstance(result, str)
        assert "APT_CACHE_URL=" in result


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

    def test_plugin_specific_command_requires_phase_batch_context(self, proxmox_manifest_path, tmp_path):
        """Phase-level approval should include all commands needed in the phase."""
        from spark_writer.plugins.json_plugin import JsonSparkPlug

        temp_manifest = tmp_path / "test-plugin.json"
        temp_manifest.write_text(proxmox_manifest_path.read_text())

        plugin = JsonSparkPlug(str(temp_manifest))

        assert plugin._plugin_allowed_commands == set()

        requires = plugin.manifest.get("requires", {}).get("commands", [])
        declared = {c.get("name") for c in requires if c.get("name")}
        phase_actions = plugin.manifest.get("actions", {}).get("on_write_complete", [])
        phase_commands = {
            a.get("command", [None])[0]
            for a in phase_actions
            if a.get("type") == "run_command" and a.get("command")
        }
        phase_commands.discard(None)

        if not phase_commands:
            pytest.skip("Manifest has no run_command actions in on_write_complete")

        # Batch approval policy: pre-phase prompt should cover every phase command.
        # This test encodes the contract by requiring the failure payload to mention
        # all run_command executables in the current phase when unapproved.
        with pytest.raises(RuntimeError) as exc_info:
            plugin.on_write_complete(
                device_path="/dev/sdb",
                preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
                ui_values={"contact-email": "admin@example.com", "fqdn": "proxmox.example.org"},
            )

        msg = str(exc_info.value)
        for cmd_name in phase_commands:
            assert cmd_name in declared
            assert cmd_name in msg
