# USB Writer Core - Notifications API Reference

This document provides an overview of the notification system in `usb-writer-core`. The system is designed to provide clear, persistent feedback for long-running operations like writing a USB drive.

## Prerequisites

This library uses the `notify-send` command-line tool to display desktop notifications. Before using this library, ensure `notify-send` is installed on your system. It is part of the `libnotify-bin` package on Debian/Ubuntu systems.

```bash
sudo apt-get install libnotify-bin
```

## Installation

```bash
# Development install from this repository
pip install -e .
```

## Quick Start

### Basic Notification Example

```python
import time
from usb_writer_core.notifications import DesktopNotifier, Notification, NotificationLevel

# Create notifier
notifier = DesktopNotifier(app_name="MyApp")

# Send a simple notification
notification = Notification(
    title="Hello!",
    body="This is a test notification.",
    level=NotificationLevel.NORMAL,
)
notifier.send_notification(notification)

# Send a notification with a progress bar
progress_notification = Notification(
    title="Downloading...",
    body="0%",
    level=NotificationLevel.NORMAL,
    progress=0,
)
notification_id = notifier.send_notification(progress_notification)

for i in range(1, 101):
    progress_notification.progress = i
    progress_notification.body = f"{i}%"
    # To update, we re-send the notification with the same ID
    # The notifier handles replacing the old notification.
    notifier.update_persistent_notification(str(notification_id), progress_notification)
    time.sleep(0.05)
```

---

## Core Notification Classes

### 1. `DesktopNotifier` (Low-level)

**Purpose**: A wrapper around the `notify-send` command-line tool to send desktop notifications.

```python
from usb_writer_core.notifications import DesktopNotifier, Notification, NotificationLevel

notifier = DesktopNotifier(app_name="MyCoolApp")

notification = Notification(
    title="File Transfer",
    body="Sending 'document.pdf'...",
    level=NotificationLevel.NORMAL,
    progress=50, # Show a progress bar at 50%
)

notifier.send_notification(notification)
```

**Features**:
- Simple API for sending notifications.
- Supports progress bars via the `progress` attribute on the `Notification` object.
- Can update existing notifications to create a persistent progress indicator.
- Relies on the standard `notify-send` tool, avoiding complex D-Bus dependencies.

### 2. `PipelineNotifier` (Pipeline Orchestrator)

**Purpose**: Manages a single, persistent notification across a multi-stage process.

```python
from usb_writer_core.notifications import PipelineNotifier, PipelineStage

# Create pipeline notifier
pipeline = PipelineNotifier(app_name="USB Writer")

# Start the overall process
pipeline.start("Writing Ubuntu 24.04 to USB")

# Stage 1: Download
pipeline.update_stage(PipelineStage.DOWNLOAD, "Downloading ISO...", progress=50)

# Stage 2: Process
pipeline.update_stage(PipelineStage.PROCESS, "Injecting configuration", progress=50)
pipeline.complete_stage(PipelineStage.PROCESS)

# Stage 3: Write
pipeline.update_stage(PipelineStage.WRITE, "Writing to /dev/sdb", progress=50)
pipeline.complete_stage(PipelineStage.WRITE)

# Stage 4: Verify
pipeline.complete_stage(PipelineStage.VERIFY)

# Stage 5: Finalize
pipeline.complete_stage(PipelineStage.FINALIZE)

# Mark the entire pipeline as successful
pipeline.success("USB drive is ready!")
```

**Pipeline Stages (`PipelineStage` enum)**:
- `DOWNLOAD`
- `PROCESS`
- `WRITE`
- `VERIFY`
- `FINALIZE`

Default stage progress is reported per active stage, not as a weighted aggregate across the whole pipeline.

Current notification body format is:

- stage emoji
- `[current/total]` stage counter
- capitalized stage name
- caller-provided message

Example body:

```text
⚙️ [2/5] Process • Injecting configuration
```

**Why Use This**:
- ✅ Simplifies notifications for complex, multi-step operations.
- ✅ Provides a single, non-spammy notification to the user.
- ✅ Reuses one persistent notification ID across the workflow.
- ✅ Shows stage-local progress and the current pipeline position.
- ✅ Clear success and failure states.

---

## Complete Pipeline Example

This example demonstrates how to use the `PipelineNotifier` to show progress for a complete workflow.

```python
import time
from usb_writer_core.notifications import PipelineNotifier, PipelineStage

# 1. Initialize the pipeline notifier
pipeline = PipelineNotifier(app_name="MetalStrapper")

# 2. Start the process
pipeline.start("Preparing Proxmox USB")

# 3. Simulate Download stage
pipeline.update_stage(PipelineStage.DOWNLOAD, "Downloading Proxmox ISO...")
for i in range(0, 101, 10):
    pipeline.update_stage(PipelineStage.DOWNLOAD, f"Downloading Proxmox ISO... {i}%", progress=i)
    time.sleep(0.1)
pipeline.complete_stage(PipelineStage.DOWNLOAD)

# 4. Simulate Process stage
pipeline.update_stage(PipelineStage.PROCESS, "Injecting configuration...")
for i in range(0, 101, 10):
    pipeline.update_stage(PipelineStage.PROCESS, f"Injecting configuration... {i}%", progress=i)
    time.sleep(0.1)
pipeline.complete_stage(PipelineStage.PROCESS)

# 5. Simulate Write stage
pipeline.update_stage(PipelineStage.WRITE, "Writing to USB drive...")
for i in range(0, 101, 10):
    pipeline.update_stage(PipelineStage.WRITE, f"Writing to USB drive... {i}%", progress=i)
    time.sleep(0.1)
pipeline.complete_stage(PipelineStage.WRITE)

# 6. Simulate Verify and Finalize
pipeline.complete_stage(PipelineStage.VERIFY)
pipeline.complete_stage(PipelineStage.FINALIZE)

# 7. Mark as successful
pipeline.success("Proxmox USB is ready to use!")
```

**Result**: A single desktop notification is reused throughout the workflow. The progress bar reflects the active stage's current `progress` value, and the body shows which stage is running.

---

## Thread Safety

The `DesktopNotifier` calls `notify-send` in a separate process for each notification. This makes it safe to call from multiple threads without worrying about shared D-Bus connections or other complex state.

If SparkWriter is running as root and `DBUS_SESSION_BUS_ADDRESS` points at a user session bus under `/run/user/<uid>/bus`, the notifier wraps `notify-send` with `runuser` so the notification is delivered into the user's desktop session.

---

## Testing Without a Desktop Environment

If `notify-send` is not available or fails, `DesktopNotifier` logs the error and returns without raising. It does not print a fallback notification payload to `stderr`.

---

## Troubleshooting

### Notifications Not Appearing

1.  **Check `notify-send` availability**:
    ```bash
    which notify-send
    ```
    If this command returns nothing, you need to install `libnotify-bin`.

2.  **Test `notify-send` manually**:
    ```bash
    notify-send "Test" "Hello World" -h int:value:50
    ```
    This should show a notification with a 50% progress bar. If it doesn't, there may be an issue with your desktop's notification server (e.g., `dunst`, `mako`, or your desktop environment's built-in server).

3.  **Check for Errors**: Run your Python script and check the console for any error output from `subprocess`.
