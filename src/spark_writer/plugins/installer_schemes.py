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


def _render_value(ctx: ActionContext, value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        return ctx.template_engine.render(value, context)
    if isinstance(value, dict):
        return {key: _render_value(ctx, val, context) for key, val in value.items()}
    if isinstance(value, list):
        return [_render_value(ctx, item, context) for item in value]
    return value


def _mapping(action: Dict[str, Any], key: str, context: Dict[str, Any], ctx: ActionContext) -> Dict[str, Any]:
    raw_mapping = action.get(key, {})
    if raw_mapping is None:
        return {}
    if not isinstance(raw_mapping, dict):
        action_id = action.get('id', 'unknown')
        raise RuntimeError(f"Action {action_id}: {key} must be an object")
    rendered = _render_value(ctx, raw_mapping, context)
    if not isinstance(rendered, dict):
        action_id = action.get('id', 'unknown')
        raise RuntimeError(f"Action {action_id}: {key} must render to an object")
    return rendered


def _artifact_ref(
    action: Dict[str, Any],
    role: str,
    *,
    legacy_key: str,
    context: Dict[str, Any],
    ctx: ActionContext,
) -> str:
    artifact_map = _mapping(action, 'artifact_map', context, ctx)
    artifact_id = artifact_map.get(role)
    if artifact_id is None:
        artifact_id = action.get(legacy_key, '')
    return str(_render_value(ctx, artifact_id, context)).strip()


def _option(
    action: Dict[str, Any],
    name: str,
    default: Any,
    *,
    context: Dict[str, Any],
    ctx: ActionContext,
) -> Any:
    options = _mapping(action, 'options', context, ctx)
    return options.get(name, default)


def prepare_installer_iso(
    ctx: ActionContext,
    action: Dict[str, Any],
    context: Dict[str, Any],
    build_approval_error: Any,
) -> str:
    """Prepare installer media using a generic scheme selector."""

    action_id = action.get('id', 'unknown')
    scheme = ctx.template_engine.render(str(action.get('installer_scheme', '')), context).strip()
    if scheme == 'ubuntu-nocloud':
        return prepare_ubuntu_nocloud_iso(ctx, action, context, build_approval_error)
    if scheme == 'proxmox-auto-install':
        return prepare_proxmox_auto_install_iso(ctx, action, context, build_approval_error)
    if not scheme:
        raise RuntimeError(f"Action {action_id}: installer_scheme is required")
    raise RuntimeError(f"Action {action_id}: unsupported installer_scheme '{scheme}'")


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

    answer_artifact_id = _artifact_ref(
        action,
        'answer-file',
        legacy_key='answer_artifact',
        context=context,
        ctx=ctx,
    )
    if not answer_artifact_id:
        raise RuntimeError(f"Action {action_id}: artifact_map.answer-file is required")

    answer_artifact = ctx.get_artifact(
        answer_artifact_id,
        action_id=action_id,
        expected_kinds=('config', 'generic'),
    )

    firstboot_artifact = None
    firstboot_artifact_id = _artifact_ref(
        action,
        'first-boot',
        legacy_key='firstboot_artifact',
        context=context,
        ctx=ctx,
    )
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

    user_data_artifact_id = _artifact_ref(
        action,
        'user-data',
        legacy_key='user_data_artifact',
        context=context,
        ctx=ctx,
    )
    meta_data_artifact_id = _artifact_ref(
        action,
        'meta-data',
        legacy_key='meta_data_artifact',
        context=context,
        ctx=ctx,
    )
    if not user_data_artifact_id or not meta_data_artifact_id:
        raise RuntimeError(
            f"Action {action_id}: artifact_map.user-data and artifact_map.meta-data are required"
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

    volume_label = str(
        _option(
            action,
            'volume_label',
            action.get('volume_label', 'Ubuntu_Auto'),
            context=context,
            ctx=ctx,
        )
    )

    return inject_cloud_init_nocloud(
        iso_path=iso_path,
        user_data=user_data_artifact.content,
        meta_data=meta_data_artifact.content,
        output_path=output_path,
        volume_label=volume_label,
    )
