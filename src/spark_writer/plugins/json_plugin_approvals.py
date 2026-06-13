"""Runtime approval persistence for JSON SparkPlug manifests."""

import json
import logging
import os
from typing import Any, Optional

from ..builders import OciBuilderRunner
from .action_context import PendingPhaseApproval, RuntimeApprovalRequiredError

logger = logging.getLogger(__name__)

APPROVAL_MODEL_VERSION = "invocation-v2"


class JsonPluginApprovalMixin:
    """Command approval helpers shared by JSON manifest plugins."""

    manifest: dict[str, Any]
    manifest_path: str
    _plugin_allowed_commands: set[str]
    _approved_builders: set[str]

    def _load_approved_commands(self) -> None:
        """Load user-approved commands from invocation-time approval metadata."""
        plugin_id = self._plugin_id()
        if not plugin_id:
            return

        candidates = [self._approval_file_path(), self._legacy_approval_file_path()]
        loaded_any = False
        for approval_file in candidates:
            if not approval_file or not os.path.exists(approval_file):
                continue

            try:
                with open(approval_file, 'r', encoding='utf-8') as f:
                    approval_data = json.load(f)

                approval_model = approval_data.get('approval_model')
                if approval_model != APPROVAL_MODEL_VERSION:
                    logger.info(
                        f"Ignoring legacy approval file for {plugin_id}; "
                        f"expected model {APPROVAL_MODEL_VERSION}"
                    )
                    continue

                approved = approval_data.get('approved_commands', [])
                self._plugin_allowed_commands.update(approved)
                approved_builders = approval_data.get("approved_builders", [])
                if isinstance(approved_builders, list):
                    self._approved_builders.update(
                        str(item) for item in approved_builders if str(item).strip()
                    )
                loaded_any = True

                if approved:
                    logger.info(f"Loaded approved commands for {plugin_id}: {', '.join(approved)}")

            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load approval file for {plugin_id}: {e}")

        if not loaded_any:
            logger.debug(f"No approval file found for {plugin_id}")

    def _approval_file_path(self) -> Optional[str]:
        plugin_id = self._plugin_id()
        if not plugin_id:
            return None
        state_home = os.environ.get("XDG_STATE_HOME")
        if not state_home:
            state_home = os.path.join(os.path.expanduser("~"), ".local", "state")
        return os.path.join(state_home, "spark-writer", "approvals", f".{plugin_id}.approval")

    def _legacy_approval_file_path(self) -> Optional[str]:
        plugin_id = self._plugin_id()
        if not plugin_id:
            return None
        manifest_dir = os.path.dirname(self.manifest_path)
        return os.path.join(manifest_dir, f".{plugin_id}.approval")

    def get_pending_phase_approval(self, phase_name: str) -> Optional[PendingPhaseApproval]:
        """Return pending approval data for a lifecycle phase if commands are unapproved."""
        actions = self.manifest.get('actions', {}).get(phase_name, [])
        if not actions:
            return None
        phase_commands = self._collect_phase_commands(actions)
        pending = [cmd for cmd in phase_commands if cmd not in self._plugin_allowed_commands]
        builders = self._collect_phase_builders(actions)
        pending_builders = [
            {"key": identity.approval_key, "display": identity.display}
            for identity in builders
            if identity.approval_key not in self._approved_builders
        ]
        if not pending and not pending_builders:
            return None
        return PendingPhaseApproval(phase_name, pending, pending_builders)

    def approve_runtime_commands(self, commands: list[str]) -> None:
        """Persist and activate newly approved runtime commands for this plugin."""

        plugin_id = self._plugin_id()
        approval_file = self._approval_file_path()
        if not plugin_id or not approval_file:
            raise RuntimeError("Plugin metadata.id is required to persist runtime approvals")

        normalized = {str(cmd).strip() for cmd in commands if str(cmd).strip()}
        merged = sorted(self._plugin_allowed_commands.union(normalized))

        payload = {
            "plugin_id": plugin_id,
            "approval_model": APPROVAL_MODEL_VERSION,
            "approved_commands": merged,
            "approved_builders": sorted(self._approved_builders),
        }

        try:
            approval_dir = os.path.dirname(approval_file)
            os.makedirs(approval_dir, exist_ok=True)
            with open(approval_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
        except OSError as exc:
            raise RuntimeError(f"Failed to persist runtime approval for {plugin_id}: {exc}") from exc

        self._plugin_allowed_commands.update(merged)
        if normalized:
            logger.info(
                "Persisted runtime approval for %s: %s",
                plugin_id,
                ", ".join(sorted(normalized)),
            )

    def approve_runtime_builders(self, approval_keys: list[str]) -> None:
        normalized = {str(key).strip() for key in approval_keys if str(key).strip()}
        self._approved_builders.update(normalized)
        self.approve_runtime_commands([])

    def approve_runtime_phase(self, pending: PendingPhaseApproval) -> None:
        if pending.builders:
            self._approved_builders.update(item["key"] for item in pending.builders)
        self.approve_runtime_commands(pending.commands)

    def _collect_phase_commands(self, actions: list[dict[str, Any]]) -> list[str]:
        """Return unique executable names needed by a phase in order."""

        phase_commands: list[str] = []
        seen: set[str] = set()
        for action in actions:
            cmd_name = self._command_name_for_action(action)
            if isinstance(cmd_name, str) and cmd_name and cmd_name not in seen:
                seen.add(cmd_name)
                phase_commands.append(cmd_name)
        return phase_commands

    def _collect_phase_builders(self, actions: list[dict[str, Any]]) -> list[Any]:
        identities = []
        for action in actions:
            if action.get("type") != "run_builder":
                continue
            builder_id = str(action.get("builder_id", "")).strip()
            image = str(action.get("image", "")).strip()
            if not builder_id or not image:
                continue
            identity = OciBuilderRunner().resolve_identity(
                builder_id=builder_id,
                image=image,
                network=bool(action.get("network", False)),
            )
            identities.append(identity)
        return identities

    def ensure_builder_approved(self, identity: Any) -> None:
        if identity.approval_key in self._approved_builders:
            return
        raise self._build_runtime_approval_error(
            PendingPhaseApproval(
                self._current_phase_name(),
                builders=[{"key": identity.approval_key, "display": identity.display}],
            )
        )

    def _command_name_for_action(self, action: dict[str, Any]) -> Optional[str]:
        action_type = action.get('type')
        if action_type == 'run_command':
            command = action.get('command') or []
            if command and isinstance(command[0], str):
                return command[0]
            return None
        return None

    def _build_runtime_approval_error(self, pending: PendingPhaseApproval) -> RuntimeApprovalRequiredError:
        return RuntimeApprovalRequiredError(self._plugin_id(), pending)

    def _current_phase_name(self) -> str:
        return self._active_phase_name or "current"

    def _ensure_phase_runtime_approval(self, phase_name: str, actions: list[dict[str, Any]]) -> None:
        """Require runtime approval for all pending commands in a lifecycle phase."""

        phase_commands = self._collect_phase_commands(actions)
        pending = [cmd for cmd in phase_commands if cmd not in self._plugin_allowed_commands]
        builders = self._collect_phase_builders(actions)
        pending_builders = [
            {"key": identity.approval_key, "display": identity.display}
            for identity in builders
            if identity.approval_key not in self._approved_builders
        ]
        if not pending and not pending_builders:
            return
        raise self._build_runtime_approval_error(
            PendingPhaseApproval(phase_name, pending, pending_builders)
        )
