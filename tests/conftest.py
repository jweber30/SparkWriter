"""Shared test fixtures for SparkWriter test suite."""

import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import JsonSparkPlug


@pytest.fixture
def proxmox_manifest_path():
    """Return path to the Proxmox Tailscale test manifest."""
    manifest_path = Path(__file__).parent / "proxmox-tailscale.json"
    if not manifest_path.exists():
        pytest.skip("Proxmox manifest not found in tests/")
    return manifest_path


@pytest.fixture
def proxmox_manifest(proxmox_manifest_path):
    """Load the Proxmox Tailscale test manifest as a dict."""
    with open(proxmox_manifest_path) as f:
        return json.load(f)


@pytest.fixture
def proxmox_plugin(proxmox_manifest_path, tmp_path):
    """Create a JsonSparkPlug instance from the Proxmox manifest.
    
    This fixture simulates the plugin installation flow by:
    1. Copying the manifest to a temp directory
    2. Loading it as a plugin
    3. Writing an approval file for plugin-specific commands
    """
    # Copy manifest to temp dir (simulating installation)
    temp_manifest = tmp_path / "proxmox-tailscale.json"
    temp_manifest.write_text(proxmox_manifest_path.read_text())
    
    plugin = JsonSparkPlug(str(temp_manifest))
    
    # Write approval for proxmox-auto-install-assistant
    approval_file = tmp_path / ".proxmox-tailscale.approval"
    approval_data = {
        "plugin_id": "proxmox-tailscale",
        "plugin_name": "Proxmox Tailscale",
        "approved_commands": ["proxmox-auto-install-assistant"]
    }
    approval_file.write_text(json.dumps(approval_data, indent=2))
    
    # Reload plugin to pick up approval
    plugin._load_approved_commands()
    
    return plugin


@pytest.fixture
def temp_iso_file(tmp_path):
    """Create a fake ISO file for testing."""
    iso_file = tmp_path / "test.iso"
    iso_file.write_bytes(b"X" * 1000000)  # 1MB file
    return iso_file


@pytest.fixture
def temp_device_path(tmp_path):
    """Create a fake device path."""
    device = tmp_path / "sdb"
    device.write_bytes(b"")  # Empty file to represent device
    return str(device)
