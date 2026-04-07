# USB Writer Core - Notifications API Reference

This document provides an overview of the notification system in `usb-writer-core`. The system is designed to provide clear, persistent feedback for long-running operations like writing a USB drive.

## Prerequisites

This library uses the `notify-send` command-line tool to display desktop notifications. Before using this library, ensure `notify-send` is installed on your system. It is part of the `libnotify-bin` package on Debian/Ubuntu systems.

```bash
sudo apt-get install libnotify-bin
```

## Installation

```bash
# Development install from monorepo
pip install -e apps/spark-writer
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

**Purpose**: Manages a single, persistent notification across a multi-stage process, automatically calculating and displaying the overall progress.

```python
from usb_writer_core.notifications import PipelineNotifier, PipelineStage

# Create pipeline notifier
pipeline = PipelineNotifier(app_name="USB Writer")

# Start the overall process
pipeline.start("Writing Ubuntu 24.04 to USB")

# Stage 1: Download (0-25% of overall progress)
pipeline.update_stage(PipelineStage.DOWNLOAD, "Downloading ISO...", stage_progress=50) # 12.5% overall

# Stage 2: Write (25-50% of overall progress)
pipeline.update_stage(PipelineStage.WRITE, "Writing to /dev/sdb", stage_progress=50) # 37.5% overall
pipeline.complete_stage(PipelineStage.WRITE) # 50% overall

# Stage 3: Verify (50-75% of overall progress)
pipeline.update_stage(PipelineStage.VERIFY, "Verifying checksum...", stage_progress=100) # 75% overall

# Stage 4: Finalize (75-100% of overall progress)
pipeline.complete_stage(PipelineStage.FINALIZE) # 100% overall

# Mark the entire pipeline as successful
pipeline.success("USB drive is ready!")
```

**Pipeline Stages (`PipelineStage` enum)**:
- `DOWNLOAD`
- `WRITE`
- `VERIFY`
- `FINALIZE`

Each stage contributes equally to the total progress (25% each in the default configuration).

**Why Use This**:
- ✅ Simplifies notifications for complex, multi-step operations.
- ✅ Provides a single, non-spammy notification to the user.
- ✅ Automatically calculates and updates a continuous progress bar from 0% to 100%.
- ✅ Clear success and failure states.

---

## Complete Pipeline Example

This example demonstrates how to use the `PipelineNotifier` to show progress for a complete "download and write" workflow.

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
    pipeline.update_stage(PipelineStage.DOWNLOAD, f"Downloading Proxmox ISO... {i}%", stage_progress=i)
    time.sleep(0.1)
pipeline.complete_stage(PipelineStage.DOWNLOAD)

# 4. Simulate Write stage
pipeline.update_stage(PipelineStage.WRITE, "Writing to USB drive...")
for i in range(0, 101, 10):
    pipeline.update_stage(PipelineStage.WRITE, f"Writing to USB drive... {i}%", stage_progress=i)
    time.sleep(0.1)
pipeline.complete_stage(PipelineStage.WRITE)

# 5. Simulate Verify and Finalize
pipeline.complete_stage(PipelineStage.VERIFY)
pipeline.complete_stage(PipelineStage.FINALIZE)

# 6. Mark as successful
pipeline.success("Proxmox USB is ready to use!")
```

**Result**: A single desktop notification will appear, starting at 0% and smoothly progressing to 100% as the stages complete, with the status text updating along the way.

---

## Thread Safety

The `DesktopNotifier` calls `notify-send` in a separate process for each notification. This makes it safe to call from multiple threads without worrying about shared D-Bus connections or other complex state.

---

## Testing Without a Desktop Environment

If `notify-send` is not available (e.g., in a CI/CD environment or a headless server), the `DesktopNotifier` will gracefully fail. It will print the notification content to `stderr` instead of crashing, allowing your application to run without a graphical environment.

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
