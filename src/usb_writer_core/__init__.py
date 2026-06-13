"""USB Writer Core - Shared USB writing and notification library.

This package provides:
- Desktop notifications with Crostini/ChromeOS support
- Domain models for USB write operations
- Session management and progress reporting
- Core USB writing functions with Crostini workarounds
"""

from .models import (
    Attachment,
    CloudInitBundle,
    DeviceRef,
    DeviceType,
    IsoProfile,
    IsoSourceType,
    NotificationConfig,
    VerifiedImage,
    WipeMode,
    WriteIntent,
    WriteSession,
    WriteStatus,
)
from .notifications import (
    DesktopNotifier,
    Notification,
    NotificationLevel,
    PipelineNotifier,
    PipelineStage,
)
from .progress import ProgressEvent, ProgressReporter
from .sessions import DeviceBusyError, SessionStore, SessionUpdate
from .writer import (
    MountError,
    PartitionNotFoundError,
    USBWriteError,
    create_aux_partition,
    find_partition_by_label,
    partition_exists,
    inject_grub_kernel_params,
    write_files_to_partition,
    write_iso_to_device,
)
from .receipts import (
    ReceiptError,
    ReceiptSigningError,
    canonicalize_receipt,
    compute_receipt_hash,
    current_timestamp,
    encode_public_key,
    generate_nonce,
    hmac_fingerprint,
    load_signing_key,
    sign_with_key,
)

__version__ = "0.1.45"

__all__ = [
    # Domain Models
    "Attachment",
    "CloudInitBundle",
    "DeviceRef",
    "DeviceType",
    "IsoProfile",
    "IsoSourceType",
    "NotificationConfig",
    "VerifiedImage",
    "WipeMode",
    "WriteIntent",
    "WriteSession",
    "WriteStatus",
    # Notifications
    "DesktopNotifier",
    "Notification",
    "NotificationLevel",
    "PipelineNotifier",
    "PipelineStage",
    # Progress
    "ProgressEvent",
    "ProgressReporter",
    # Sessions
    "DeviceBusyError",
    "SessionStore",
    "SessionUpdate",
    # Writer
    "MountError",
    "PartitionNotFoundError",
    "USBWriteError",
    "create_aux_partition",
    "find_partition_by_label",
    "partition_exists",
    "inject_grub_kernel_params",
    "write_files_to_partition",
    "write_iso_to_device",
    # Receipts
    "ReceiptError",
    "ReceiptSigningError",
    "canonicalize_receipt",
    "compute_receipt_hash",
    "current_timestamp",
    "encode_public_key",
    "generate_nonce",
    "hmac_fingerprint",
    "load_signing_key",
    "sign_with_key",
]
