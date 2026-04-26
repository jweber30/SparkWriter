"""Tests for host-owned Source catalogs and Source-aware SparkPlug selection."""

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


def test_source_catalog_loads_builtin_json():
    catalog = SourceCatalog()
    sources = catalog.list_sources()

    assert sources
    assert any(source.id == "ubuntu-24.04-server" for source in sources)
    assert all(source.url for source in sources)
    assert all(source.family for source in sources)


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
                        "acquire": {"kind": "direct"},
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
    assert source.installer_scheme == "ubuntu-nocloud"
    assert source.to_dict()["source_family"] == "ubuntu"


def _write_plugin(tmp_path: Path, plugin_id: str, manifest: dict) -> JsonSparkPlug:
    path = tmp_path / f"{plugin_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return JsonSparkPlug(str(path))


def test_plugin_manager_filters_plugins_by_source_compatibility(tmp_path):
    ubuntu_manifest = {
        "version": "1.0",
        "metadata": {"id": "ubuntu-only", "name": "Ubuntu Only"},
        "requires": {"commands": []},
        "ui_visibility": {"when": {"source_family": ["ubuntu"]}},
    }
    proxmox_manifest = {
        "version": "1.0",
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


def test_plugin_manager_detects_conflicting_selection(tmp_path):
    first_manifest = {
        "version": "1.0",
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
        "version": "1.0",
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
        "version": "1.0",
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
