"""Test the notification pipeline."""

from unittest.mock import MagicMock

import pytest
from usb_writer_core.notifications import (
    Notification,
    NotificationLevel,
    PipelineNotifier,
    PipelineStage,
)


@pytest.fixture
def mock_desktop_notifier():
    """Fixture for a mocked DesktopNotifier."""
    return MagicMock()


def test_pipeline_notifier_full_lifecycle(mock_desktop_notifier):
    """Test the full lifecycle of the pipeline notifier with all stages."""
    pipeline_notifier = PipelineNotifier(
        "test-app", desktop_notifier=mock_desktop_notifier
    )

    # Start the process
    pipeline_notifier.start("Writing USB Drive")
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="Starting...",
            level=NotificationLevel.NORMAL,
            progress=0,
        ),
    )

    # Update progress for DOWNLOAD stage
    pipeline_notifier.update_stage(
        PipelineStage.DOWNLOAD, "Downloading (45 peers)", 73
    )
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="⬇️ [1/5] Download • Downloading (45 peers)",
            level=NotificationLevel.NORMAL,
            progress=73,  # Stage's own progress
        ),
    )

    # Complete DOWNLOAD stage
    pipeline_notifier.complete_stage(PipelineStage.DOWNLOAD)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="⬇️ [1/5] Download • Complete",
            level=NotificationLevel.NORMAL,
            progress=100,
        ),
    )

    # Update PROCESS stage
    pipeline_notifier.update_stage(PipelineStage.PROCESS, "Injecting configuration", 50)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="⚙️ [2/5] Process • Injecting configuration",
            level=NotificationLevel.NORMAL,
            progress=50,
        ),
    )

    # Complete PROCESS stage
    pipeline_notifier.complete_stage(PipelineStage.PROCESS)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="⚙️ [2/5] Process • Complete",
            level=NotificationLevel.NORMAL,
            progress=100,
        ),
    )

    # Update WRITE stage
    pipeline_notifier.update_stage(PipelineStage.WRITE, "Writing to USB...", 45)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="💾 [3/5] Write • Writing to USB...",
            level=NotificationLevel.NORMAL,
            progress=45,
        ),
    )

    # Complete WRITE stage
    pipeline_notifier.complete_stage(PipelineStage.WRITE)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="💾 [3/5] Write • Complete",
            level=NotificationLevel.NORMAL,
            progress=100,
        ),
    )

    # Mark as success
    pipeline_notifier.success("USB drive is ready.")
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="🎉 Complete • USB drive is ready.",
            level=NotificationLevel.NORMAL,
            progress=None,
        ),
    )

    # Mark as failure
    pipeline_notifier.failure("Failed to write to USB.")
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Writing USB Drive",
            body="❌ Failed • Failed to write to USB.",
            level=NotificationLevel.CRITICAL,
            progress=None,
        ),
    )


def test_pipeline_notifier_custom_stages(mock_desktop_notifier):
    """Test pipeline with custom subset of stages."""
    # Only DOWNLOAD and WRITE stages
    pipeline_notifier = PipelineNotifier(
        "test-app",
        stages=[PipelineStage.DOWNLOAD, PipelineStage.WRITE],
        desktop_notifier=mock_desktop_notifier
    )

    pipeline_notifier.start("Quick Flash")
    
    # DOWNLOAD is stage 1 of 2
    pipeline_notifier.update_stage(PipelineStage.DOWNLOAD, "Downloading...", 50)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Quick Flash",
            body="⬇️ [1/2] Download • Downloading...",
            level=NotificationLevel.NORMAL,
            progress=50,
        ),
    )

    pipeline_notifier.complete_stage(PipelineStage.DOWNLOAD)
    
    # WRITE is stage 2 of 2
    pipeline_notifier.update_stage(PipelineStage.WRITE, "Writing...", 75)
    mock_desktop_notifier.update_persistent_notification.assert_called_with(
        "test-app-pipeline",
        Notification(
            title="Quick Flash",
            body="💾 [2/2] Write • Writing...",
            level=NotificationLevel.NORMAL,
            progress=75,
        ),
    )


def test_pipeline_notifier_stage_progress_tracking(mock_desktop_notifier):
    """Test that stage progress is tracked independently."""
    pipeline_notifier = PipelineNotifier(
        "test-app", desktop_notifier=mock_desktop_notifier
    )

    pipeline_notifier.start("Test")

    # Update DOWNLOAD to 50%
    pipeline_notifier.update_stage(PipelineStage.DOWNLOAD, "Downloading", 50)
    assert pipeline_notifier.stage_progress[PipelineStage.DOWNLOAD] == 50
    assert pipeline_notifier.active_stage == PipelineStage.DOWNLOAD

    # Update DOWNLOAD to 100%
    pipeline_notifier.update_stage(PipelineStage.DOWNLOAD, "Downloading", 100)
    assert pipeline_notifier.stage_progress[PipelineStage.DOWNLOAD] == 100

    # Complete DOWNLOAD
    pipeline_notifier.complete_stage(PipelineStage.DOWNLOAD)
    assert pipeline_notifier.stage_progress[PipelineStage.DOWNLOAD] == 100

    # Start WRITE at 0%
    pipeline_notifier.update_stage(PipelineStage.WRITE, "Writing", 0)
    assert pipeline_notifier.stage_progress[PipelineStage.WRITE] == 0
    assert pipeline_notifier.stage_progress[PipelineStage.DOWNLOAD] == 100  # Still tracked
    assert pipeline_notifier.active_stage == PipelineStage.WRITE


def test_pipeline_notifier_invalid_stage(mock_desktop_notifier):
    """Test that using a stage not in the pipeline raises an error."""
    # Pipeline with only DOWNLOAD and WRITE
    pipeline_notifier = PipelineNotifier(
        "test-app",
        stages=[PipelineStage.DOWNLOAD, PipelineStage.WRITE],
        desktop_notifier=mock_desktop_notifier
    )

    pipeline_notifier.start("Test")

    # Try to update PROCESS stage (not in this pipeline)
    with pytest.raises(ValueError, match="Stage .* is not in the configured pipeline stages"):
        pipeline_notifier.update_stage(PipelineStage.PROCESS, "Processing", 50)

    # Try to complete VERIFY stage (not in this pipeline)
    with pytest.raises(ValueError, match="Stage .* is not in the configured pipeline stages"):
        pipeline_notifier.complete_stage(PipelineStage.VERIFY)

