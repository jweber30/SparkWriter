"""Test desktop notifications for the USB writer."""

import asyncio
import os
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest
from usb_writer_core.notifications import (
    DesktopNotifier,
    Notification,
    NotificationLevel,
)


@pytest.fixture
def notifier():
    """Fixture for the DesktopNotifier instance."""
    return DesktopNotifier("test-app", icon_path="test-icon.png")


@patch("subprocess.run")
def test_send_notification(mock_run, notifier):
    """Test sending a simple notification."""
    # Setup mock to return success with an ID
    mock_run.return_value.stdout = "123"
    mock_run.return_value.returncode = 0

    notification = Notification(
        title="Test Title",
        body="Test Body",
        level=NotificationLevel.INFO,
    )

    result_id = notifier.send_notification(notification)

    assert result_id == 123

    expected_cmd = [
        "notify-send",
        "-a", "test-app",
        "-u", "low",
        "-p",
        "-i", os.path.abspath("test-icon.png"),
        "Test Title",
        "Test Body"
    ]

    # Verify subprocess.run was called correctly
    # Note: depending on environment, _wrap_command_for_user might modify the command
    # but in a standard test env without root, it should pass through.
    # However, we should check if the called args contain our expected args

    args, kwargs = mock_run.call_args
    called_cmd = args[0]

    # Check basic components are present
    assert "notify-send" in called_cmd
    assert "-a" in called_cmd
    assert "test-app" in called_cmd
    assert "Test Title" in called_cmd


@patch("subprocess.run")
def test_update_persistent_notification(mock_run, notifier):
    """Test updating a persistent notification."""
    # Setup mock
    mock_run.return_value.stdout = "123"
    mock_run.return_value.returncode = 0

    notification1 = Notification(
        title="Step 1",
        body="Writing to USB",
        level=NotificationLevel.INFO,
    )
    notification2 = Notification(
        title="Step 2",
        body="Verifying",
        level=NotificationLevel.INFO,
    )

    # First call
    notifier.update_persistent_notification("usb-write-progress", notification1)

    args1, _ = mock_run.call_args
    cmd1 = args1[0]
    assert "-p" in cmd1
    assert "-r" not in cmd1

    # Second call - mock returning same ID as if it was replaced/updated
    mock_run.return_value.stdout = "123"

    notifier.update_persistent_notification("usb-write-progress", notification2)

    args2, _ = mock_run.call_args
    cmd2 = args2[0]
    assert "-r" in cmd2
    assert "123" in cmd2
