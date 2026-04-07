"""A notification pipeline for long-running operations."""

from enum import Enum, auto
from typing import Dict, List, Optional

from .notifier import DesktopNotifier, Notification, NotificationLevel


class PipelineStage(Enum):
    """A stage in the USB provisioning pipeline."""

    DOWNLOAD = auto()
    PROCESS = auto()
    WRITE = auto()
    VERIFY = auto()
    FINALIZE = auto()


# Stage emoticons for visual feedback
STAGE_EMOJI = {
    PipelineStage.DOWNLOAD: "⬇️",
    PipelineStage.PROCESS: "⚙️",
    PipelineStage.WRITE: "💾",
    PipelineStage.VERIFY: "✓",
    PipelineStage.FINALIZE: "🎉",
}


class PipelineNotifier:
    """A notifier for a multi-stage pipeline."""

    def __init__(
        self,
        app_name: str,
        stages: Optional[List[PipelineStage]] = None,
        desktop_notifier: Optional[DesktopNotifier] = None,
    ):
        """Initialize the pipeline notifier.
        
        Args:
            app_name: Application name for notifications
            stages: List of stages this pipeline will execute. If None, uses all stages.
            desktop_notifier: Optional custom notifier instance
        """
        self.app_name = app_name
        self.desktop_notifier = desktop_notifier or DesktopNotifier(app_name)
        self.pipeline_id = f"{app_name}-pipeline"
        self.current_title = ""
        
        # Use provided stages or default to all stages
        if stages is None:
            self.stages = [
                PipelineStage.DOWNLOAD,
                PipelineStage.PROCESS,
                PipelineStage.WRITE,
                PipelineStage.VERIFY,
                PipelineStage.FINALIZE,
            ]
        else:
            self.stages = stages
        
        # Track progress for each stage independently (0-100)
        self.stage_progress: Dict[PipelineStage, int] = {
            stage: 0 for stage in self.stages
        }
        self.active_stage: Optional[PipelineStage] = None

    def start(self, title: str):
        """Start the pipeline and show the initial notification."""
        self.current_title = title
        # Reset all stage progress
        for stage in self.stages:
            self.stage_progress[stage] = 0
        self.active_stage = None
        
        notification = Notification(
            title=self.current_title,
            body="Starting...",
            level=NotificationLevel.NORMAL,
            progress=0,
        )
        self.desktop_notifier.update_persistent_notification(
            self.pipeline_id, notification
        )

    def update_stage(self, stage: PipelineStage, message: str, progress: int):
        """Update the status of a specific stage.
        
        Args:
            stage: The stage being updated
            message: Status message for this stage
            progress: Progress within this stage (0-100)
        """
        if stage not in self.stages:
            raise ValueError(f"Stage {stage} is not in the configured pipeline stages")
        
        # Update stage progress and mark as active
        self.stage_progress[stage] = progress
        self.active_stage = stage
        
        # Format notification body with emoji, stage counter, and message
        stage_index = self.stages.index(stage) + 1
        total_stages = len(self.stages)
        emoji = STAGE_EMOJI.get(stage, "•")
        stage_name = stage.name.capitalize()
        body = f"{emoji} [{stage_index}/{total_stages}] {stage_name} • {message}"
        
        notification = Notification(
            title=self.current_title,
            body=body,
            level=NotificationLevel.NORMAL,
            progress=progress,  # Show this stage's progress bar
        )
        self.desktop_notifier.update_persistent_notification(
            self.pipeline_id, notification
        )

    def complete_stage(self, stage: PipelineStage):
        """Mark a stage as complete.
        
        Args:
            stage: The stage being completed
        """
        if stage not in self.stages:
            raise ValueError(f"Stage {stage} is not in the configured pipeline stages")
        
        # Mark stage as 100% complete
        self.stage_progress[stage] = 100
        self.active_stage = stage
        
        # Format completion message
        stage_index = self.stages.index(stage) + 1
        total_stages = len(self.stages)
        emoji = STAGE_EMOJI.get(stage, "•")
        stage_name = stage.name.capitalize()
        body = f"{emoji} [{stage_index}/{total_stages}] {stage_name} • Complete"
        
        notification = Notification(
            title=self.current_title,
            body=body,
            level=NotificationLevel.NORMAL,
            progress=100,
        )
        self.desktop_notifier.update_persistent_notification(
            self.pipeline_id, notification
        )

    def success(self, message: str):
        """Mark the pipeline as successful."""
        notification = Notification(
            title=self.current_title,
            body=f"🎉 Complete • {message}",
            level=NotificationLevel.NORMAL,
            progress=None,  # No progress bar for final state
        )
        self.desktop_notifier.update_persistent_notification(
            self.pipeline_id, notification
        )

    def failure(self, message: str):
        """Mark the pipeline as failed."""
        notification = Notification(
            title=self.current_title,
            body=f"❌ Failed • {message}",
            level=NotificationLevel.CRITICAL,
            progress=None,  # No progress bar for error state
        )
        self.desktop_notifier.update_persistent_notification(
            self.pipeline_id, notification
        )
