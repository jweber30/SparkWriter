"""Domain models shared across USB writer implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class DeviceType(str, Enum):
    """USB device type classification."""

    USB_STICK = "usb_stick"
    SD_CARD = "sd_card"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DeviceRef:
    """Reference to a physical USB device."""

    id: str
    path: str
    display_name: str
    size_bytes: int
    size_human: str
    vendor: Optional[str] = None
    model: Optional[str] = None
    device_type: DeviceType = DeviceType.UNKNOWN
    is_system_device: bool = False
    removable: bool = True
    bus: Optional[str] = None
    supports_wipe: bool = True


class IsoSourceType(str, Enum):
    """Where an ISO/profile originates."""

    HTTP = "http"
    TORRENT = "torrent"
    LOCAL = "local"


@dataclass(frozen=True)
class IsoProfile:
    """Metadata describing an install ISO profile."""

    profile_id: str
    label: str
    description: str
    source_type: IsoSourceType
    source_uri: str
    checksum: Optional[str] = None
    autoinstall_capable: bool = False
    default_cloud_init: Optional["CloudInitBundle"] = None


@dataclass(frozen=True)
class Attachment:
    """Binary attachment included in a CloudInitBundle."""

    name: str
    content: bytes


@dataclass(frozen=True)
class CloudInitBundle:
    """Cloud-init/autoinstall artifacts bundled with a request."""

    user_data: str
    meta_data: str
    network_config: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)


@dataclass
class NotificationConfig:
    """Controls how a writer emits desktop/system notifications."""

    enabled: bool = True
    app_name: str = "USB Writer"
    suppress_on_success: bool = False
    suppress_on_error: bool = False
    icon_path: Optional[str] = None
    use_system_notifications: bool = True


class WipeMode(str, Enum):
    """Wipe modes supported by write intents."""

    FULL = "full"
    QUICK = "quick"
    NONE = "none"


@dataclass
class WriteIntent:
    """Canonical request payload for launching a USB write job.
    
    The `tags` dict supports various configuration options:
    
    General tags:
        - force: bool = true  # Allow multiple concurrent jobs on same device
        - apt_cache_url: str  # APT cache/proxy URL (e.g., "http://apt-cache.local:3142")
                              # For Ubuntu/Debian: injected as apt.proxy in cloud-config
    """

    device_id: str
    iso_source: str
    cloud_init: Optional[CloudInitBundle] = None
    wipe_mode: WipeMode = WipeMode.FULL
    verify_after: bool = True
    include_glint: bool = False
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    tags: Dict[str, str] = field(default_factory=dict)


class WriteStatus(str, Enum):
    """Lifecycle states for a USB write session."""

    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WriteSession:
    """Represents the current state of a USB write operation."""

    session_id: str
    status: WriteStatus
    progress_percent: int = 0
    bytes_written: int = 0
    eta_seconds: Optional[int] = None
    message: str = ""
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    device_id: Optional[str] = None
    iso_source: Optional[str] = None
    logs_url: Optional[str] = None
    artifacts: Dict[str, str] = field(default_factory=dict)
    notification_id: Optional[str] = None


@dataclass
class ErrorEnvelope:
    """Standardized error contract."""

    code: str
    message: str
    detail: Optional[str] = None
    remediation: Optional[str] = None


@dataclass
class ValidationReport:
    """Outcome of validating a write intent before launch."""

    is_valid: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    recommended_device: Optional[DeviceRef] = None
    recommended_profile: Optional[IsoProfile] = None
