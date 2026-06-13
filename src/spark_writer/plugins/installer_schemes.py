"""Host-owned installer scheme primitives for SparkPlug manifest execution.

Each function corresponds to one installer scheme action type.  They are
deliberately module-level so that new schemes can be added here without
growing JsonSparkPlug, and so they can be unit-tested without a full
manifest instance.
"""

import logging
from typing import Any, Dict, Optional

from ..core.iso_utils import inject_cloud_init_nocloud

from .action_context import ActionContext

logger = logging.getLogger(__name__)

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
        raise RuntimeError(
            f"Action {action_id}: Proxmox preparation must use the run_builder action"
        )
    if not scheme:
        raise RuntimeError(f"Action {action_id}: installer_scheme is required")
    raise RuntimeError(f"Action {action_id}: unsupported installer_scheme '{scheme}'")


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
