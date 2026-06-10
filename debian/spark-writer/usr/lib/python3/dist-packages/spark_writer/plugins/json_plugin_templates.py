"""Template, condition, and runtime-context helpers for JSON SparkPlugs."""

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .manifest_assets import normalize_sidecar_ref, resolve_asset_path


class JsonPluginTemplateMixin:
    """Shared helpers for rendering manifest values."""

    manifest: dict[str, Any]
    manifest_path: str

    def should_show_ui(self, preset_id: str, preset_data: Dict[str, Any]) -> bool:
        """Determine if plugin UI should be shown based on manifest rules."""
        visibility = self.manifest.get('ui_visibility', {}).get('when', {})

        source_id = str(
            preset_data.get('source_id') or preset_data.get('id') or preset_id or ''
        ).strip()
        source_family = str(
            preset_data.get('source_family') or preset_data.get('family') or preset_data.get('distro') or ''
        ).lower()
        installer_scheme = str(preset_data.get('installer_scheme') or '').strip()
        source_capabilities = {
            str(item).strip()
            for item in preset_data.get('source_capabilities', preset_data.get('capabilities', []))
            if str(item).strip()
        }

        allowed_source_ids = visibility.get('source_id', [])
        if allowed_source_ids and source_id not in allowed_source_ids:
            return False

        allowed_source_families = visibility.get('source_family', [])
        if allowed_source_families:
            if source_family not in [str(item).lower() for item in allowed_source_families]:
                return False

        allowed_schemes = visibility.get('installer_scheme', [])
        if allowed_schemes and installer_scheme not in allowed_schemes:
            return False

        required_capabilities = visibility.get('source_capabilities', [])
        if required_capabilities:
            normalized_requirements = {
                str(item).strip() for item in required_capabilities if str(item).strip()
            }
            if not normalized_requirements.issubset(source_capabilities):
                return False

        # Compatibility aliases for legacy manifests.
        allowed_distros = visibility.get('preset_distro', [])
        if allowed_distros:
            if source_family not in [str(d).lower() for d in allowed_distros]:
                return False

        allowed_presets = visibility.get('preset_id', [])
        if allowed_presets and source_id not in allowed_presets:
            return False

        return True

    def _evaluate_condition(self, condition: Dict[str, Any], ui_values: Dict[str, Any]) -> bool:
        """Evaluate a manifest conditional expression."""
        field_id = condition.get('field')
        operator = condition.get('operator')
        expected = condition.get('value')

        if not field_id or not operator:
            return True

        actual = ui_values.get(field_id)

        if operator == 'not_empty':
            return bool(actual and str(actual).strip())
        if operator == 'empty':
            return not actual or not str(actual).strip()
        if operator == 'equals':
            return actual == expected
        if operator == 'not_equals':
            return actual != expected
        if operator == 'in':
            return actual in (expected if isinstance(expected, list) else [expected])
        if operator == 'not_in':
            return actual not in (expected if isinstance(expected, list) else [expected])

        return True

    def _resolve_template_string(self, template_value: Any) -> str:
        """Resolve inline, line-array, sidecar-file, or asset-backed template values."""
        if isinstance(template_value, str):
            return template_value
        if isinstance(template_value, list):
            return "\n".join(str(line) for line in template_value)
        if isinstance(template_value, dict):
            assets = self.manifest.get("assets", {})
            if assets is None:
                assets = {}
            if not isinstance(assets, dict):
                raise ValueError("Manifest assets section must be an object")

            file_ref = template_value.get("file")
            asset_ref = template_value.get("asset")

            if file_ref and asset_ref:
                raise ValueError("Template dict must not define both 'file' and 'asset'")
            if file_ref:
                relative_path = normalize_sidecar_ref(file_ref)
            elif asset_ref:
                relative_path = resolve_asset_path(assets, asset_ref)
            else:
                raise ValueError("Template dict must have a 'file' or 'asset' key")

            manifest_dir = os.path.dirname(os.path.abspath(self.manifest_path))
            sidecar_path = os.path.join(manifest_dir, relative_path)
            try:
                with open(sidecar_path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError as exc:
                raise ValueError(f"Cannot read template file '{relative_path}': {exc}") from exc
        raise ValueError(f"Unsupported template value type: {type(template_value).__name__}")

    def _render_value(self, value: Any, context: Dict[str, Any]) -> Any:
        """Render template values recursively."""

        if isinstance(value, str):
            return self.template_engine.render(value, context)
        if isinstance(value, dict):
            return {k: self._render_value(v, context) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render_value(item, context) for item in value]
        return value

    def _render_mapping(self, mapping: Optional[Dict[str, Any]], context: Dict[str, Any]) -> Dict[str, Any]:
        if not mapping:
            return {}
        return {key: self._render_value(val, context) for key, val in mapping.items()}

    def _config_field_defaults(self) -> Dict[str, Any]:
        """Return default values for declared config fields."""

        defaults: Dict[str, Any] = {}
        for field in self.manifest.get('config_fields', []):
            field_id = field.get('id')
            if not field_id:
                continue
            defaults[str(field_id)] = field.get('default', '')
        return defaults

    def _build_template_context(
        self,
        ui_values: Dict[str, Any],
        preset: Dict[str, Any],
        iso_path: Optional[str],
        device_path: Optional[str],
    ) -> Dict[str, Any]:
        """Build template context with declared-field defaults and runtime values."""

        context = {
            **self._config_field_defaults(),
            **ui_values,
            **self._exec_ctx.action_vars,
            'iso_path': iso_path,
            'device_path': device_path,
            'preset_id': preset.get('id', ''),
            'preset_name': preset.get('name', ''),
            'source_id': preset.get('source_id', preset.get('id', '')),
            'source_name': preset.get('source_name', preset.get('name', '')),
            'source_family': preset.get('source_family', preset.get('family', preset.get('distro', ''))),
            'source_url': preset.get('source_url', preset.get('url', '')),
            'source_version': preset.get('source_version', preset.get('version', '')),
            'installer_scheme': preset.get('installer_scheme', ''),
            'source_capabilities': preset.get('source_capabilities', preset.get('capabilities', [])),
        }

        # Compatibility bridge for legacy template key naming.
        if 'apt-cache' not in context and 'apt-proxy' in context:
            context['apt-cache'] = context.get('apt-proxy', '')
        if 'apt-proxy' not in context and 'apt-cache' in context:
            context['apt-proxy'] = context.get('apt-cache', '')

        return context

    def _hash_file(self, file_path: Path, algorithm: str) -> str:
        """Return hex digest for the provided file."""

        if not file_path.exists():
            raise RuntimeError(f"File not found: {file_path}")

        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            raise RuntimeError(f"Unsupported hash algorithm: {algorithm}") from exc

        with file_path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
