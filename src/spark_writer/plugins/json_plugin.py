"""JSON-based SparkPlug runtime implementation."""

import importlib.metadata
import json
import logging
import secrets
import shutil
import string
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from usb_writer_core import receipts as receipt_utils
from usb_writer_core import writer as usb_writer
from ..return_delivery import is_secure_return_url

from .action_context import (
    ActionContext,
    ManifestArtifact,
    RuntimeApprovalRequiredError,
)
from .base import PluginEventType, SparkPlug
from .json_plugin_approvals import APPROVAL_MODEL_VERSION, JsonPluginApprovalMixin
from .json_plugin_presets import JsonPluginPresetMixin
from .json_plugin_templates import JsonPluginTemplateMixin
from .manifest_schema import (
    LOCKED_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    validate_manifest_schema,
)
from .template_engine import SparkTemplateEngine
from . import installer_schemes

logger = logging.getLogger(__name__)

__all__ = ["APPROVAL_MODEL_VERSION", "JsonSparkPlug", "RuntimeApprovalRequiredError"]

SUPPORTED_MANIFEST_VERSIONS = SUPPORTED_SCHEMA_VERSIONS


class JsonSparkPlug(
    JsonPluginApprovalMixin,
    JsonPluginPresetMixin,
    JsonPluginTemplateMixin,
    SparkPlug,
):
    """SparkPlug implementation that executes JSON manifests.
    
    This class provides a secure runtime for declarative plugin manifests,
    supporting template rendering, command execution, and lifecycle hooks
    without importing arbitrary Python code.
    """

    _SUPPORTED_ACTION_TYPES = {
        'render_template',
        'run_command',
        'compute_file_hash',
        'create_partition',
        'write_partition_files',
        'generate_receipt',
        'format_yaml_list',
        'generate_ephemeral_password',
        'store_ephemeral_secret',
        'show_ephemeral_secret_button',
        'create_artifact',
        'prepare_installer_iso',
    }
    _RETIRED_ACTION_TYPES = {
        'write_file': "write_file is retired; use create_artifact plus a host-owned primitive instead.",
        'modify_iso': "modify_iso is retired; use create_artifact plus prepare_installer_iso.",
    }
    _PROXMOX_WRAPPER_COMMAND = 'proxmox-auto-install-assistant'

    def __init__(self, manifest_path: str):
        """Initialize from a JSON manifest file.
        
        Args:
            manifest_path: Path to the .json manifest file
        """
        super().__init__()
        self.manifest_path = manifest_path
        self.manifest: Dict[str, Any] = {}
        self.template_engine = SparkTemplateEngine()
        self._available = True
        self._unavailable_reason: Optional[str] = None
        self._plugin_allowed_commands: set = set()  # Plugin-specific commands user approved
        self._ephemeral_secrets: Dict[str, str] = {}  # In-memory secrets (cleared on app exit)
        self._active_phase_name: Optional[str] = None
        self._spark_writer_version = self._detect_spark_writer_version()
        self._exec_ctx = ActionContext(
            template_engine=self.template_engine,
            allowed_commands=self._plugin_allowed_commands,
            plugin_id='',  # updated after manifest load
        )

        self._load_and_validate()
        self._exec_ctx.plugin_id = self._plugin_id()
        self._load_approved_commands()

    def _load_and_validate(self) -> None:
        """Load and validate the JSON manifest."""
        try:
            with open(self.manifest_path, 'r', encoding='utf-8') as f:
                self.manifest = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load manifest {self.manifest_path}: {e}")
            self._available = False
            self._unavailable_reason = f"Invalid manifest: {e}"
            return

        # Validate required top-level fields
        manifest_version = str(self.manifest.get('version', '')).strip()
        if manifest_version not in SUPPORTED_MANIFEST_VERSIONS:
            self._available = False
            supported = ", ".join(SUPPORTED_MANIFEST_VERSIONS)
            self._unavailable_reason = (
                f"Unsupported manifest version: {manifest_version or 'missing'}; "
                f"this SparkWriter supports: {supported}"
            )
            return

        if 'metadata' not in self.manifest or 'requires' not in self.manifest:
            self._available = False
            self._unavailable_reason = "Missing required manifest fields"
            return

        legacy_secure_keys = [key for key in ("secure_manifest", "signature") if key in self.manifest]
        if legacy_secure_keys:
            self._available = False
            keys = ", ".join(sorted(legacy_secure_keys))
            self._unavailable_reason = (
                f"Unsupported manifest fields: {keys}. "
                "Secure manifest keys are deprecated; publish a plain manifest and reinstall it."
            )
            return

        try:
            validate_manifest_schema(self.manifest)
        except ValueError as exc:
            self._available = False
            self._unavailable_reason = f"Manifest schema validation failed: {exc}"
            return

        # Validate all templates are syntactically valid
        for template_name, template_value in self.manifest.get('templates', {}).items():
            try:
                template_str = self._resolve_template_string(template_value)
            except ValueError as exc:
                self._available = False
                self._unavailable_reason = f"Invalid template '{template_name}': {exc}"
                return
            if not self.template_engine.validate_template(template_str):
                self._available = False
                self._unavailable_reason = f"Invalid template syntax: {template_name}"
                return

        action_error = self._validate_manifest_actions()
        if action_error:
            self._available = False
            self._unavailable_reason = action_error
            return

        return_delivery_error = self._validate_return_delivery()
        if return_delivery_error:
            self._available = False
            self._unavailable_reason = return_delivery_error
            return

        wizard_error = self._validate_manifest_wizard()
        if wizard_error:
            self._available = False
            self._unavailable_reason = wizard_error
            return

        # Check for required external commands
        self._evaluate_availability()

    def _validate_manifest_actions(self) -> Optional[str]:
        """Return an availability error string for unsupported manifest actions."""

        for phase_name, actions in self.manifest.get('actions', {}).items():
            if not isinstance(actions, list):
                return f"Manifest phase '{phase_name}' must be an array of actions"

            for action in actions:
                action_id = action.get('id', 'unknown')
                action_type = action.get('type')
                if action_type in self._RETIRED_ACTION_TYPES:
                    return f"Action {action_id}: {self._RETIRED_ACTION_TYPES[action_type]}"
                if action_type not in self._SUPPORTED_ACTION_TYPES:
                    return f"Action {action_id}: unsupported action type '{action_type}'"

        return None

    def _validate_return_delivery(self) -> Optional[str]:
        spec = self.manifest.get("return_delivery")
        if not spec:
            return None
        if not isinstance(spec, dict):
            return "return_delivery must be an object"

        if not bool(spec.get("enabled", True)):
            return None

        secrets_spec = spec.get("secrets", [])
        if not isinstance(secrets_spec, list):
            return "return_delivery.secrets must be an array"
        for idx, key in enumerate(secrets_spec, start=1):
            if not str(key).strip():
                return f"return_delivery.secrets item {idx} must not be empty"

        endpoints = spec.get("endpoints", [])
        if endpoints is None:
            endpoints = []
        if not isinstance(endpoints, list):
            return "return_delivery.endpoints must be an array"
        seen_ids: set[str] = set()
        for idx, endpoint in enumerate(endpoints, start=1):
            if not isinstance(endpoint, dict):
                return f"return_delivery.endpoints item {idx} must be an object"
            endpoint_id = str(endpoint.get("id", "")).strip()
            label = str(endpoint.get("label", "")).strip()
            url = str(endpoint.get("url", "")).strip()
            if not endpoint_id or not label or not url:
                return f"return_delivery.endpoints item {idx} requires id, label, and url"
            if endpoint_id in seen_ids:
                return f"return_delivery endpoint '{endpoint_id}' is duplicated"
            seen_ids.add(endpoint_id)
            if not is_secure_return_url(url):
                return (
                    f"return_delivery endpoint '{endpoint_id}' must use HTTPS "
                    "or localhost HTTP"
                )

        return None

    def _validate_manifest_wizard(self) -> Optional[str]:
        """Return an availability error string for invalid wizard page metadata."""

        wizard = self.manifest.get("wizard", {})
        if not wizard:
            return None
        if not isinstance(wizard, dict):
            return "Manifest wizard must be an object"

        pages = wizard.get("pages", [])
        if not pages:
            return None
        if not isinstance(pages, list):
            return "Manifest wizard.pages must be an array"

        field_ids = {
            str(field.get("id", "")).strip()
            for field in self.manifest.get("config_fields", [])
            if isinstance(field, dict) and str(field.get("id", "")).strip()
        }
        page_ids: set[str] = set()
        seen_fields: set[str] = set()
        for idx, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                return f"Manifest wizard page {idx} must be an object"
            page_id = str(page.get("id", "")).strip()
            if not page_id:
                return f"Manifest wizard page {idx} requires id"
            if page_id in page_ids:
                return f"Manifest wizard page '{page_id}' is duplicated"
            page_ids.add(page_id)

            fields = page.get("fields", [])
            if not isinstance(fields, list):
                return f"Manifest wizard page '{page_id}' fields must be an array"
            for raw_field_id in fields:
                field_id = str(raw_field_id).strip()
                if field_id not in field_ids:
                    return f"Manifest wizard page '{page_id}' references unknown field '{field_id}'"
                if field_id in seen_fields:
                    return f"Manifest wizard field '{field_id}' is listed more than once"
                seen_fields.add(field_id)

        return None

    def _plugin_id(self) -> str:
        return str(self.manifest.get('metadata', {}).get('id', '')).strip()

    def _detect_spark_writer_version(self) -> str:
        """Return the installed spark-writer package version if available."""

        try:
            return importlib.metadata.version("spark-writer")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - dev installs
            return "unknown"

    def _evaluate_availability(self) -> None:
        """Check if required external commands are available.
        
        Note: Most operations use usb-writer-core, so plugins should rarely need
        external commands. Plugin-specific tools (like proxmox-auto-install-assistant)
        are automatically treated as such. 
        
        This method checks if commands exist on the system. The runtime execution
        in _execute_action() will check if commands are approved.
        """
        required_cmds = self.manifest.get('requires', {}).get('commands', [])
        missing = []
        
        for cmd_spec in required_cmds:
            cmd_name = cmd_spec.get('name')
            if not cmd_name:
                continue
            
            # Check if command exists on system
            if not shutil.which(cmd_name):
                package = cmd_spec.get('package', '')
                if package:
                    missing.append(f"missing {cmd_name}: sudo apt install -y {package}")
                else:
                    install_hint = cmd_spec.get('install_hint', '')
                    hint_str = f" ({install_hint})" if install_hint else ""
                    missing.append(f"missing {cmd_name}{hint_str}")

        if missing:
            self._available = False
            self._unavailable_reason = "; ".join(missing)
        else:
            self._available = True
            self._unavailable_reason = None

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    @property
    def name(self) -> str:
        return self.manifest.get('metadata', {}).get('name', 'Unknown Plugin')

    @property
    def plugin_id(self) -> str:
        return self._plugin_id() or self.name

    def requires_processing(self) -> bool:
        """Return True if plugin has on_iso_ready actions."""
        actions = self.manifest.get('actions', {})
        return bool(actions.get('on_iso_ready'))

    def supports_save_iso(self) -> bool:
        outputs = self.manifest.get('outputs', {})
        if not isinstance(outputs, dict):
            return True
        return bool(outputs.get('iso', True))

    def supports_usb_write(self) -> bool:
        outputs = self.manifest.get('outputs', {})
        if not isinstance(outputs, dict):
            return True
        return bool(outputs.get('usb', True))

    def get_config_fields(self) -> List[Dict[str, Any]]:
        """Return config fields from manifest."""
        return self.manifest.get('config_fields', [])

    def get_wizard_pages(self) -> List[Dict[str, Any]]:
        wizard = self.manifest.get("wizard", {})
        if not isinstance(wizard, dict):
            return []
        pages = wizard.get("pages", [])
        if not isinstance(pages, list):
            return []
        return [page for page in pages if isinstance(page, dict)]

    def _reset_phase_state(self, initial_action_vars: Optional[Dict[str, Any]] = None) -> None:
        self._exec_ctx.active_phase = self._active_phase_name or "current"
        self._exec_ctx.reset(initial_action_vars)

    def _clear_phase_state(self) -> None:
        self._exec_ctx.clear()

    def _handle_render_template(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        template_name = action.get('template')
        if not template_name:
            logger.error(f"Action {action_id}: missing template name")
            return None

        template_value = self.manifest.get('templates', {}).get(template_name)
        if template_value is None:
            logger.error(f"Action {action_id}: template '{template_name}' not found")
            return None

        try:
            template_str = self._resolve_template_string(template_value)
        except ValueError as exc:
            raise RuntimeError(f"Action {action_id}: {exc}") from exc

        result = self.template_engine.render(template_str, context)
        logger.debug(f"Rendered template {template_name}")
        return result

    def _handle_create_artifact(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        artifact_id = str(action.get('artifact_id', '')).strip()
        if not artifact_id:
            raise RuntimeError(f"Action {action_id}: artifact_id is required")
        if artifact_id in self._exec_ctx.artifacts:
            raise RuntimeError(f"Action {action_id}: artifact '{artifact_id}' already exists")

        content = self._exec_ctx.resolve_artifact_content(action, context)
        kind = str(action.get('kind', 'generic')).strip() or 'generic'
        logical_name = self._exec_ctx.validate_artifact_name(
            action_id,
            str(action.get('logical_name') or artifact_id),
        )
        media_type = action.get('media_type')
        executable = bool(action.get('executable', False))

        if executable and kind not in {'script', 'executable'}:
            raise RuntimeError(
                f"Action {action_id}: executable artifacts must declare kind 'script' or 'executable'"
            )

        artifact = ManifestArtifact(
            artifact_id=artifact_id,
            content=content,
            kind=kind,
            logical_name=logical_name,
            media_type=str(media_type) if media_type is not None else None,
            executable=executable,
        )
        self._exec_ctx.store_artifact(artifact)
        logger.info(f"Created artifact: {artifact_id}")
        return None

    def _handle_run_command(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        cmd_template = action.get('command', [])
        use_sudo = action.get('sudo', False)

        cmd = [self.template_engine.render(arg, context) for arg in cmd_template]
        output_path = None
        if '--output' in cmd:
            output_idx = cmd.index('--output')
            if output_idx + 1 < len(cmd):
                output_path = cmd[output_idx + 1]

        return self._exec_ctx.run_approved_command(
            cmd,
            use_sudo=use_sudo,
            output_path=output_path,
            build_approval_error=self._build_runtime_approval_error,
        )

    def _handle_prepare_installer_iso(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        return installer_schemes.prepare_installer_iso(
            self._exec_ctx, action, context, self._build_runtime_approval_error
        )

    def _handle_compute_file_hash(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        path_template = action.get('path')
        if not path_template:
            raise RuntimeError(f"Action {action_id}: 'path' is required for compute_file_hash")

        rendered_path = self.template_engine.render(path_template, context)
        algorithm = str(action.get('algorithm', 'sha256'))
        hash_value = self._hash_file(Path(rendered_path).expanduser(), algorithm)
        logger.info(f"Computed {algorithm} hash for {rendered_path}")
        return hash_value

    def _handle_create_partition(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        device_path = context.get('device_path')
        if not device_path:
            raise RuntimeError("create_partition requires a USB device path")

        label_template = action.get('label')
        if not label_template:
            raise RuntimeError(f"Action {action_id}: 'label' is required for create_partition")

        label = self.template_engine.render(label_template, context)
        rendered_size = self._render_value(action.get('size_mb', 100), context)
        try:
            size_mb = int(rendered_size)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Action {action_id}: size_mb must be an integer") from exc

        partition_type = str(self._render_value(action.get('partition_type', '0700'), context))
        skip_if_exists = bool(action.get('skip_if_exists', True))

        if skip_if_exists and usb_writer.partition_exists(device_path, label):
            logger.info(f"Partition {label} already present; skipping creation")
        else:
            usb_writer.create_aux_partition(
                device_path,
                label,
                size_mb=size_mb,
                partition_type=partition_type,
            )
            logger.info(f"Created partition {label} ({size_mb} MB)")
        return None

    def _handle_write_partition_files(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        device_path = context.get('device_path')
        if not device_path:
            raise RuntimeError("write_partition_files requires a USB device path")

        label_template = action.get('partition_label')
        if not label_template:
            raise RuntimeError(f"Action {action_id}: 'partition_label' is required")

        partition_label = self.template_engine.render(label_template, context)
        files: Dict[str, str] = {}

        files_var = action.get('files_var')
        if files_var:
            bundle = self._exec_ctx.action_vars.get(files_var)
            if not isinstance(bundle, dict):
                raise RuntimeError(f"Action {action_id}: files_var '{files_var}' not found")
            files = {str(name): str(content) for name, content in bundle.items()}
        else:
            files_spec = action.get('files', {})
            if not isinstance(files_spec, dict):
                raise RuntimeError(f"Action {action_id}: 'files' must be an object")
            for filename, content_template in files_spec.items():
                rendered_name = self.template_engine.render(str(filename), context)
                rendered_content = self.template_engine.render(str(content_template), context)
                files[rendered_name] = rendered_content

        if not files:
            logger.info(f"No files to write for partition {partition_label}; skipping")
        else:
            usb_writer.write_files_to_partition(device_path, partition_label, files)
            logger.info(f"Wrote {len(files)} file(s) to partition {partition_label}")
        return None

    def _handle_generate_receipt(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        action_id = action.get('id', 'unknown')
        signing_spec = action.get('signing', {})
        private_key_value = signing_spec.get('private_key')
        if not private_key_value:
            raise RuntimeError(f"Action {action_id}: signing.private_key is required")

        private_key = str(self._render_value(private_key_value, context))
        signature_encoding = str(self._render_value(signing_spec.get('encoding', 'base64'), context))

        signing_key = receipt_utils.load_signing_key(private_key)
        public_key_override = signing_spec.get('public_key')
        if public_key_override:
            encoded_public_key = str(self._render_value(public_key_override, context))
        else:
            encoded_public_key = receipt_utils.encode_public_key(signing_key, signature_encoding)

        public_key_field = str(signing_spec.get('public_key_field', 'receipt_public_key'))

        identity = self._render_mapping(action.get('identity', {}), context)
        format_version = str(self._render_value(action.get('format_version', '1.0'), context))
        identity.setdefault('receipt_format_version', format_version)

        metadata = self.manifest.get('metadata', {})
        identity.setdefault('sparkplug_id', metadata.get('id', 'unknown'))
        identity.setdefault('sparkplug_version', metadata.get('version', 'unknown'))
        identity.setdefault('spark_writer_version', self._spark_writer_version)
        identity.setdefault(public_key_field, encoded_public_key)

        inputs_spec = action.get('inputs', {})
        public_inputs = self._render_mapping(inputs_spec.get('public', {}), context)

        redacted_raw = self._render_value(inputs_spec.get('redacted', []), context)
        if isinstance(redacted_raw, str):
            redacted_fields = [redacted_raw]
        elif isinstance(redacted_raw, list):
            redacted_fields = [str(item) for item in redacted_raw]
        else:
            redacted_fields = []

        fingerprints: Dict[str, str] = {}
        fp_spec = inputs_spec.get('keyed_fingerprints', {})
        if fp_spec:
            if 'fields' in fp_spec and 'key' in fp_spec:
                fingerprint_key = str(self._render_value(fp_spec['key'], context))
                algorithm = str(fp_spec.get('algorithm', 'sha256'))
                fp_encoding = str(fp_spec.get('encoding', signature_encoding))
                fields = fp_spec.get('fields', [])
                for raw_name in fields:
                    field_name = str(self._render_value(raw_name, context))
                    value = public_inputs.get(field_name)
                    if value is None and field_name in context:
                        value = context[field_name]
                    if value is None:
                        raise RuntimeError(
                            f"Action {action_id}: cannot fingerprint undefined field '{field_name}'"
                        )
                    fingerprints[field_name] = receipt_utils.hmac_fingerprint(
                        fingerprint_key,
                        str(value),
                        algorithm=algorithm,
                        encoding=fp_encoding,
                    )
            else:
                fingerprints = self._render_mapping(fp_spec.get('values', {}), context)

        inputs_section: Dict[str, Any] = {}
        if public_inputs:
            inputs_section['public'] = public_inputs
        if redacted_fields:
            inputs_section['redacted'] = redacted_fields
        if fingerprints:
            inputs_section['keyed_fingerprints'] = fingerprints

        artifacts = self._render_mapping(action.get('artifacts', {}), context)

        run_metadata = self._render_mapping(action.get('run_metadata', {}), context)
        if 'timestamp' not in run_metadata:
            run_metadata['timestamp'] = receipt_utils.current_timestamp()

        if 'nonce' not in run_metadata:
            raw_nonce_bytes = self._render_value(action.get('nonce_bytes', 16), context)
            try:
                nonce_bytes = int(raw_nonce_bytes)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Action {action_id}: nonce_bytes must be an integer") from exc
            nonce_encoding = str(self._render_value(action.get('nonce_encoding', 'hex'), context))
            run_metadata['nonce'] = receipt_utils.generate_nonce(nonce_bytes, nonce_encoding)

        chain_section = self._render_mapping(action.get('chain', {}), context)

        receipt_payload: Dict[str, Any] = {
            'identity': identity,
            'artifacts': artifacts,
            'run_metadata': run_metadata,
        }
        if inputs_section:
            receipt_payload['inputs'] = inputs_section
        if chain_section:
            receipt_payload['chain'] = chain_section

        canonical_json = receipt_utils.canonicalize_receipt(receipt_payload)
        receipt_hash = receipt_utils.compute_receipt_hash(canonical_json)
        signature = receipt_utils.sign_with_key(signing_key, canonical_json, encoding=signature_encoding)

        receipt_filename = str(self._render_value(action.get('receipt_filename', 'receipt.json'), context))
        signature_filename = str(self._render_value(action.get('signature_filename', 'receipt.sig'), context))

        files_bundle = {
            receipt_filename: canonical_json,
            signature_filename: signature,
        }

        json_var = action.get('json_output_var')
        if json_var:
            self._exec_ctx.action_vars[json_var] = canonical_json

        signature_var = action.get('signature_output_var')
        if signature_var:
            self._exec_ctx.action_vars[signature_var] = signature

        hash_var = action.get('hash_output_var')
        if hash_var:
            self._exec_ctx.action_vars[hash_var] = receipt_hash

        files_var_name = action.get('files_output_var') or signing_spec.get('files_output_var')
        if files_var_name:
            self._exec_ctx.action_vars[files_var_name] = files_bundle

        public_key_var = action.get('public_key_output_var') or signing_spec.get('public_key_output_var')
        if public_key_var:
            self._exec_ctx.action_vars[public_key_var] = encoded_public_key

        logger.info(f"Generated receipt payload ({len(canonical_json)} bytes)")
        return canonical_json

    def _handle_format_yaml_list(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        # Convert newline or space-separated list to YAML list format
        input_str = self.template_engine.render(action.get('input', ''), context)
        default_str = action.get('default', '')
        indent = action.get('indent', 0)

        if not input_str.strip() and default_str:
            input_str = default_str

        if '\n' in input_str:
            items = [line.strip() for line in input_str.split('\n') if line.strip()]
        else:
            items = [item.strip() for item in input_str.split() if item.strip()]

        yaml_lines = [f"{' ' * indent}- {item}" for item in items]
        result = '\n'.join(yaml_lines)
        logger.info(f"Formatted {len(items)} items as YAML list")
        return result

    def _handle_generate_ephemeral_password(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        # Use cryptographically secure random generation for one-time secrets.
        action_id = action.get('id', 'unknown')
        raw_length = self._render_value(action.get('length', 20), context)
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Action {action_id}: length must be an integer") from exc
        if length < 1:
            raise RuntimeError(f"Action {action_id}: length must be >= 1")

        charset = action.get('charset')
        if charset:
            charset = str(self._render_value(charset, context))
        else:
            charset = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        if not charset:
            raise RuntimeError(f"Action {action_id}: charset must not be empty")

        result = ''.join(secrets.choice(charset) for _ in range(length))
        logger.info(f"Generated ephemeral password ({length} chars)")
        return result

    def _handle_store_ephemeral_secret(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        # Store secret in memory (cleared on app exit)
        key = action.get('key')
        value = self.template_engine.render(action.get('value', ''), context)
        if key:
            self._ephemeral_secrets[key] = value
            logger.info(f"Stored ephemeral secret")
        return None

    def _handle_show_ephemeral_secret_button(
        self, action: Dict[str, Any], context: Dict[str, Any]
    ) -> Optional[str]:
        # This is handled by the UI layer after write completes
        # Just validate the action has required fields
        key = action.get('key')
        if key not in self._ephemeral_secrets:
            logger.warning(f"Ephemeral secret not found")
        return None

    def _execute_action(
        self,
        action: Dict[str, Any],
        ui_values: Dict[str, Any],
        preset: Dict[str, Any],
        iso_path: Optional[str] = None,
        device_path: Optional[str] = None,
    ) -> Optional[str]:
        """Execute a single action from the manifest.
        
        Args:
            action: Action definition dict
            ui_values: User-provided config values
            preset: Preset data
            iso_path: Current ISO path (for on_iso_ready)
            device_path: Device path (for on_write_complete)
            
        Returns:
            Output value if action produces one (e.g., modified ISO path)
        """
        action_id = action.get('id', 'unknown')
        action_type = action.get('type')

        # Check condition
        condition = action.get('when')
        if condition and not self._evaluate_condition(condition, ui_values):
            logger.debug(f"Skipping action {action_id} due to condition")
            return None

        # Prepare template context
        context = self._build_template_context(ui_values, preset, iso_path, device_path)
        # Keep compatibility with templates that expect a precomputed password fallback.
        if 'effective_root_password' not in context:
            context['effective_root_password'] = (
                str(ui_values.get('root-password') or '').strip()
                or str(self._exec_ctx.action_vars.get('_generated_root_password_plaintext') or '').strip()
                or 'AUTO_GENERATED'
            )

        # Emit event if specified
        emit_spec = action.get('emit_event')
        if emit_spec:
            self.emit_event(
                message=self.template_engine.render(emit_spec.get('message', ''), context),
                progress=emit_spec.get('progress'),
                event_type=PluginEventType.UPDATE,
            )

        _dispatch = {
            'render_template':             self._handle_render_template,
            'run_command':                 self._handle_run_command,
            'compute_file_hash':           self._handle_compute_file_hash,
            'create_partition':            self._handle_create_partition,
            'write_partition_files':       self._handle_write_partition_files,
            'generate_receipt':            self._handle_generate_receipt,
            'format_yaml_list':            self._handle_format_yaml_list,
            'generate_ephemeral_password': self._handle_generate_ephemeral_password,
            'store_ephemeral_secret':      self._handle_store_ephemeral_secret,
            'show_ephemeral_secret_button': self._handle_show_ephemeral_secret_button,
            'create_artifact':            self._handle_create_artifact,
            'prepare_installer_iso':       self._handle_prepare_installer_iso,
        }

        handler = _dispatch.get(action_type)
        if not handler:
            raise RuntimeError(f"Action {action_id}: unsupported action type '{action_type}'")

        result = handler(action, context)

        # Store result in action variables if requested
        output_var = action.get('output_var')
        if output_var and result is not None:
            self._exec_ctx.action_vars[output_var] = result

        return result

    def on_iso_ready(self, iso_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]) -> str:
        """Execute on_iso_ready actions from manifest."""
        if not self.is_available:
            raise RuntimeError(self.unavailable_reason or "Plugin unavailable")

        actions = self.manifest.get('actions', {}).get('on_iso_ready', [])
        if not actions:
            return iso_path

        self._ensure_phase_runtime_approval("on_iso_ready", actions)

        self.emit_event(
            event_type=PluginEventType.START,
            message=f"Processing ISO with {self.name}",
        )

        self._active_phase_name = "on_iso_ready"
        self._reset_phase_state({'original_iso_path': iso_path})
        current_iso = iso_path

        try:
            for action in actions:
                result = self._execute_action(
                    action=action,
                    ui_values=ui_values,
                    preset=preset,
                    iso_path=current_iso,
                )
                
                # If action returns a new ISO path, use it
                if result and result.endswith('.iso'):
                    current_iso = result

            self.emit_event(
                event_type=PluginEventType.COMPLETE,
                message=f"ISO processing complete",
            )
            
            return current_iso

        except Exception as e:
            self.emit_event(
                event_type=PluginEventType.ERROR,
                message=f"Failed: {str(e)}",
            )
            raise
        finally:
            self._active_phase_name = None
            self._clear_phase_state()

    def on_write_complete(
        self, device_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]
    ) -> None:
        """Execute on_write_complete actions from manifest."""
        actions = self.manifest.get('actions', {}).get('on_write_complete', [])
        if not actions:
            return

        self._ensure_phase_runtime_approval("on_write_complete", actions)

        self.emit_event(
            event_type=PluginEventType.START,
            message=f"Post-write processing with {self.name}",
        )

        self._active_phase_name = "on_write_complete"
        self._reset_phase_state()

        try:
            for action in actions:
                self._execute_action(
                    action=action,
                    ui_values=ui_values,
                    preset=preset,
                    device_path=device_path,
                )

            self.emit_event(
                event_type=PluginEventType.COMPLETE,
                message="Post-write processing complete",
            )

        except Exception as e:
            self.emit_event(
                event_type=PluginEventType.ERROR,
                message=f"Failed: {str(e)}",
            )
            raise
        finally:
            self._active_phase_name = None
            self._clear_phase_state()
    
    def get_ephemeral_secrets(self) -> Dict[str, str]:
        """Return all stored ephemeral secrets.
        
        Returns:
            Dictionary of secret keys and values stored in memory
        """
        return self._ephemeral_secrets.copy()

    def get_return_delivery_spec(self) -> Dict[str, Any]:
        """Return the manifest-declared return delivery contract, if enabled."""

        spec = self.manifest.get("return_delivery")
        if not isinstance(spec, dict) or not bool(spec.get("enabled", True)):
            return {}

        secrets_spec = spec.get("secrets", [])
        endpoints_spec = spec.get("endpoints", [])
        secrets_list = [
            str(key).strip()
            for key in secrets_spec
            if str(key).strip()
        ] if isinstance(secrets_spec, list) else []

        endpoints: List[Dict[str, str]] = []
        if isinstance(endpoints_spec, list):
            for endpoint in endpoints_spec:
                if not isinstance(endpoint, dict):
                    continue
                endpoints.append(
                    {
                        "id": str(endpoint.get("id", "")).strip(),
                        "label": str(endpoint.get("label", "")).strip(),
                        "url": str(endpoint.get("url", "")).strip(),
                    }
                )

        return {
            "enabled": True,
            "secrets": secrets_list,
            "endpoints": endpoints,
        }

    def requires_return_delivery(self) -> bool:
        spec = self.get_return_delivery_spec()
        return bool(spec.get("enabled") and spec.get("secrets"))
    
    def destroy_ephemeral_secret(self, key: str) -> bool:
        """Destroy an ephemeral secret from memory.
        
        Args:
            key: Secret key to destroy
            
        Returns:
            True if secret was destroyed, False if not found
        """
        if key in self._ephemeral_secrets:
            del self._ephemeral_secrets[key]
            logger.info(f"Destroyed ephemeral secret")
            return True
        return False
    
    def has_ephemeral_secrets(self) -> bool:
        """Check if plugin has any ephemeral secrets stored.
        
        Returns:
            True if secrets exist, False otherwise
        """
        return len(self._ephemeral_secrets) > 0

    def get_declared_artifact_ids(self) -> List[str]:
        artifact_ids: List[str] = []
        seen: set[str] = set()
        for actions in self.manifest.get("actions", {}).values():
            if not isinstance(actions, list):
                continue
            for action in actions:
                artifact_id = str(action.get("artifact_id", "")).strip()
                if artifact_id and artifact_id not in seen:
                    seen.add(artifact_id)
                    artifact_ids.append(artifact_id)
        return artifact_ids

    def get_declared_host_action_types(self) -> List[str]:
        host_owned = {
            "prepare_installer_iso",
        }
        action_types: List[str] = []
        seen: set[str] = set()
        for actions in self.manifest.get("actions", {}).values():
            if not isinstance(actions, list):
                continue
            for action in actions:
                action_type = str(action.get("type", "")).strip()
                if action_type in host_owned and action_type not in seen:
                    seen.add(action_type)
                    action_types.append(action_type)
        return action_types
