"""Host-owned installer scheme primitives for SparkPlug manifest execution.

Each function corresponds to one installer scheme action type.  They are
deliberately module-level so that new schemes can be added here without
growing JsonSparkPlug, and so they can be unit-tested without a full
manifest instance.
"""

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from usb_writer_core.iso_utils import inject_cloud_init_nocloud

from .action_context import ActionContext

logger = logging.getLogger(__name__)

PROXMOX_WRAPPER_COMMAND = 'proxmox-auto-install-assistant'


def prepare_proxmox_auto_install_iso(
    ctx: ActionContext,
    action: Dict[str, Any],
    context: Dict[str, Any],
    build_approval_error: Any,
) -> str:
    """Prepare a Proxmox auto-install ISO using proxmox-auto-install-assistant.

    Materialises required artifacts into a private staging directory, invokes
    the wrapper with a fixed argument layout, then cleans up.  The manifest
    never sees the staging paths.

    Args:
        ctx: Phase execution context (artifacts, allowed commands, etc.)
        action: Raw action dict from the manifest.
        context: Rendered template context for this action.
        build_approval_error: Callable(PendingPhaseApproval) -> RuntimeApprovalRequiredError,
            supplied by JsonSparkPlug to produce correctly attributed errors.

    Returns:
        The output ISO path string.
    """
    action_id = action.get('id', 'unknown')
    iso_path = ctx.template_engine.render(str(action.get('iso_path', '')), context)
    if not iso_path:
        raise RuntimeError(f"Action {action_id}: iso_path is required")

    output_path = ctx.template_engine.render(str(action.get('output_path', '')), context)
    if not output_path:
        raise RuntimeError(f"Action {action_id}: output_path is required")

    answer_artifact_id = str(action.get('answer_artifact', '')).strip()
    if not answer_artifact_id:
        raise RuntimeError(f"Action {action_id}: answer_artifact is required")

    answer_artifact = ctx.get_artifact(
        answer_artifact_id,
        action_id=action_id,
        expected_kinds=('config', 'generic'),
    )

    firstboot_artifact = None
    firstboot_artifact_id = str(action.get('firstboot_artifact', '')).strip()
    if firstboot_artifact_id:
        firstboot_artifact = ctx.get_artifact(
            firstboot_artifact_id,
            action_id=action_id,
            expected_kinds=('script', 'executable'),
        )

    with tempfile.TemporaryDirectory(prefix='spark-proxmox-') as tmpdir:
        staging_dir = Path(tmpdir)
        answer_path = ctx.materialize_artifact(answer_artifact, directory=staging_dir)

        cmd = [
            PROXMOX_WRAPPER_COMMAND,
            'prepare-iso',
            '--fetch-from',
            'iso',
            '--answer-file',
            str(answer_path),
        ]

        if firstboot_artifact is not None:
            firstboot_path = ctx.materialize_artifact(firstboot_artifact, directory=staging_dir)
            cmd.extend(['--on-first-boot', str(firstboot_path)])

        cmd.extend(['--output', output_path, iso_path])
        ctx.run_approved_command(
            cmd,
            use_sudo=bool(action.get('sudo', True)),
            output_path=output_path,
            build_approval_error=build_approval_error,
        )

    logger.info("Prepared Proxmox auto-install ISO")
    return output_path


def prepare_ubuntu_nocloud_iso(
    ctx: ActionContext,
    action: Dict[str, Any],
    context: Dict[str, Any],
    build_approval_error: Any,
) -> str:
    """Inject NoCloud cloud-init data into an Ubuntu ISO.

    Args:
        ctx: Phase execution context.
        action: Raw action dict from the manifest.
        context: Rendered template context for this action.
        build_approval_error: Unused for this scheme (no external commands);
            accepted for a uniform call signature.

    Returns:
        The output ISO path string.
    """
    action_id = action.get('id', 'unknown')
    iso_path = ctx.template_engine.render(str(action.get('iso_path', '')), context)
    output_path = ctx.template_engine.render(str(action.get('output_path', '')), context)
    if not iso_path:
        raise RuntimeError(f"Action {action_id}: iso_path is required")
    if not output_path:
        raise RuntimeError(f"Action {action_id}: output_path is required")

    user_data_artifact_id = str(action.get('user_data_artifact', '')).strip()
    meta_data_artifact_id = str(action.get('meta_data_artifact', '')).strip()
    if not user_data_artifact_id or not meta_data_artifact_id:
        raise RuntimeError(
            f"Action {action_id}: user_data_artifact and meta_data_artifact are required"
        )

    user_data_artifact = ctx.get_artifact(
        user_data_artifact_id,
        action_id=action_id,
        expected_kinds=('cloud_init', 'config', 'generic'),
    )
    meta_data_artifact = ctx.get_artifact(
        meta_data_artifact_id,
        action_id=action_id,
        expected_kinds=('cloud_init', 'config', 'generic'),
    )

    volume_label = str(action.get('volume_label', 'Ubuntu_Auto'))

    return inject_cloud_init_nocloud(
        iso_path=iso_path,
        user_data=user_data_artifact.content,
        meta_data=meta_data_artifact.content,
        output_path=output_path,
        volume_label=volume_label,
    )
