"""Core USB writing functions.

This module provides low-level USB writing operations with Crostini support.
All filesystem operations use the dd-to-temp-file approach to bypass
Crostini's USB mounting restrictions.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Constants
CIDATA_LABEL = "CIDATA"
CIDATA_SIZE_MB = 64
DEFAULT_PARTITION_TYPE = "0700"  # Microsoft basic data


class USBWriteError(Exception):
    """Base exception for USB write operations."""
    pass


class PartitionNotFoundError(USBWriteError):
    """Raised when expected partition cannot be found."""
    pass


class MountError(USBWriteError):
    """Raised when filesystem mounting fails."""
    pass


def list_removable_drives() -> List[Dict[str, Any]]:
    """
    List available removable drives using lsblk.
    
    Returns:
        List of dictionaries containing drive info (name, size, model, path).
    """
    try:
        # -d: nodeps (don't list partitions)
        # -o: output columns
        # -J: json output
        cmd = ['lsblk', '-d', '-o', 'NAME,SIZE,MODEL,TRAN,RM,HOTPLUG', '-J']
        output = subprocess.check_output(cmd).decode()
        data = json.loads(output)
        
        removable_drives = []
        for device in data.get('blockdevices', []):
            # Filter for removable drives
            # RM="1" means removable
            # TRAN="usb" usually means USB
            # We also exclude loop devices
            
            name = device.get('name', '')
            if name.startswith('loop'):
                continue
                
            is_removable = device.get('rm') == True or device.get('rm') == '1'
            is_usb = device.get('tran') == 'usb'
            is_hotplug = device.get('hotplug') == True or device.get('hotplug') == '1'
            
            # In some environments (like Crostini), RM might not be set correctly for passed-through devices.
            # But usually USB devices show up with tran=usb.
            if is_removable or is_usb or is_hotplug:
                removable_drives.append({
                    'name': name,
                    'path': f"/dev/{name}",
                    'size': device.get('size', 'Unknown'),
                    'model': device.get('model', 'Unknown Device'),
                    'tran': device.get('tran')
                })
                
        return removable_drives
        
    except Exception as e:
        logger.error(f"Failed to list drives: {e}")
        return []


def wipe_device(device_path: str) -> None:
    """
    Wipe partition table and signatures from device.
    
    Args:
        device_path: Target device (e.g., /dev/sdb)
    """
    logger.info(f"Wiping device {device_path}")
    try:
        # Wipe filesystem signatures
        subprocess.run(
            ["wipefs", "-a", device_path], 
            check=False, 
            capture_output=True
        )
        
        # Zap GPT structures
        subprocess.run(
            ["sgdisk", "-Z", device_path], 
            check=False, 
            capture_output=True
        )
        
        # Force kernel to reload partition table
        subprocess.run(["partprobe", device_path], check=False)
        subprocess.run(["udevadm", "settle"], check=False)
        time.sleep(1)
        
    except Exception as e:
        logger.warning(f"Wipe warning (continuing anyway): {e}")


def write_iso_to_device(iso_path: Path, device_path: str, block_size: str = "4M", progress_callback=None) -> None:
    """
    Write an ISO image to a USB device using dd.
    
    Args:
        iso_path: Path to the ISO file
        device_path: Target device (e.g., /dev/sdb)
        block_size: Block size for dd (default: 4M)
        progress_callback: Optional callback(bytes_written, total_bytes)
        
    Raises:
        USBWriteError: If write operation fails
    """
    if not iso_path.exists():
        raise USBWriteError(f"ISO file not found: {iso_path}")
    
    if not Path(device_path).exists():
        raise USBWriteError(f"Device not found: {device_path}")
    
    # Wipe device first to ensure clean state
    wipe_device(device_path)
    
    logger.info(f"Writing ISO {iso_path} to {device_path} with block size {block_size}")
    
    total_size = iso_path.stat().st_size
    
    try:
        cmd = ["dd", f"if={iso_path}", f"of={device_path}", f"bs={block_size}", "oflag=sync", "status=progress"]
        
        process = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        # Parse stderr for progress
        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            
            if line and progress_callback:
                # Example line: 20971520 bytes (21 MB, 20 MiB) copied, 0.345 s, 60.8 MB/s
                try:
                    parts = line.split()
                    if len(parts) > 0 and parts[0].isdigit():
                        bytes_written = int(parts[0])
                        progress_callback(bytes_written, total_size)
                except ValueError:
                    pass
                    
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
            
        subprocess.run(["sync"], check=False)
        time.sleep(2)  # Give kernel time to recognize changes
        
        logger.info(f"Successfully wrote ISO to {device_path}")
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        raise USBWriteError(f"Failed to write ISO: {error_msg}")


def create_aux_partition(
    device_path: str,
    label: str,
    size_mb: int = 100,
    partition_type: str = DEFAULT_PARTITION_TYPE
) -> None:
    """
    Creates a small FAT32 partition at the end of the device.
    
    Args:
        device_path: The target USB device (e.g., /dev/sdb)
        label: The filesystem label (e.g., 'CIDATA', 'OEMDRV')
        size_mb: Size in megabytes (default 100)
        partition_type: GPT partition type code (default: 0700)
        
    Raises:
        USBWriteError: If partition creation fails
    """
    logger.info(f"Creating aux partition on {device_path} ({size_mb}MB) with label {label}")
    
    try:
        # Relocate GPT backup header to end of disk
        subprocess.run(
            ["sgdisk", "-e", device_path],
            capture_output=True,
            check=True
        )
        logger.info("Relocated GPT headers")
        
        # Create new partition at end of disk
        subprocess.run(
            ["sgdisk", "-n", f"0:0:+{size_mb}M", "-t", f"0:{partition_type}", 
             "-c", f"0:{label}", device_path],
            capture_output=True,
            check=True
        )
        logger.info(f"Created {size_mb}MB partition with label {label}")
        
        # Refresh partition table
        subprocess.run(["partprobe", device_path], capture_output=True, check=False)
        subprocess.run(["udevadm", "settle", "--timeout=5"], capture_output=True, check=False)
        time.sleep(2)  # Give kernel time to recognize new partition
        
        logger.info(f"Partition {label} created successfully")
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        raise USBWriteError(f"Failed to create partition {label}: {error_msg}")


def find_partition_by_label(device_path: str, label: str, max_attempts: int = 10) -> str:
    """
    Find a partition by its GPT partition name.
    
    Args:
        device_path: Device to search (e.g., /dev/sdb)
        label: Partition label to find
        max_attempts: Number of times to retry
        
    Returns:
        Full partition path (e.g., /dev/sdb4)
        
    Raises:
        PartitionNotFoundError: If partition cannot be found
    """
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,PARTLABEL"],
                capture_output=True,
                text=True,
                check=True
            )
            data = json.loads(result.stdout)
            
            for block_device in data.get('blockdevices', []):
                if _search_partitions(block_device, device_path, label):
                    partition_path = _search_partitions(block_device, device_path, label)
                    if os.path.exists(partition_path):
                        return partition_path
        except Exception as e:
            logger.debug(f"Attempt {attempt + 1} failed: {e}")
        
        if attempt < max_attempts - 1:
            time.sleep(1)
    
    raise PartitionNotFoundError(f"Could not find partition labeled '{label}' on {device_path}")


def partition_exists(device_path: str, label: str) -> bool:
    """Return True if a partition with the specified label exists."""

    try:
        find_partition_by_label(device_path, label, max_attempts=1)
        return True
    except PartitionNotFoundError:
        return False


def write_files_to_partition(
    device_path: str,
    label: str,
    files: Dict[str, str]
) -> None:
    """
    Write files to a partition using dd-to-temp-file approach.
    
    This bypasses Crostini's USB mounting restrictions by:
    1. Creating a FAT filesystem image in a temp file
    2. Writing files to the image with mtools
    3. Using dd to copy the image to the partition
    
    Args:
        device_path: Device containing the partition
        label: Label of the partition to write to
        files: Dict of { 'filename': 'content' }
        
    Raises:
        USBWriteError: If write operation fails
    """
    logger.info(f"Writing {len(files)} files to partition {label} on {device_path}")
    
    # Find partition
    try:
        partition_path = find_partition_by_label(device_path, label)
    except PartitionNotFoundError as e:
        raise USBWriteError(str(e))
    
    # Calculate the actual size of the data
    total_data_bytes = sum(len(content.encode('utf-8')) for content in files.values())
    
    # Add a 2MB buffer for FAT tables and metadata
    # Use an 8MB floor to ensure a stable FAT16/32 structure
    overhead = 2 * 1024 * 1024 
    floor = 8 * 1024 * 1024
    
    truncate_size = max(floor, total_data_bytes + overhead)
    logger.info(f"Calculating filesystem image size: {total_data_bytes} bytes data "
               f"+ {overhead} bytes overhead = {truncate_size} bytes "
               f"({truncate_size // (1024*1024)} MB)")
    
    # Create temporary filesystem image
    fs_image_file = tempfile.NamedTemporaryFile(
        prefix=f'{label}-fs-', suffix='.img', delete=False
    )
    fs_image_path = fs_image_file.name
    fs_image_file.close()
    
    temp_files = []
    
    try:
        # Create empty filesystem image sized for actual data + overhead
        logger.info(f"Creating temporary FAT filesystem image")
        subprocess.run(
            ["truncate", "-s", str(truncate_size), fs_image_path],
            check=True,
            capture_output=True
        )
        
        # Format the image as FAT32
        subprocess.run(
            ["mkfs.vfat", "-n", label, fs_image_path],
            check=True,
            capture_output=True
        )
        logger.info("Formatted filesystem image as FAT32")
        
        # Write files to the image using mtools
        for filename, content in files.items():
            # Create temp file for content
            tf = tempfile.NamedTemporaryFile(
                mode='w', prefix=f'write-{filename}-', delete=False
            )
            tf.write(content)
            tf.close()
            temp_files.append(tf.name)
            
            # Copy to image
            dest_path = f":::{filename}"
            subprocess.run(
                ["mcopy", "-i", fs_image_path, tf.name, dest_path],
                check=True,
                capture_output=True
            )
            logger.info(f"Wrote {filename} to filesystem image")

        # Write the filesystem image to the partition
        logger.info(f"Writing filesystem image to partition {partition_path}")
        subprocess.run(
            ["dd", f"if={fs_image_path}", f"of={partition_path}", "bs=4M", "status=progress", "oflag=sync"],
            check=True
        )
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed: {e.cmd}")
        if e.stderr:
            logger.error(f"Stderr: {e.stderr.decode()}")
        raise USBWriteError(f"Failed to write files to partition: {e}")
    finally:
        # Cleanup
        if os.path.exists(fs_image_path):
            os.unlink(fs_image_path)
        for tf in temp_files:
            if os.path.exists(tf):
                os.unlink(tf)


def _patch_grub_content(content: str, kernel_cmdline: str, timeout: int = 1) -> str:
    """Patch GRUB configuration content."""
    lines = content.splitlines()
    new_lines = []
    
    for line in lines:
        if line.strip().startswith('set timeout='):
            new_lines.append(f'set timeout={timeout}')
        elif line.strip().startswith('linux') or line.strip().startswith('linuxefi'):
            # Append kernel args
            new_lines.append(f"{line} {kernel_cmdline}")
        else:
            new_lines.append(line)
            
    return '\n'.join(new_lines)


def inject_grub_kernel_params(device_path: str, kernel_cmdline: str) -> None:
    """
    Inject kernel parameters into GRUB configuration on the EFI partition.
    
    Args:
        device_path: Target device (e.g., /dev/sdb)
        kernel_cmdline: Kernel parameters to append

    Raises:
        USBWriteError: If partition table cannot be read or injection fails
        PartitionNotFoundError: If EFI partition is not found
    """
    logger.info(f"Injecting kernel params into {device_path}: {kernel_cmdline}")
    
    # 1. Find EFI partition
    # We look for partition type EF00 (EFI System)
    efi_partition = None
    try:
        result = subprocess.run(
            ["sgdisk", "-p", device_path],
            capture_output=True,
            text=True,
            check=True
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 6 and parts[5] == 'EF00':
                efi_partition = f"{device_path}{parts[0]}"
                break
    except subprocess.CalledProcessError as e:
        raise USBWriteError(f"Failed to read partition table: {e}")
        
    if not efi_partition:
        raise PartitionNotFoundError(f"No EFI partition found on {device_path}")
        
    logger.info(f"Found EFI partition at {efi_partition}")
    
    # 2. Copy partition to temp file
    with tempfile.NamedTemporaryFile(prefix='efi-fs-', suffix='.img', delete=False) as tf:
        efi_image_path = tf.name
    
    grub_local_path = None
    try:
        subprocess.run(
            ["dd", f"if={efi_partition}", f"of={efi_image_path}", "bs=4M", "status=none"],
            check=True
        )
        
        # 3. Extract grub.cfg
        # Try common locations
        grub_locations = [
            "::/boot/grub/grub.cfg",
            "::/EFI/BOOT/grub.cfg",
            "::/EFI/debian/grub.cfg",
            "::/grub/grub.cfg"
        ]
        
        grub_content = None
        found_loc = None
        
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as grub_tf:
            grub_local_path = grub_tf.name
            
        for loc in grub_locations:
            try:
                subprocess.run(
                    ["mcopy", "-i", efi_image_path, loc, grub_local_path],
                    check=True,
                    capture_output=True
                )
                found_loc = loc
                with open(grub_local_path, 'r') as f:
                    grub_content = f.read()
                break
            except subprocess.CalledProcessError:
                continue
                
        if not grub_content or not found_loc:
            logger.warning("Could not find grub.cfg in EFI partition. Skipping injection.")
            return

        # 4. Patch content
        new_content = _patch_grub_content(grub_content, kernel_cmdline)
        
        with open(grub_local_path, 'w') as f:
            f.write(new_content)
            
        # 5. Write back
        subprocess.run(
            ["mcopy", "-o", "-i", efi_image_path, grub_local_path, found_loc],
            check=True,
            capture_output=True
        )
        
        # 6. Write image back to partition
        subprocess.run(
            ["dd", f"if={efi_image_path}", f"of={efi_partition}", "bs=4M", "status=none", "oflag=sync"],
            check=True
        )
        logger.info("Successfully injected kernel parameters")
        
    except Exception as e:
        raise USBWriteError(f"Failed to inject kernel parameters: {e}")
    finally:
        if os.path.exists(efi_image_path):
            os.unlink(efi_image_path)
        if grub_local_path and os.path.exists(grub_local_path):
            os.unlink(grub_local_path)
