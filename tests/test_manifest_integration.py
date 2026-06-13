"""Integration tests using real manifest examples.

This test suite exercises the full SparkWriter flow:
1. Load a real manifest (Proxmox Tailscale)
2. Simulate user interaction (config fields, preset selection)
3. Execute the manifest workflow
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import JsonSparkPlug


class TestProxmoxManifestIntegration:
    """Integration tests using the Proxmox Tailscale manifest."""

    def test_proxmox_manifest_loads_successfully(self, proxmox_plugin):
        """Verify the real manifest loads without errors."""
        # is_available depends on whether proxmox-auto-install-assistant is installed
        # Just verify the manifest loaded correctly
        assert proxmox_plugin.name == "Proxmox Tailscale"
        assert "proxmox-tailscale" in proxmox_plugin.manifest["metadata"]["id"]
        assert proxmox_plugin.manifest["version"] == "1.6"

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

    def test_proxmox_manifest_uses_container_builder(self, proxmox_plugin):
        commands = proxmox_plugin.manifest.get("requires", {}).get("commands", [])
        assert commands == []
        action = next(
            item
            for item in proxmox_plugin.manifest["actions"]["on_iso_ready"]
            if item["type"] == "run_builder"
        )
        assert action["builder_id"] == "proxmox-auto-install"

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

    def test_create_artifact_stores_metadata(self, proxmox_plugin):
        action = {
            "id": "create_test_artifact",
            "type": "create_artifact",
            "artifact_id": "answer_toml",
            "content": "hello",
            "kind": "config",
            "logical_name": "answer.toml",
            "media_type": "application/toml",
        }

        result = proxmox_plugin._execute_action(
            action=action,
            ui_values={},
            preset={"id": "proxmox-ve-9.1", "name": "Proxmox VE 9.1"},
            iso_path="/tmp/test.iso",
        )

        assert result is None
        artifact = proxmox_plugin._exec_ctx.artifacts["answer_toml"]
        assert artifact.content == "hello"
        assert artifact.kind == "config"
        assert artifact.logical_name == "answer.toml"
        assert artifact.media_type == "application/toml"

    def test_phase_cleanup_clears_artifacts_after_failure(self, proxmox_plugin):
        proxmox_plugin.manifest["actions"] = {
            "on_iso_ready": [
                {
                    "id": "create_artifact",
                    "type": "create_artifact",
                    "artifact_id": "temp_artifact",
                    "content": "hello",
                    "kind": "config",
                    "logical_name": "hello.txt",
                },
                {
                    "id": "missing_artifact",
                    "type": "prepare_installer_iso",
                    "installer_scheme": "ubuntu-nocloud",
                    "iso_path": "{{iso_path}}",
                    "output_path": "{{iso_path}}",
                    "artifact_map": {
                        "user-data": "missing",
                        "meta-data": "missing",
                    },
                },
            ]
        }

        with pytest.raises(RuntimeError):
            proxmox_plugin.on_iso_ready(
                iso_path="/tmp/test.iso",
                preset={"id": "demo", "name": "Demo"},
                ui_values={},
            )

        assert proxmox_plugin._exec_ctx.artifacts == {}
        assert proxmox_plugin._exec_ctx.action_vars == {}

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
        assert proxmox_plugin._exec_ctx.action_vars["generated_password"] == result

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

        generated = proxmox_plugin._exec_ctx.action_vars.get("_generated_root_password_plaintext")
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


class TestUbuntuLivePersistenceManifestIntegration:
    """Integration tests for the built-in Ubuntu live persistence manifest."""

    def test_manifest_loads_successfully(self, ubuntu_live_persistence_plugin):
        assert ubuntu_live_persistence_plugin.name == "Ubuntu Live Persistence"
        assert ubuntu_live_persistence_plugin.manifest["metadata"]["id"] == "ubuntu-live-persistence"
        assert ubuntu_live_persistence_plugin.manifest["version"] == "1.6"

    def test_manifest_is_post_write_only(self, ubuntu_live_persistence_plugin):
        assert ubuntu_live_persistence_plugin.requires_processing() is False
        assert ubuntu_live_persistence_plugin.supports_save_iso() is True
        assert ubuntu_live_persistence_plugin.manifest.get("actions", {}).get("on_iso_ready", []) == []

    def test_manifest_is_visible_for_ubuntu_presets(self, ubuntu_live_persistence_plugin):
        assert ubuntu_live_persistence_plugin.should_show_ui(
            "ubuntu-24.04-desktop",
            {"id": "ubuntu-24.04-desktop", "name": "Ubuntu 24.04 Desktop", "distro": "ubuntu"},
        ) is True
        assert ubuntu_live_persistence_plugin.should_show_ui(
            "debian-live-12",
            {"id": "debian-live-12", "name": "Debian Live 12", "distro": "debian"},
        ) is False

    def test_manifest_has_expected_post_write_actions(self, ubuntu_live_persistence_manifest):
        actions = ubuntu_live_persistence_manifest.get("actions", {}).get("on_write_complete", [])

        assert len(actions) == 2
        assert actions[0]["type"] == "create_partition"
        assert actions[0]["label"] == "writable"
        assert actions[0]["size_mb"] == 4096
        assert actions[0]["skip_if_exists"] is False

        assert actions[1]["type"] == "write_partition_files"
        assert actions[1]["partition_label"] == "writable"
        assert actions[1]["files"] == {"persistence.conf": "/\n"}

    @patch("spark_writer.plugins.json_plugin.usb_writer.create_aux_partition")
    @patch("spark_writer.plugins.json_plugin.usb_writer.write_files_to_partition")
    def test_on_write_complete_creates_writable_partition_and_marker(
        self,
        mock_write_files,
        mock_create_partition,
        ubuntu_live_persistence_plugin,
    ):
        ubuntu_live_persistence_plugin.on_write_complete(
            device_path="/dev/sdb",
            preset={"id": "ubuntu-24.04-desktop", "name": "Ubuntu 24.04 Desktop", "distro": "ubuntu"},
            ui_values={},
        )

        mock_create_partition.assert_called_once_with(
            "/dev/sdb",
            "writable",
            size_mb=4096,
            partition_type="0700",
        )
        mock_write_files.assert_called_once_with("/dev/sdb", "writable", {"persistence.conf": "/"})


class TestUbuntuAutoinstallManifestIntegration:
    """Integration tests for the built-in Ubuntu autoinstall manifest."""

    def test_manifest_loads_successfully(self, ubuntu_autoinstall_plugin):
        assert ubuntu_autoinstall_plugin.name == "Ubuntu Autoinstall"
        assert ubuntu_autoinstall_plugin.manifest["metadata"]["id"] == "ubuntu-autoinstall"
        assert ubuntu_autoinstall_plugin.manifest["version"] == "1.6"

    def test_manifest_uses_host_owned_nocloud_wrapper(self, ubuntu_autoinstall_manifest):
        actions = ubuntu_autoinstall_manifest["actions"]["on_iso_ready"]
        assert any(action["type"] == "create_artifact" for action in actions)
        assert any(
            action["type"] == "prepare_installer_iso"
            and action["installer_scheme"] == "ubuntu-nocloud"
            for action in actions
        )
        assert all(action["type"] != "modify_iso" for action in actions)
        assert all(action["type"] != "prepare_ubuntu_nocloud_iso" for action in actions)

    def test_builtin_manifest_installer_actions_conform_to_schema(
        self,
        ubuntu_autoinstall_manifest,
        proxmox_manifest,
    ):
        import json

        schema_path = (
            SRC_ROOT
            / "spark_writer"
            / "plugins"
            / "schema"
            / "sparkplug_manifest.schema.json"
        )
        with schema_path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)

        allowed_action_types = set(
            schema["definitions"]["action"]["properties"]["type"]["enum"]
        )
        action_properties = set(schema["definitions"]["action"]["properties"])
        retired_scheme_fields = {
            "answer_artifact",
            "firstboot_artifact",
            "user_data_artifact",
            "meta_data_artifact",
            "volume_label",
        }
        manifests = [ubuntu_autoinstall_manifest, proxmox_manifest]
        manifest_actions = [
            action
            for manifest in manifests
            for phase_actions in manifest.get("actions", {}).values()
            for action in phase_actions
        ]
        manifest_action_types = {action["type"] for action in manifest_actions}
        manifest_action_fields = {
            field for action in manifest_actions for field in action
        }

        assert manifest_action_types <= allowed_action_types
        assert retired_scheme_fields.isdisjoint(action_properties)
        assert retired_scheme_fields.isdisjoint(manifest_action_fields)
        assert not any(
            action_type.startswith("prepare_ubuntu_")
            or action_type.startswith("prepare_proxmox_")
            for action_type in allowed_action_types
        )

    @patch("spark_writer.plugins.installer_schemes.inject_cloud_init_nocloud")
    def test_ubuntu_wrapper_uses_artifacts(self, mock_inject, ubuntu_autoinstall_plugin):
        mock_inject.return_value = "/tmp/ubuntu.iso"

        ubuntu_autoinstall_plugin._execute_action(
            action={
                "id": "create_user_data_artifact",
                "type": "create_artifact",
                "artifact_id": "user_data",
                "content": "#cloud-config\n",
                "kind": "cloud_init",
                "logical_name": "user-data",
            },
            ui_values={},
            preset={"id": "ubuntu-24.04-server", "name": "Ubuntu 24.04"},
            iso_path="/tmp/ubuntu.iso",
        )
        ubuntu_autoinstall_plugin._execute_action(
            action={
                "id": "create_meta_data_artifact",
                "type": "create_artifact",
                "artifact_id": "meta_data",
                "content": "instance-id: demo\n",
                "kind": "cloud_init",
                "logical_name": "meta-data",
            },
            ui_values={},
            preset={"id": "ubuntu-24.04-server", "name": "Ubuntu 24.04"},
            iso_path="/tmp/ubuntu.iso",
        )

        result = ubuntu_autoinstall_plugin._execute_action(
            action={
                "id": "inject_cloud_init",
                "type": "prepare_installer_iso",
                "installer_scheme": "ubuntu-nocloud",
                "iso_path": "{{iso_path}}",
                "output_path": "{{iso_path}}",
                "artifact_map": {
                    "user-data": "user_data",
                    "meta-data": "meta_data",
                },
                "options": {
                    "volume_label": "Ubuntu_Auto",
                },
            },
            ui_values={},
            preset={"id": "ubuntu-24.04-server", "name": "Ubuntu 24.04"},
            iso_path="/tmp/ubuntu.iso",
        )

        assert result == "/tmp/ubuntu.iso"
        mock_inject.assert_called_once_with(
            iso_path="/tmp/ubuntu.iso",
            user_data="#cloud-config",
            meta_data="instance-id: demo",
            output_path="/tmp/ubuntu.iso",
            volume_label="Ubuntu_Auto",
        )


class TestManifestLoadingFromDisk:
    """Test loading manifests directly from disk."""

    def test_load_manifest_from_json_file(self, proxmox_manifest_path, monkeypatch, tmp_path):
        """Verify manifest can be loaded from disk."""
        from spark_writer.plugins.json_plugin import JsonSparkPlug

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        
        plugin = JsonSparkPlug(str(proxmox_manifest_path))
        
        # Should load, but might not be available if proxmox-auto-install-assistant isn't installed
        # (that's OK for this test - we're just checking it loads)
        assert "proxmox-tailscale" in plugin.manifest["metadata"]["id"]

    def test_manifest_schema_is_valid(self, proxmox_manifest):
        """Verify the manifest follows the schema."""
        # Check required top-level fields
        assert "version" in proxmox_manifest
        assert proxmox_manifest["version"] == "1.6"
        
        assert "metadata" in proxmox_manifest
        assert proxmox_manifest["metadata"].get("id")
        assert proxmox_manifest["metadata"].get("name")
        
        assert "requires" in proxmox_manifest
        assert isinstance(proxmox_manifest["requires"].get("commands", []), list)

    def test_manifest_has_no_deprecated_fields(self, proxmox_manifest):
        """Verify deprecated fields are not present."""
        assert "secure_manifest" not in proxmox_manifest
        assert "signature" not in proxmox_manifest

    def test_manifest_16_rejects_legacy_proxmox_host_action(self, tmp_path):
        manifest = {
            "version": "1.6",
            "metadata": {"id": "legacy-proxmox", "name": "Legacy Proxmox"},
            "requires": {"commands": []},
            "actions": {
                "on_iso_ready": [
                    {
                        "id": "legacy",
                        "type": "prepare_installer_iso",
                        "installer_scheme": "proxmox-auto-install",
                        "iso_path": "{{iso_path}}",
                        "output_path": "{{iso_path}}",
                    }
                ]
            },
        }
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        plugin = JsonSparkPlug(str(path))
        assert plugin.is_available is False
        assert "must use run_builder" in (plugin.unavailable_reason or "")

    def test_ubuntu_live_persistence_manifest_has_expected_shape(self, ubuntu_live_persistence_manifest):
        assert ubuntu_live_persistence_manifest["version"] == "1.6"
        assert ubuntu_live_persistence_manifest["metadata"]["id"] == "ubuntu-live-persistence"
        assert ubuntu_live_persistence_manifest["requires"]["commands"] == []
        assert ubuntu_live_persistence_manifest.get("config_fields", []) == []
        assert ubuntu_live_persistence_manifest["ui_visibility"]["when"]["preset_distro"] == ["ubuntu"]


class TestManifestPermissionFlow:
    """Test the permission flow when manifesting plugin-specific commands."""

    def test_plugin_specific_command_requires_phase_batch_context(self, proxmox_manifest_path, tmp_path, monkeypatch):
        """Phase-level approval should include all commands needed in the phase."""
        from spark_writer.plugins.json_plugin import JsonSparkPlug

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

        temp_manifest = tmp_path / "test-plugin.json"
        temp_manifest.write_text(proxmox_manifest_path.read_text())
        sidecar = proxmox_manifest_path.with_name("proxmox-tailscale.firstboot.sh")
        (tmp_path / sidecar.name).write_text(sidecar.read_text(encoding="utf-8"), encoding="utf-8")

        plugin = JsonSparkPlug(str(temp_manifest))

        assert plugin._plugin_allowed_commands == set()

        requires = plugin.manifest.get("requires", {}).get("commands", [])
        declared = {c.get("name") for c in requires if c.get("name")}
        assert declared == set()
        action = next(
            item
            for item in plugin.manifest["actions"]["on_iso_ready"]
            if item["type"] == "run_builder"
        )
        assert action["image"] == "spark-writer/proxmox-auto-install:trixie-v2"


class TestArtifactValidation:
    def test_duplicate_artifact_ids_fail_fast(self, proxmox_plugin):
        action = {
            "id": "create_test_artifact",
            "type": "create_artifact",
            "artifact_id": "duplicate",
            "content": "hello",
            "kind": "config",
            "logical_name": "value.txt",
        }

        proxmox_plugin._execute_action(
            action=action,
            ui_values={},
            preset={"id": "demo", "name": "Demo"},
            iso_path="/tmp/test.iso",
        )

        with pytest.raises(RuntimeError, match="already exists"):
            proxmox_plugin._execute_action(
                action=action,
                ui_values={},
                preset={"id": "demo", "name": "Demo"},
                iso_path="/tmp/test.iso",
            )

    def test_manifest_with_retired_action_is_unavailable(self, tmp_path, monkeypatch):
        from spark_writer.plugins.json_plugin import JsonSparkPlug

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        manifest = tmp_path / "retired.json"
        manifest.write_text(
            '{"version": "1.6","metadata":{"id":"retired","name":"Retired"},'
            '"requires":{"commands":[]},"actions":{"on_iso_ready":['
            '{"id":"legacy","type":"write_file","content":"x","path":"/tmp/x"}]}}',
            encoding="utf-8",
        )

        plugin = JsonSparkPlug(str(manifest))
        assert plugin.is_available is False
        assert "Manifest schema validation failed" in (plugin.unavailable_reason or "")
