"""ISO manipulation utilities.

Provides functions for extracting, modifying, and repacking ISO images.
Uses xorriso for compatibility and maintains bootability.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ISOError(Exception):
    """Base exception for ISO manipulation operations."""
    pass


def check_xorriso_available() -> bool:
    """Check if xorriso is available on the system."""
    return shutil.which("xorriso") is not None


def extract_iso(iso_path: str, extract_dir: Path) -> None:
    """
    Extract ISO contents to a directory.
    
    Args:
        iso_path: Path to the ISO file
        extract_dir: Directory to extract contents to (must exist)
        
    Raises:
        ISOError: If extraction fails
    """
    if not check_xorriso_available():
        raise ISOError("xorriso is not installed. Install with: sudo apt install xorriso")
    
    logger.info(f"Extracting ISO {iso_path} to {extract_dir}")
    
    extract_cmd = [
        'xorriso',
        '-osirrox', 'on',
        '-indev', iso_path,
        '-extract', '/', str(extract_dir)
    ]
    
    try:
        result = subprocess.run(
            extract_cmd,
            check=True,
            capture_output=True,
            text=True
        )
        logger.info("ISO extracted successfully")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        raise ISOError(f"Failed to extract ISO: {error_msg}")


def repack_iso(
    source_dir: Path,
    output_path: str,
    volume_label: str = "Custom ISO",
    boot_config: Optional[dict] = None
) -> None:
    """
    Repack directory contents into a bootable ISO.
    
    Args:
        source_dir: Directory containing ISO contents
        output_path: Path for the output ISO file
        volume_label: Volume label for the ISO
        boot_config: Optional boot configuration dict with keys:
            - bios_boot: Path to BIOS boot image (e.g., 'boot/grub/i386-pc/eltorito.img')
            - bios_catalog: Path to boot catalog (e.g., 'boot.catalog')
            - uefi_boot: Path to UEFI boot image (e.g., 'boot/grub/efi.img')
            
    Raises:
        ISOError: If repacking fails
    """
    if not check_xorriso_available():
        raise ISOError("xorriso is not installed. Install with: sudo apt install xorriso")
    
    logger.info(f"Repacking ISO from {source_dir} to {output_path}")
    
    # Default boot configuration for Ubuntu-style ISOs
    if boot_config is None:
        boot_config = {
            'bios_boot': 'boot/grub/i386-pc/eltorito.img',
            'bios_catalog': 'boot.catalog',
            'uefi_boot': 'boot/grub/efi.img'
        }
    
    repack_cmd = [
        'xorriso',
        '-as', 'mkisofs',
        '-r',
        '-V', volume_label,
        '-J', '-joliet-long',
    ]
    
    # Add BIOS boot support
    if 'bios_boot' in boot_config:
        repack_cmd.extend([
            '-b', boot_config['bios_boot'],
            '-c', boot_config.get('bios_catalog', 'boot.catalog'),
            '-no-emul-boot',
            '-boot-load-size', '4',
            '-boot-info-table',
        ])
    
    # Add UEFI boot support
    if 'uefi_boot' in boot_config:
        repack_cmd.extend([
            '-eltorito-alt-boot',
            '-e', boot_config['uefi_boot'],
            '-no-emul-boot',
        ])
    
    # Output file and source directory
    repack_cmd.extend([
        '-o', output_path,
        str(source_dir)
    ])
    
    try:
        result = subprocess.run(
            repack_cmd,
            check=True,
            capture_output=True,
            text=True
        )
        logger.info(f"ISO repacked successfully: {output_path}")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr if e.stderr else str(e)
        raise ISOError(f"Failed to repack ISO: {error_msg}")


def modify_grub_config(
    extract_dir: Path,
    replacements: dict,
    grub_path: str = "boot/grub/grub.cfg"
) -> None:
    """
    Modify GRUB configuration with string replacements.
    
    Args:
        extract_dir: Directory containing extracted ISO
        replacements: Dict of {old_string: new_string} replacements
        grub_path: Relative path to grub.cfg from extract_dir
        
    Raises:
        ISOError: If GRUB config not found or modification fails
    """
    grub_cfg = extract_dir / grub_path
    
    if not grub_cfg.exists():
        raise ISOError(f"GRUB config not found at {grub_cfg}")
    
    logger.info(f"Modifying GRUB config at {grub_cfg}")
    
    try:
        content = grub_cfg.read_text()
        
        for old_str, new_str in replacements.items():
            content = content.replace(old_str, new_str)
        
        grub_cfg.write_text(content)
        logger.info("GRUB config modified successfully")
    except Exception as e:
        raise ISOError(f"Failed to modify GRUB config: {e}")


def inject_cloud_init_nocloud(
    iso_path: str,
    user_data: str,
    meta_data: str,
    output_path: str,
    volume_label: str = "Custom ISO",
    grub_modifications: Optional[dict] = None
) -> str:
    """
    Inject cloud-init NoCloud data into an Ubuntu ISO.
    
    This is a high-level convenience function that:
    1. Extracts the ISO
    2. Adds user-data and meta-data files to nocloud/ subdirectory
    3. Modifies GRUB to enable NoCloud datasource with proper quoting
    4. Repacks the ISO
    
    Args:
        iso_path: Path to source ISO
        user_data: Cloud-init user-data content
        meta_data: Cloud-init meta-data content
        output_path: Path for output ISO
        volume_label: Volume label for output ISO
        grub_modifications: Optional additional GRUB replacements
        
    Returns:
        Path to the output ISO
        
    Raises:
        ISOError: If any step fails
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        extract_dir = tmpdir_path / "iso"
        extract_dir.mkdir()
        
        # Extract ISO
        extract_iso(iso_path, extract_dir)
        
        # Create nocloud subdirectory and write cloud-init files
        nocloud_dir = extract_dir / "nocloud"
        nocloud_dir.mkdir(exist_ok=True)
        (nocloud_dir / "user-data").write_text(user_data)
        (nocloud_dir / "meta-data").write_text(meta_data)
        logger.info("Wrote cloud-init files to nocloud/ subdirectory")
        
        # Modify GRUB config for NoCloud with proper quoting
        # The quotes around ds=nocloud are crucial for proper parsing
        default_grub_mods = {
            'linux /casper/vmlinuz': 'linux /casper/vmlinuz autoinstall "ds=nocloud;s=/cdrom/nocloud/"'
        }
        
        if grub_modifications:
            default_grub_mods.update(grub_modifications)
        
        try:
            modify_grub_config(extract_dir, default_grub_mods)
        except ISOError as e:
            logger.warning(f"Failed to modify GRUB config: {e}")
        
        # Repack ISO
        repack_iso(extract_dir, output_path, volume_label)
    
    return output_path
