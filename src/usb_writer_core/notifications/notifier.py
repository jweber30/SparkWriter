"""Desktop notification primitives for MetalStrapper."""

import os
import subprocess
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict


class NotificationLevel(Enum):
    """Notification urgency level."""

    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"
    INFO = "low"  # Alias for LOW for backward compatibility with test


@dataclass
class Notification:
    """A desktop notification."""

    title: str
    body: str
    level: NotificationLevel = NotificationLevel.NORMAL
    progress: Optional[int] = None  # Progress percentage (0-100)


class DesktopNotifier:
    """A wrapper around notify-send for sending desktop notifications."""

    def __init__(self, app_name: str, icon_path: str = "notification-icon.png"):
        """Initialize the notifier."""
        self.logger = logging.getLogger(__name__)
        self.app_name = app_name
        self.icon_path = icon_path
        self._persistent_notifications: Dict[str, int] = {}  # Maps notification_id to dbus notification id

    def _wrap_command_for_user(self, cmd: list[str]) -> list[str]:
        """
        If running as root, wrap the command to run as the user who owns the DBus session.
        """
        if os.geteuid() != 0:
            return cmd

        dbus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if "unix:path=/run/user/" not in dbus_addr:
            return cmd

        try:
            # Extract UID from path (e.g. /run/user/1000/bus)
            parts = dbus_addr.split("/run/user/")
            if len(parts) < 2:
                return cmd
            
            uid_str = parts[1].split("/")[0]
            uid = int(uid_str)
            
            # Wrap with runuser
            return [
                "runuser", 
                "-u", f"#{uid}", 
                "--", 
                "env", 
                f"DBUS_SESSION_BUS_ADDRESS={dbus_addr}"
            ] + cmd
        except (ValueError, IndexError):
            return cmd

    def send_notification(self, notification: Notification) -> Optional[int]:
        """Send a simple desktop notification and returns the notification ID."""
        cmd = [
            "notify-send",
            "-a", self.app_name,
            "-u", notification.level.value,
            "-p",  # Always ask for the ID
        ]
        if self.icon_path and os.path.exists(self.icon_path):
            cmd.extend(["-i", os.path.abspath(self.icon_path)])

        if notification.progress is not None:
            cmd.extend(["-h", f"int:value:{notification.progress}"])

        cmd.extend([notification.title, notification.body])
        
        cmd = self._wrap_command_for_user(cmd)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if result.stdout.strip().isdigit():
                return int(result.stdout.strip())
        except FileNotFoundError as e:
            # notify-send not installed or not found
            self.logger.error("notify-send not found: %s", e)
        except subprocess.CalledProcessError as e:
            # notify-send returned non-zero
            self.logger.error("notify-send failed: %s", e)
            self.logger.debug("notify-send stderr: %s", e.stderr)
        return None

    def update_persistent_notification(self, notification_id: str, notification: Notification):
        """Create or update a persistent notification."""
        replaces_id = self._persistent_notifications.get(notification_id)

        cmd = [
            "notify-send",
            "-a", self.app_name,
            "-u", notification.level.value,
        ]
        if replaces_id:
            cmd.extend(["-r", str(replaces_id)])
        else:
            cmd.append("-p")

        if self.icon_path and os.path.exists(self.icon_path):
            cmd.extend(["-i", os.path.abspath(self.icon_path)])

        if notification.progress is not None:
            cmd.extend(["-h", f"int:value:{notification.progress}"])

        cmd.extend([notification.title, notification.body])
        
        cmd = self._wrap_command_for_user(cmd)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            if result.stdout.strip().isdigit():
                new_id = int(result.stdout.strip())
                self._persistent_notifications[notification_id] = new_id
        except FileNotFoundError as e:
            self.logger.error("notify-send not found: %s", e)
        except subprocess.CalledProcessError as e:
            self.logger.error("notify-send failed: %s", e)
            self.logger.debug("notify-send stderr: %s", e.stderr)

    def _map_level_to_urgency(self, level: NotificationLevel):
        """Map NotificationLevel to notify-send urgency string."""
        return level.value
