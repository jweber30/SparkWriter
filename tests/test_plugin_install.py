import os
from pathlib import Path

from spark_writer.plugins.plugin_install import create_plugin_stage_dir, record_install_origin


def test_plugin_install_stages_on_destination_filesystem(monkeypatch, tmp_path):
    plugin_dir = tmp_path / "data" / "spark-writer" / "plugins"
    stage_dir = Path(create_plugin_stage_dir(str(plugin_dir), "metalstrapper-proxmox-hosts"))
    staged_manifest = stage_dir / "metalstrapper-proxmox-hosts.json"
    staged_manifest.write_text('{"version": "1.6"}', encoding="utf-8")
    installed_manifest = plugin_dir / "metalstrapper-proxmox-hosts.json"

    real_replace = os.replace

    def reject_cross_device_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if plugin_dir not in source_path.parents or destination_path.parent != plugin_dir:
            raise OSError(18, "Invalid cross-device link")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", reject_cross_device_replace)
    os.replace(staged_manifest, installed_manifest)

    assert stage_dir.parent == plugin_dir
    assert installed_manifest.read_text(encoding="utf-8") == '{"version": "1.6"}'


def test_record_install_origin_strips_query_and_fragment():
    manifest = {"metadata": {"id": "demo", "name": "Demo"}}

    record_install_origin(
        manifest,
        "https://metalstrapper.example/api/manifests/demo?token=secret#section",
    )

    assert manifest["metadata"]["installed_from"] == (
        "https://metalstrapper.example/api/manifests/demo"
    )
