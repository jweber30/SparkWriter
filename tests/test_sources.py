"""Tests for manifest-owned Sources and Source-aware SparkPlug selection."""

import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import JsonSparkPlug
from spark_writer.plugins.manager import PluginManager
from spark_writer.receipts import build_receipt_payload
from spark_writer.sources import Source, SourceCatalog


def test_source_catalog_without_json_is_empty():
    catalog = SourceCatalog()
    assert catalog.list_sources() == []


def test_source_catalog_normalizes_catalog_shape(tmp_path):
    catalog_path = tmp_path / "sources.json"
    catalog_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "demo-source",
                        "name": "Demo Source",
                        "family": "ubuntu",
                        "url": "https://example.com/demo.iso",
                        "acquire": {"kind": "direct", "artifact": "demo.iso"},
                        "installer_scheme": "ubuntu-nocloud",
                        "capabilities": ["cloud-init-nocloud"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    source = SourceCatalog(catalog_path).list_sources()[0]
    assert source.id == "demo-source"
    assert source.acquire_kind == "direct"
    assert source.acquire_artifact == "demo.iso"
    assert source.installer_scheme == "ubuntu-nocloud"
    assert source.to_dict()["source_family"] == "ubuntu"
    assert source.to_dict()["source_acquire_artifact"] == "demo.iso"
    assert source.can_write_usb is True
    assert source.can_export_iso is True


def test_plugin_manager_collects_manifest_owned_sources(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "ubuntu-autoinstall", "name": "Ubuntu Autoinstall"},
        "requires": {"commands": []},
        "source": {
            "id": "ubuntu-24.04-server-autoinstall",
            "name": "Ubuntu 24.04 LTS Server Autoinstall",
            "family": "ubuntu",
            "url": "https://example.com/ubuntu.iso",
            "installer_scheme": "ubuntu-nocloud",
        },
        "outputs": {"usb": True, "iso": False},
    }

    manager = PluginManager()
    manager.plugins = [_write_plugin(tmp_path, "ubuntu-autoinstall", manifest)]

    source = manager.get_manifest_sources()[0]

    assert source.id == "ubuntu-24.04-server-autoinstall"
    assert source.sparkplug_id == "ubuntu-autoinstall"
    assert source.can_write_usb is True
    assert source.can_export_iso is False


def _write_plugin(tmp_path: Path, plugin_id: str, manifest: dict) -> JsonSparkPlug:
    path = tmp_path / f"{plugin_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return JsonSparkPlug(str(path))


def test_plugin_manager_filters_plugins_by_source_compatibility(tmp_path):
    ubuntu_manifest = {
        "version": "1.4",
        "metadata": {"id": "ubuntu-only", "name": "Ubuntu Only"},
        "requires": {"commands": []},
        "ui_visibility": {"when": {"source_family": ["ubuntu"]}},
    }
    proxmox_manifest = {
        "version": "1.4",
        "metadata": {"id": "proxmox-only", "name": "Proxmox Only"},
        "requires": {"commands": []},
        "ui_visibility": {"when": {"source_id": ["proxmox-ve-9.1"]}},
    }

    manager = PluginManager()
    manager.plugins = [
        _write_plugin(tmp_path, "ubuntu-only", ubuntu_manifest),
        _write_plugin(tmp_path, "proxmox-only", proxmox_manifest),
    ]

    ubuntu_source = {
        "id": "ubuntu-24.04-server",
        "family": "ubuntu",
        "source_id": "ubuntu-24.04-server",
        "source_family": "ubuntu",
    }
    compatible = manager.get_compatible_plugins(ubuntu_source)

    assert [plugin.plugin_id for plugin in compatible] == ["ubuntu-only"]


def test_plugin_manager_uses_manifest_source_owner(tmp_path):
    owner_manifest = {
        "version": "1.4",
        "metadata": {"id": "owner", "name": "Owner"},
        "requires": {"commands": []},
        "source": {
            "id": "owner-source",
            "name": "Owner Source",
            "family": "ubuntu",
            "url": "https://example.com/owner.iso",
        },
    }
    broad_manifest = {
        "version": "1.4",
        "metadata": {"id": "broad", "name": "Broad"},
        "requires": {"commands": []},
        "ui_visibility": {"when": {"source_family": ["ubuntu"]}},
    }

    manager = PluginManager()
    manager.plugins = [
        _write_plugin(tmp_path, "owner", owner_manifest),
        _write_plugin(tmp_path, "broad", broad_manifest),
    ]

    source = manager.get_manifest_sources()[0]
    compatible = manager.get_compatible_plugins(source.to_dict())

    assert [plugin.plugin_id for plugin in compatible] == ["owner"]


def test_plugin_manager_detects_conflicting_selection(tmp_path):
    first_manifest = {
        "version": "1.4",
        "metadata": {"id": "first", "name": "First"},
        "requires": {"commands": []},
        "config_fields": [{"id": "hostname", "label": "Hostname", "type": "text"}],
        "actions": {
            "on_iso_ready": [
                {
                    "id": "a1",
                    "type": "create_artifact",
                    "artifact_id": "user_data",
                    "content": "x",
                    "logical_name": "user-data",
                }
            ]
        },
    }
    second_manifest = {
        "version": "1.4",
        "metadata": {"id": "second", "name": "Second"},
        "requires": {"commands": []},
        "config_fields": [{"id": "hostname", "label": "Hostname", "type": "text"}],
    }

    manager = PluginManager()
    first = _write_plugin(tmp_path, "first", first_manifest)
    second = _write_plugin(tmp_path, "second", second_manifest)

    conflict = manager.validate_plugin_selection([first, second])
    assert conflict is not None
    assert "hostname" in conflict


def test_json_plugin_exposes_manifest_wizard_pages(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "wizard-demo", "name": "Wizard Demo"},
        "requires": {"commands": []},
        "config_fields": [
            {"id": "hostname", "label": "Hostname", "type": "text"},
        ],
        "wizard": {
            "pages": [
                {
                    "id": "identity",
                    "title": "Identity",
                    "fields": ["hostname"],
                }
            ]
        },
    }

    plugin = _write_plugin(tmp_path, "wizard-demo", manifest)

    assert plugin.is_available
    assert plugin.get_wizard_pages()[0]["id"] == "identity"


def test_json_plugin_rejects_unknown_wizard_field(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "bad-wizard", "name": "Bad Wizard"},
        "requires": {"commands": []},
        "config_fields": [
            {"id": "hostname", "label": "Hostname", "type": "text"},
        ],
        "wizard": {
            "pages": [
                {
                    "id": "identity",
                    "title": "Identity",
                    "fields": ["missing"],
                }
            ]
        },
    }

    plugin = _write_plugin(tmp_path, "bad-wizard", manifest)

    assert not plugin.is_available
    assert "unknown field 'missing'" in (plugin.unavailable_reason or "")


def test_json_plugin_accepts_new_manifest_version_with_return_delivery(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "delivery-demo", "name": "Delivery Demo"},
        "requires": {"commands": []},
        "return_delivery": {
            "enabled": True,
            "secrets": ["admin_password", "join_token"],
            "endpoints": [
                {
                    "id": "ops",
                    "label": "Ops",
                    "url": "https://ops.example.com/sparkwriter",
                }
            ],
        },
    }

    plugin = _write_plugin(tmp_path, "delivery-demo", manifest)

    assert plugin.is_available
    assert plugin.requires_return_delivery() is True
    spec = plugin.get_return_delivery_spec()
    assert spec["secrets"] == ["admin_password", "join_token"]
    assert spec["endpoints"][0]["label"] == "Ops"


def test_json_plugin_rejects_legacy_manifest_version(tmp_path):
    manifest = {
        "version": "1.0",
        "metadata": {"id": "legacy-delivery", "name": "Legacy Delivery"},
        "requires": {"commands": []},
        "return_delivery": {"secrets": ["admin_password"]},
    }

    plugin = _write_plugin(tmp_path, "legacy-delivery", manifest)

    assert not plugin.is_available
    assert "Unsupported manifest version: 1.0" in (plugin.unavailable_reason or "")


def test_json_plugin_rejects_future_manifest_version_with_supported_versions(tmp_path):
    manifest = {
        "version": "9.9",
        "metadata": {"id": "future", "name": "Future"},
        "requires": {"commands": []},
    }

    plugin = _write_plugin(tmp_path, "future", manifest)

    assert not plugin.is_available
    reason = plugin.unavailable_reason or ""
    assert "Unsupported manifest version: 9.9" in reason
    assert "1.4" in reason


def test_json_plugin_rejects_non_https_return_endpoint(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "bad-endpoint", "name": "Bad Endpoint"},
        "requires": {"commands": []},
        "return_delivery": {
            "secrets": ["admin_password"],
            "endpoints": [
                {
                    "id": "plain",
                    "label": "Plain HTTP",
                    "url": "http://ops.example.com/sparkwriter",
                }
            ],
        },
    }

    plugin = _write_plugin(tmp_path, "bad-endpoint", manifest)

    assert not plugin.is_available
    assert "Manifest schema validation failed" in (plugin.unavailable_reason or "")
    assert "return_delivery.endpoints.0.url" in (plugin.unavailable_reason or "")


def test_json_plugin_accepts_localhost_http_return_endpoint(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "local-endpoint", "name": "Local Endpoint"},
        "requires": {"commands": []},
        "return_delivery": {
            "secrets": ["admin_password"],
            "endpoints": [
                {
                    "id": "local",
                    "label": "Local Collector",
                    "url": "http://localhost:8765/sparkwriter",
                }
            ],
        },
    }

    plugin = _write_plugin(tmp_path, "local-endpoint", manifest)

    assert plugin.is_available
    assert plugin.get_return_delivery_spec()["endpoints"][0]["url"].startswith("http://localhost")


def test_builtin_ubuntu_autoinstall_uses_source_compatibility(ubuntu_autoinstall_plugin):
    assert ubuntu_autoinstall_plugin.should_show_ui(
        "ubuntu-24.04-server",
        {
            "id": "ubuntu-24.04-server",
            "family": "ubuntu",
            "source_family": "ubuntu",
            "installer_scheme": "ubuntu-nocloud",
        },
    )
    assert not ubuntu_autoinstall_plugin.should_show_ui(
        "debian-12",
        {
            "id": "debian-12",
            "family": "debian",
            "source_family": "debian",
            "installer_scheme": "preseed",
        },
    )


def test_receipt_builder_emits_source_and_sparkplugs(tmp_path):
    iso_path = tmp_path / "ubuntu.iso"
    iso_path.write_bytes(b"hello")

    manifest = {
        "version": "1.4",
        "metadata": {"id": "ubuntu-autoinstall", "name": "Ubuntu Autoinstall", "version": "1.0.0"},
        "requires": {"commands": []},
    }
    plugin = _write_plugin(tmp_path, "ubuntu-autoinstall", manifest)

    source = Source.from_dict(
        {
            "id": "ubuntu-24.04-server",
            "name": "Ubuntu 24.04 LTS Server",
            "family": "ubuntu",
            "url": "https://example.com/ubuntu.iso",
            "installer_scheme": "ubuntu-nocloud",
        }
    )

    receipt = build_receipt_payload(
        source=source,
        sparkplugs=[plugin],
        original_iso_path=str(iso_path),
    )

    assert receipt["source"]["id"] == "ubuntu-24.04-server"
    assert receipt["sparkplugs"][0]["id"] == "ubuntu-autoinstall"
    assert "original_iso_sha256" in receipt["final_artifacts"]
