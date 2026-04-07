"""Test usb_writer_core subprocess contracts with spark_writer.

This test suite validates the integration seams:
- Correct subprocess commands are constructed
- Progress callbacks fire with expected values
- Error handling is clear
- Crostini detection branches correctly
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call, mock_open

import pytest

# Add src to path for imports
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from usb_writer_core.writer import (
    list_removable_drives,
    write_iso_to_device,
    create_aux_partition,
    find_partition_by_label,
    wipe_device,
    USBWriteError,
    PartitionNotFoundError,
)
from usb_writer_core.notifications.crostini import is_running_in_crostini


# ============================================================================
# Fixtures
# ============================================================================

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


# ============================================================================
# Tests: list_removable_drives
# ============================================================================

@patch("subprocess.check_output")
def test_list_removable_drives_calls_lsblk(mock_check_output):
    """Verify list_removable_drives calls lsblk with correct args."""
    mock_check_output.return_value = json.dumps({
        "blockdevices": []
    }).encode()
    
    list_removable_drives()
    
    # Verify lsblk was called with correct flags
    mock_check_output.assert_called_once()
    cmd = mock_check_output.call_args[0][0]
    
    assert cmd[0] == "lsblk"
    assert "-d" in cmd  # nodeps
    assert "-J" in cmd  # json output
    assert "-o" in cmd  # output specification


@patch("subprocess.check_output")
def test_list_removable_drives_filters_loop_devices(mock_check_output):
    """Verify loop devices are excluded from results."""
    mock_check_output.return_value = json.dumps({
        "blockdevices": [
            {"name": "loop0", "size": "1G", "tran": ""},
            {"name": "sdb", "size": "32G", "tran": "usb"},
        ]
    }).encode()
    
    drives = list_removable_drives()
    
    assert len(drives) == 1
    assert drives[0]["name"] == "sdb"
    assert drives[0]["path"] == "/dev/sdb"


@patch("subprocess.check_output")
def test_list_removable_drives_includes_usb_devices(mock_check_output):
    """Verify USB devices are included."""
    mock_check_output.return_value = json.dumps({
        "blockdevices": [
            {"name": "sdb", "size": "32G", "tran": "usb", "rm": 1},
            {"name": "sdc", "size": "64G", "tran": "usb", "rm": 1, "model": "Kingston", "hotplug": 1},
        ]
    }).encode()
    
    drives = list_removable_drives()
    
    assert len(drives) == 2
    assert drives[0]["name"] == "sdb"
    assert drives[1]["name"] == "sdc"
    assert drives[1]["model"] == "Kingston"


@patch("subprocess.check_output")
def test_list_removable_drives_handles_malformed_json(mock_check_output):
    """Verify graceful handling of malformed JSON from lsblk."""
    mock_check_output.return_value = b"not json at all {"
    
    drives = list_removable_drives()
    
    assert drives == []


@patch("subprocess.check_output")
def test_list_removable_drives_handles_lsblk_failure(mock_check_output):
    """Verify graceful handling when lsblk fails."""
    mock_check_output.side_effect = Exception("lsblk not found")
    
    drives = list_removable_drives()
    
    assert drives == []


# ============================================================================
# Tests: write_iso_to_device
# ============================================================================

@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_write_iso_calls_dd_with_correct_args(
    mock_stat, mock_exists, mock_popen, mock_run, temp_iso_file
):
    """Verify dd is called with correct arguments."""
    mock_exists.return_value = True
    mock_stat.return_value = MagicMock(st_size=1000000)
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.poll = MagicMock(side_effect=[None, 0])  # Not done, then done
    # Create file-like object for stderr
    mock_stderr = MagicMock()
    mock_stderr.readline = MagicMock(return_value="")  # No output
    mock_process.stderr = mock_stderr
    mock_popen.return_value = mock_process
    
    write_iso_to_device(temp_iso_file, "/dev/sdb")
    
    # Verify dd was called
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    
    assert cmd[0] == "dd"
    assert f"if={temp_iso_file}" in cmd
    assert "of=/dev/sdb" in cmd
    assert "oflag=sync" in cmd
    assert "status=progress" in cmd


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_write_iso_triggers_progress_callback(
    mock_stat, mock_exists, mock_popen, mock_run, temp_iso_file
):
    """Verify progress callback is called with correct values."""
    mock_exists.return_value = True
    mock_stat.return_value = MagicMock(st_size=1000000)
    
    # Simulate dd progress output on stderr
    dd_output = [
        "20971520 bytes (21 MB, 20 MiB) copied, 0.345 s, 60.8 MB/s\n",
        "41943040 bytes (42 MB, 40 MiB) copied, 0.689 s, 60.9 MB/s\n",
    ]
    
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.poll = MagicMock(side_effect=[None, None, None, 0])  # Not done x3, then done
    # Create file-like stderr that returns lines then empty, then keeps returning empty
    def readline_side_effect():
        for line in dd_output:
            yield line
        while True:
            yield ""
    
    mock_stderr = MagicMock()
    mock_stderr.readline = MagicMock(side_effect=readline_side_effect())
    mock_process.stderr = mock_stderr
    mock_popen.return_value = mock_process
    
    progress_values = []
    
    def capture_progress(bytes_written, total):
        progress_values.append((bytes_written, total))
    
    write_iso_to_device(temp_iso_file, "/dev/sdb", progress_callback=capture_progress)
    
    # Verify progress callback was called with correct bytes
    assert len(progress_values) == 2
    assert progress_values[0] == (20971520, 1000000)
    assert progress_values[1] == (41943040, 1000000)


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_write_iso_raises_on_missing_iso(
    mock_stat, mock_exists, mock_popen, mock_run
):
    """Verify error if ISO file doesn't exist."""
    mock_exists.side_effect = [False, True]  # ISO missing, device exists
    
    with pytest.raises(USBWriteError) as exc_info:
        write_iso_to_device(Path("/tmp/missing.iso"), "/dev/sdb")
    
    assert "not found" in str(exc_info.value).lower()


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_write_iso_raises_on_missing_device(
    mock_stat, mock_exists, mock_popen, mock_run
):
    """Verify error if device doesn't exist."""
    mock_exists.side_effect = [True, False]  # ISO exists, device missing
    
    with pytest.raises(USBWriteError) as exc_info:
        write_iso_to_device(Path("/tmp/test.iso"), "/dev/missing")
    
    assert "not found" in str(exc_info.value).lower()


@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_write_iso_calls_wipe_device_first(
    mock_stat, mock_exists, mock_popen, mock_run, temp_iso_file
):
    """Verify wipe_device is called before dd."""
    mock_exists.return_value = True
    mock_stat.return_value = MagicMock(st_size=1000000)
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.poll = MagicMock(side_effect=[None, 0])  # Not done, then done
    mock_stderr = MagicMock()
    mock_stderr.readline = MagicMock(return_value="")
    mock_process.stderr = mock_stderr
    mock_popen.return_value = mock_process
    
    write_iso_to_device(temp_iso_file, "/dev/sdb")
    
    # Verify wipefs and sgdisk were called
    calls = mock_run.call_args_list
    
    # First call should be wipefs
    assert calls[0][0][0][0] == "wipefs"
    
    # Second call should be sgdisk
    assert calls[1][0][0][0] == "sgdisk"


# ============================================================================
# Tests: create_aux_partition
# ============================================================================

@patch("subprocess.run")
def test_create_aux_partition_calls_sgdisk_with_correct_args(mock_run):
    """Verify sgdisk is called with correct partition parameters."""
    mock_run.return_value = MagicMock(returncode=0)
    
    create_aux_partition("/dev/sdb", "CIDATA", size_mb=64)
    
    calls = mock_run.call_args_list
    
    # Should have: sgdisk -e, sgdisk -n, partprobe, udevadm
    sgdisk_calls = [c for c in calls if c[0][0][0] == "sgdisk"]
    assert len(sgdisk_calls) >= 2
    
    # Check for -e (relocation) and -n (new partition)
    assert any("-e" in call[0][0] for call in sgdisk_calls)
    assert any("-n" in call[0][0] for call in sgdisk_calls)


@patch("subprocess.run")
def test_create_aux_partition_specifies_label(mock_run):
    """Verify partition label is set correctly."""
    mock_run.return_value = MagicMock(returncode=0)
    
    create_aux_partition("/dev/sdb", "CUSTOM_LABEL", size_mb=100)
    
    calls = mock_run.call_args_list
    sgdisk_calls = [call[0][0] for call in calls if len(call[0][0]) > 0 and call[0][0][0] == "sgdisk"]
    
    # Find the call with -c (label) flag (looks like: sgdisk -n ... -c 0:CUSTOM_LABEL ...)
    label_call = [c for c in sgdisk_calls if "-c" in c]
    assert len(label_call) > 0
    # Check that CUSTOM_LABEL appears in the command somewhere
    assert any("CUSTOM_LABEL" in str(arg) for arg in label_call[0])


@patch("subprocess.run")
def test_create_aux_partition_raises_on_sgdisk_failure(mock_run):
    """Verify error if sgdisk fails."""
    import subprocess as subprocess_module
    # Simulate check=True failure by raising CalledProcessError
    error = subprocess_module.CalledProcessError(1, ["sgdisk"], stderr="Command failed")
    mock_run.side_effect = error
    
    with pytest.raises(USBWriteError) as exc_info:
        create_aux_partition("/dev/sdb", "CIDATA")
    
    assert "partition" in str(exc_info.value).lower()


# ============================================================================
# Tests: find_partition_by_label
# ============================================================================

@patch("subprocess.run")
@patch("os.path.exists")
@patch("time.sleep")
def test_find_partition_by_label_parses_sgdisk_output(
    mock_sleep, mock_exists, mock_run
):
    """Verify partition is found and path is returned."""
    sgdisk_output = """Number  Start (sector)    End (sector)  Size       Code  Name
   1            2048        999423  488 MiB     EF00  
   2          999424       2000000  488 MiB     0700  CIDATA
"""
    
    mock_run.return_value = MagicMock(returncode=0, stdout=sgdisk_output)
    mock_exists.return_value = True
    
    partition_path = find_partition_by_label("/dev/sdb", "CIDATA")
    
    assert partition_path == "/dev/sdb2"


@patch("subprocess.run")
@patch("os.path.exists")
@patch("time.sleep")
def test_find_partition_by_label_retries_on_not_found(
    mock_sleep, mock_exists, mock_run
):
    """Verify retry logic if partition not immediately found."""
    sgdisk_output = """Number  Start (sector)    End (sector)  Size       Code  Name
   1            2048        999423  488 MiB     EF00  
"""
    
    # First few calls don't have partition, then succeed
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout=sgdisk_output),
        MagicMock(returncode=0, stdout=sgdisk_output),
        MagicMock(returncode=0, stdout=sgdisk_output + "   2          999424       2000000  488 MiB     0700  CIDATA\n"),
    ]
    
    mock_exists.return_value = True
    
    partition_path = find_partition_by_label("/dev/sdb", "CIDATA", max_attempts=3)
    
    assert partition_path == "/dev/sdb2"
    assert mock_run.call_count == 3


@patch("subprocess.run")
@patch("os.path.exists")
@patch("time.sleep")
def test_find_partition_by_label_raises_if_not_found(
    mock_sleep, mock_exists, mock_run
):
    """Verify PartitionNotFoundError if label not found after retries."""
    mock_run.return_value = MagicMock(returncode=0, stdout="No partitions")
    mock_exists.return_value = False
    
    with pytest.raises(PartitionNotFoundError) as exc_info:
        find_partition_by_label("/dev/sdb", "MISSING", max_attempts=2)
    
    assert "MISSING" in str(exc_info.value)


# ============================================================================
# Tests: wipe_device
# ============================================================================

@patch("subprocess.run")
def test_wipe_device_calls_wipefs_and_sgdisk(mock_run):
    """Verify wipe_device calls correct commands in order."""
    mock_run.return_value = MagicMock(returncode=0)
    
    wipe_device("/dev/sdb")
    
    calls = mock_run.call_args_list
    
    # First call: wipefs
    assert calls[0][0][0][0] == "wipefs"
    assert "-a" in calls[0][0][0]
    
    # Second call: sgdisk
    assert calls[1][0][0][0] == "sgdisk"
    assert "-Z" in calls[1][0][0]


@patch("subprocess.run")
def test_wipe_device_continues_on_error(mock_run):
    """Verify wipe_device doesn't raise on error (logs warning instead)."""
    # First wipefs fails
    mock_run.side_effect = Exception("wipefs failed")
    
    # Should not raise
    wipe_device("/dev/sdb")


# ============================================================================
# Tests: Crostini Detection
# ============================================================================

def test_is_running_in_crostini_with_env_var_set(monkeypatch):
    """Verify Crostini detection when env var is set."""
    monkeypatch.setenv("CROS_CONTAINER_VERSION", "1.2.3")
    
    # Clear cache to pick up env var change
    is_running_in_crostini.cache_clear()
    
    assert is_running_in_crostini() is True


def test_is_running_in_crostini_with_env_var_unset(monkeypatch):
    """Verify Crostini detection when env var is not set."""
    monkeypatch.delenv("CROS_CONTAINER_VERSION", raising=False)
    
    # Clear cache to pick up env var change
    is_running_in_crostini.cache_clear()
    
    assert is_running_in_crostini() is False


# ============================================================================
# Integration: Command Contract Between spark_writer and usb_writer_core
# ============================================================================

@patch("subprocess.run")
@patch("subprocess.Popen")
@patch("pathlib.Path.exists")
@patch("pathlib.Path.stat")
def test_full_write_flow_subprocess_sequence(
    mock_stat, mock_exists, mock_popen, mock_run, temp_iso_file
):
    """Integration test: verify full sequence of subprocess calls."""
    mock_exists.return_value = True
    mock_stat.return_value = MagicMock(st_size=1000000)
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.poll = MagicMock(side_effect=[None, 0])
    mock_stderr = MagicMock()
    mock_stderr.readline = MagicMock(return_value="")
    mock_process.stderr = mock_stderr
    mock_popen.return_value = mock_process
    
    write_iso_to_device(temp_iso_file, "/dev/sdb")
    
    # Verify call sequence
    calls = mock_run.call_args_list
    
    # Should see: wipefs, sgdisk, partprobe, udevadm, sync, udevadm settle
    command_names = [call[0][0][0] for call in calls]
    
    assert "wipefs" in command_names
    assert "sgdisk" in command_names
    assert "partprobe" in command_names
    assert "sync" in command_names
