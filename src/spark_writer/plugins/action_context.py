"""Phase-scoped execution state for SparkPlug JSON manifest actions."""

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .template_engine import SparkTemplateEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingPhaseApproval:
    phase_name: str
    commands: list[str]


@dataclass(frozen=True)
class ManifestArtifact:
    artifact_id: str
    content: str
    kind: str
    logical_name: str
    media_type: Optional[str] = None
    executable: bool = False


class RuntimeApprovalRequiredError(RuntimeError):
    """Raised when a phase requires runtime command approval before execution."""

    def __init__(self, plugin_id: str, pending: PendingPhaseApproval):
        self.plugin_id = plugin_id
        self.pending = pending
        command_list = ", ".join(pending.commands)
        super().__init__(
            "Runtime approval required before executing plugin commands. "
            f"Phase '{pending.phase_name}' needs approval for: {command_list}. "
            "Approve these commands in the runtime approval prompt to continue."
        )


@dataclass
class ActionContext:
    """Execution state for a single manifest phase run.

    Holds per-phase artifacts and action output variables.  ``allowed_commands``
    is a reference to the plugin's long-lived approval set so that approvals
    granted mid-session are immediately visible without extra synchronisation.
    """

    template_engine: SparkTemplateEngine
    allowed_commands: set  # reference to JsonSparkPlug._plugin_allowed_commands
    plugin_id: str = ""
    active_phase: str = "current"
    artifacts: Dict[str, ManifestArtifact] = field(default_factory=dict)
    action_vars: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Phase lifecycle
    # ------------------------------------------------------------------

    def reset(self, initial_action_vars: Optional[Dict[str, Any]] = None) -> None:
        """Reset per-phase state at the start of a new phase."""
        self.action_vars = dict(initial_action_vars or {})
        self.artifacts = {}

    def clear(self) -> None:
        """Clear per-phase state after a phase completes or fails."""
        self.action_vars = {}
        self.artifacts = {}

    # ------------------------------------------------------------------
    # Artifact registry
    # ------------------------------------------------------------------

    def validate_artifact_name(self, action_id: str, logical_name: str) -> str:
        candidate = str(logical_name or '').strip()
        if not candidate:
            raise RuntimeError(f"Action {action_id}: logical_name is required")
        if Path(candidate).name != candidate or candidate in {'.', '..'}:
            raise RuntimeError(
                f"Action {action_id}: logical_name '{candidate}' must be a simple file name"
            )
        return candidate

    def resolve_artifact_content(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> str:
        action_id = action.get('id', 'unknown')
        has_content = 'content' in action
        has_content_var = 'content_var' in action

        if has_content == has_content_var:
            raise RuntimeError(
                f"Action {action_id}: exactly one of 'content' or 'content_var' is required"
            )

        if has_content_var:
            variable_name = str(action.get('content_var', '')).strip()
            if not variable_name:
                raise RuntimeError(f"Action {action_id}: content_var must not be empty")
            if variable_name not in context:
                raise RuntimeError(
                    f"Action {action_id}: artifact source variable '{variable_name}' is undefined"
                )
            raw_content = context[variable_name]
        else:
            raw_content = self.template_engine.render(str(action.get('content', '')), context)

        if not isinstance(raw_content, str):
            raise RuntimeError(f"Action {action_id}: artifact content must resolve to text")

        return raw_content

    def store_artifact(self, artifact: ManifestArtifact) -> None:
        self.artifacts[artifact.artifact_id] = artifact

    def get_artifact(
        self,
        artifact_id: str,
        *,
        action_id: str,
        expected_kinds: Optional[Sequence[str]] = None,
    ) -> ManifestArtifact:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            raise RuntimeError(f"Action {action_id}: artifact '{artifact_id}' was not created")

        if expected_kinds and artifact.kind not in expected_kinds:
            expected = ", ".join(expected_kinds)
            raise RuntimeError(
                f"Action {action_id}: artifact '{artifact_id}' has kind '{artifact.kind}', "
                f"expected one of: {expected}"
            )
        return artifact

    def materialize_artifact(
        self,
        artifact: ManifestArtifact,
        *,
        directory: Path,
        file_name: Optional[str] = None,
    ) -> Path:
        """Write artifact content to a file inside *directory* and return the path."""
        output_name = file_name or artifact.logical_name
        output_path = directory / output_name
        output_path.write_text(artifact.content, encoding='utf-8')
        mode = 0o755 if artifact.executable else 0o644
        os.chmod(output_path, mode)
        return output_path

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def ensure_command_approved(
        self,
        command_name: str,
        build_error: Any,  # callable(PendingPhaseApproval) -> RuntimeApprovalRequiredError
    ) -> None:
        if command_name not in self.allowed_commands:
            raise build_error(PendingPhaseApproval(self.active_phase, [command_name]))

    def run_approved_command(
        self,
        cmd: list,
        *,
        use_sudo: bool,
        output_path: Optional[str] = None,
        build_approval_error: Any,  # callable(PendingPhaseApproval) -> RuntimeApprovalRequiredError
    ) -> str:
        """Validate approval, run *cmd*, and return stdout or *output_path*."""
        cmd_name = cmd[0] if cmd else ''
        self.ensure_command_approved(cmd_name, build_approval_error)

        if not shutil.which(cmd_name):
            raise RuntimeError(f"Command '{cmd_name}' not found in PATH")

        if use_sudo:
            if not shutil.which('sudo'):
                raise RuntimeError("sudo is required but not found in PATH")
            cmd = ['sudo', '-n'] + cmd

        logger.info(f"Running{' (with sudo)' if use_sudo else ''} command")
        try:
            proc_result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            if use_sudo and 'sudo: a password is required' in e.stderr:
                raise RuntimeError(
                    "Command requires passwordless sudo. "
                    "Configure NOPASSWD for user in /etc/sudoers or run SparkGTK with sudo."
                ) from e
            raise RuntimeError(f"Command failed: {e.stderr}") from e

        if proc_result.stderr:
            logger.debug(f"Command stderr: {proc_result.stderr}")

        if output_path is not None:
            return output_path
        return proc_result.stdout.strip()
