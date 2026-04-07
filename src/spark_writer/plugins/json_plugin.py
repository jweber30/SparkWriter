"""JSON-based SparkPlug runtime implementation."""

import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from usb_writer_core import receipts as receipt_utils
from usb_writer_core import writer as usb_writer

from .base import ConfigField, ConfigOption, PluginEventType, SparkPlug
from .template_engine import SparkTemplateEngine

logger = logging.getLogger(__name__)


class JsonSparkPlug(SparkPlug):
    """SparkPlug implementation that executes JSON manifests.
    
    This class provides a secure runtime for declarative plugin manifests,
    supporting template rendering, command execution, and lifecycle hooks
    without importing arbitrary Python code.
    """

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
        self._action_vars: Dict[str, Any] = {}  # Variables from action outputs
        self._plugin_allowed_commands: set = set()  # Plugin-specific commands user approved
        self._ephemeral_secrets: Dict[str, str] = {}  # In-memory secrets (cleared on app exit)
        self._spark_writer_version = self._detect_spark_writer_version()
        
        self._load_and_validate()
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
        if self.manifest.get('version') != '1.0':
            self._available = False
            self._unavailable_reason = "Unsupported manifest version"
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

        # Validate all templates are syntactically valid
        for template_name, template_str in self.manifest.get('templates', {}).items():
            if not self.template_engine.validate_template(template_str):
                self._available = False
                self._unavailable_reason = f"Invalid template syntax: {template_name}"
                return

        # Check for required external commands
        self._evaluate_availability()
    
    def _load_approved_commands(self) -> None:
        """Load user-approved commands from the approval metadata file."""
        # Find approval file in same directory as manifest
        manifest_dir = os.path.dirname(self.manifest_path)
        plugin_id = self.manifest.get('metadata', {}).get('id', '')
        
        if not plugin_id:
            return
        
        approval_file = os.path.join(manifest_dir, f".{plugin_id}.approval")
        
        if not os.path.exists(approval_file):
            # No approval file - plugin installed before approval system or has no plugin-specific commands
            logger.debug(f"No approval file found for {plugin_id}")
            return
        
        try:
            with open(approval_file, 'r') as f:
                approval_data = json.load(f)
            
            approved = approval_data.get('approved_commands', [])
            self._plugin_allowed_commands.update(approved)
            
            if approved:
                logger.info(f"Loaded approved commands for {plugin_id}: {', '.join(approved)}")
        
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load approval file for {plugin_id}: {e}")

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
                install_hint = cmd_spec.get('install_hint', '')
                hint_str = f" ({install_hint})" if install_hint else ""
                missing.append(f"{cmd_name}{hint_str}")

        if missing:
            self._available = False
            self._unavailable_reason = "Missing required commands: " + ", ".join(missing)
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

    def requires_processing(self) -> bool:
        """Return True if plugin has on_iso_ready actions."""
        actions = self.manifest.get('actions', {})
        return bool(actions.get('on_iso_ready'))

    def register_presets(self) -> Dict[str, Any]:
        """Return presets defined in manifest, including those from remote feeds."""
        presets = {}
        
        # Load presets from remote feeds first
        for feed_spec in self.manifest.get('preset_feeds', []):
            feed_url = feed_spec.get('url', '')
            if not feed_url.startswith('https://'):
                logger.warning(f"Skipping non-HTTPS feed: {feed_url}")
                continue
                
            try:
                feed_presets = self._fetch_preset_feed(feed_url)
                presets.update(feed_presets)
                logger.info(f"Loaded {len(feed_presets)} presets from {feed_url}")
            except Exception as e:
                logger.error(f"Failed to fetch preset feed {feed_url}: {e}")
                # Continue loading other feeds
        
        # Load static presets from manifest (these override feed presets)
        for preset in self.manifest.get('presets', []):
            preset_id = preset.get('id')
            if preset_id:
                presets[preset_id] = {
                    'name': preset.get('name', ''),
                    'url': preset.get('url', ''),
                    'sha256': preset.get('sha256', ''),
                    'distro': preset.get('distro', ''),
                    **preset.get('metadata', {})
                }
        return presets
    
    def _fetch_preset_feed(self, feed_url: str) -> Dict[str, Any]:
        """Fetch and parse a JSON Feed 1.1 preset feed.
        
        Args:
            feed_url: HTTPS URL of the feed
            
        Returns:
            Dictionary of presets {id: {name, url, sha256, distro, ...}}
        """
        import json
        try:
            import requests
        except ImportError:
            logger.warning("requests library not available, skipping feed fetch")
            return {}
        
        try:
            response = requests.get(feed_url, timeout=10)
            response.raise_for_status()
            feed = response.json()
            
            # Validate JSON Feed structure
            if feed.get('version') != 'https://jsonfeed.org/version/1.1':
                logger.warning(f"Unknown feed version: {feed.get('version')}")
            
            presets = {}
            for item in feed.get('items', []):
                # Extract preset ID from item.id (format: "preset:ubuntu-24.04")
                item_id = item.get('id', '')
                if not item_id.startswith('preset:'):
                    continue
                preset_id = item_id.replace('preset:', '', 1)
                
                # Find download URLs from attachments
                url = ''
                sha256 = ''
                for attachment in item.get('attachments', []):
                    mime = attachment.get('mime_type', '')
                    title = attachment.get('title', '')
                    
                    # Prefer torrent, fallback to direct ISO
                    if 'torrent' in mime or 'torrent' in title.lower():
                        url = attachment.get('url', '')
                    elif not url and ('iso' in title.lower() or 'octet-stream' in mime):
                        url = attachment.get('url', '')
                    
                    # Extract checksum if provided
                    if 'sha256' in attachment:
                        sha256 = attachment['sha256']
                
                if not url:
                    logger.warning(f"No download URL found for preset {preset_id}")
                    continue
                
                # Extract distro from tags
                distro = ''
                tags = item.get('tags', [])
                distro_tags = ['ubuntu', 'debian', 'proxmox', 'fedora', 'arch']
                for tag in tags:
                    if tag.lower() in distro_tags:
                        distro = tag.lower()
                        break
                
                presets[preset_id] = {
                    'name': item.get('title', preset_id),
                    'url': url,
                    'sha256': sha256,
                    'distro': distro,
                    'description': item.get('summary', ''),
                }
            
            return presets
            
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to fetch feed: {e}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in feed: {e}")

    def get_config_fields(self) -> List[Dict[str, Any]]:
        """Return config fields from manifest."""
        return self.manifest.get('config_fields', [])

    def should_show_ui(self, preset_id: str, preset_data: Dict[str, Any]) -> bool:
        """Determine if plugin UI should be shown based on manifest rules."""
        visibility = self.manifest.get('ui_visibility', {}).get('when', {})
        
        # Check distro filter
        allowed_distros = visibility.get('preset_distro', [])
        if allowed_distros:
            distro = preset_data.get('distro', '').lower()
            if distro not in [d.lower() for d in allowed_distros]:
                return False
        
        # Check preset ID filter
        allowed_presets = visibility.get('preset_id', [])
        if allowed_presets:
            if preset_id not in allowed_presets:
                return False
        
        return True

    def _evaluate_condition(self, condition: Dict[str, Any], ui_values: Dict[str, Any]) -> bool:
        """Evaluate a conditional expression.
        
        Args:
            condition: Condition dict with 'field', 'operator', and optional 'value'
            ui_values: User-provided config values
            
        Returns:
            True if condition matches, False otherwise
        """
        field_id = condition.get('field')
        operator = condition.get('operator')
        expected = condition.get('value')
        
        if not field_id or not operator:
            return True  # Invalid condition defaults to true
        
        actual = ui_values.get(field_id)
        
        if operator == 'not_empty':
            return bool(actual and str(actual).strip())
        elif operator == 'empty':
            return not actual or not str(actual).strip()
        elif operator == 'equals':
            return actual == expected
        elif operator == 'not_equals':
            return actual != expected
        elif operator == 'in':
            return actual in (expected if isinstance(expected, list) else [expected])
        elif operator == 'not_in':
            return actual not in (expected if isinstance(expected, list) else [expected])
        
        return True

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
        context = {
            **ui_values,
            **self._action_vars,
            'iso_path': iso_path,
            'device_path': device_path,
            'preset_id': preset.get('id', ''),
            'preset_name': preset.get('name', ''),
        }

        # Emit event if specified
        emit_spec = action.get('emit_event')
        if emit_spec:
            self.emit_event(
                message=self.template_engine.render(emit_spec.get('message', ''), context),
                progress=emit_spec.get('progress'),
                event_type=PluginEventType.UPDATE,
            )

        result = None
        
        if action_type == 'render_template':
            template_name = action.get('template')
            if not template_name:
                logger.error(f"Action {action_id}: missing template name")
                return None
            
            template_str = self.manifest.get('templates', {}).get(template_name)
            if not template_str:
                logger.error(f"Action {action_id}: template '{template_name}' not found")
                return None
            
            result = self.template_engine.render(template_str, context)
            logger.debug(f"Rendered template {template_name}")

        elif action_type == 'write_file':
            content_src = action.get('content', '')
            path_template = action.get('path', '')
            permissions = action.get('permissions', '644')
            
            # Render content (either direct string or variable reference)
            if content_src.startswith('{{') and content_src.endswith('}}'):
                var_name = content_src[2:-2].strip()
                content = context.get(var_name, '')
            else:
                content = self.template_engine.render(content_src, context)
            
            # Render path
            file_path = self.template_engine.render(path_template, context)
            
            # Write file
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            # Set permissions
            os.chmod(file_path, int(permissions, 8))
            result = file_path
            logger.info(f"Wrote file: {file_path}")

        elif action_type == 'run_command':
            cmd_template = action.get('command', [])
            use_sudo = action.get('sudo', False)
            
            # Render command arguments
            cmd = [self.template_engine.render(arg, context) for arg in cmd_template]
            
            # Validate command is allowed (user-approved plugin-specific)
            cmd_name = cmd[0] if cmd else ''
            if cmd_name not in self._plugin_allowed_commands:
                # Build helpful error message
                if self._plugin_allowed_commands:
                    approved_list = ', '.join(sorted(self._plugin_allowed_commands))
                    error_msg = (
                        f"Command '{cmd_name}' is not approved for this plugin. "
                        f"Approved commands: {approved_list}"
                    )
                else:
                    error_msg = (
                        f"Command '{cmd_name}' is not allowed. "
                        f"Reinstall the plugin to approve commands."
                    )
                raise RuntimeError(error_msg)
            
            # Check if command exists
            if not shutil.which(cmd_name):
                raise RuntimeError(f"Command '{cmd_name}' not found in PATH")
            
            # Add sudo if requested
            # Note: Uses -n (non-interactive) flag for headless operation during autoinstall.
            # This requires NOPASSWD sudo configuration, which is acceptable because:
            # 1. Commands are pre-approved by user at plugin install time
            # 2. The command whitelist + approval system provides security
            if use_sudo:
                if not shutil.which('sudo'):
                    raise RuntimeError("sudo is required but not found in PATH")
                cmd = ['sudo', '-n'] + cmd
            
            # Execute
            logger.info(f"Running{' (with sudo)' if use_sudo else ''} command")
            try:
                proc_result = subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as e:
                # Provide helpful error message for sudo failures
                if use_sudo and 'sudo: a password is required' in e.stderr:
                    raise RuntimeError(
                        f"Command requires passwordless sudo. "
                        f"Configure NOPASSWD for user in /etc/sudoers or run SparkGTK with sudo."
                    ) from e
                raise RuntimeError(f"Command failed: {e.stderr}") from e
            
            # For commands with --output flag, use that as result
            # This handles tools like proxmox-auto-install-assistant
            if '--output' in cmd:
                output_idx = cmd.index('--output')
                if output_idx + 1 < len(cmd):
                    result = cmd[output_idx + 1]
                else:
                    result = proc_result.stdout.strip()
            else:
                result = proc_result.stdout.strip()
            
            if proc_result.stderr:
                logger.debug(f"Command stderr: {proc_result.stderr}")

        elif action_type == 'compute_file_hash':
            path_template = action.get('path')
            if not path_template:
                raise RuntimeError(f"Action {action_id}: 'path' is required for compute_file_hash")

            rendered_path = self.template_engine.render(path_template, context)
            algorithm = str(action.get('algorithm', 'sha256'))
            hash_value = self._hash_file(Path(rendered_path).expanduser(), algorithm)

            output_var = action.get('output_var')
            if output_var:
                self._action_vars[output_var] = hash_value
            result = hash_value
            logger.info(f"Computed {algorithm} hash for {rendered_path}")

        elif action_type == 'create_partition':
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

        elif action_type == 'write_partition_files':
            if not device_path:
                raise RuntimeError("write_partition_files requires a USB device path")

            label_template = action.get('partition_label')
            if not label_template:
                raise RuntimeError(f"Action {action_id}: 'partition_label' is required")

            partition_label = self.template_engine.render(label_template, context)
            files: Dict[str, str] = {}

            files_var = action.get('files_var')
            if files_var:
                bundle = self._action_vars.get(files_var)
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

        elif action_type == 'generate_receipt':
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
                self._action_vars[json_var] = canonical_json

            signature_var = action.get('signature_output_var')
            if signature_var:
                self._action_vars[signature_var] = signature

            hash_var = action.get('hash_output_var')
            if hash_var:
                self._action_vars[hash_var] = receipt_hash

            files_var_name = action.get('files_output_var') or signing_spec.get('files_output_var')
            if files_var_name:
                self._action_vars[files_var_name] = files_bundle

            public_key_var = action.get('public_key_output_var') or signing_spec.get('public_key_output_var')
            if public_key_var:
                self._action_vars[public_key_var] = encoded_public_key

            result = canonical_json
            logger.info(f"Generated receipt payload ({len(canonical_json)} bytes)")
        
        elif action_type == 'format_yaml_list':
            # Convert newline or space-separated list to YAML list format
            input_str = self.template_engine.render(action.get('input', ''), context)
            default_str = action.get('default', '')
            indent = action.get('indent', 0)
            
            # Use default if input is empty
            if not input_str.strip() and default_str:
                input_str = default_str
            
            # Parse items (newline or space-separated)
            if '\n' in input_str:
                items = [line.strip() for line in input_str.split('\n') if line.strip()]
            else:
                items = [item.strip() for item in input_str.split() if item.strip()]
            
            # Format as YAML list
            yaml_lines = [f"{' ' * indent}- {item}" for item in items]
            result = '\n'.join(yaml_lines)
            logger.info(f"Formatted {len(items)} items as YAML list")
        
        elif action_type == 'store_ephemeral_secret':
            # Store secret in memory (cleared on app exit)
            key = action.get('key')
            value = self.template_engine.render(action.get('value', ''), context)
            if key:
                self._ephemeral_secrets[key] = value
                logger.info(f"Stored ephemeral secret")
            result = None
        
        elif action_type == 'show_ephemeral_secret_button':
            # This is handled by the UI layer after write completes
            # Just validate the action has required fields
            key = action.get('key')
            if key not in self._ephemeral_secrets:
                logger.warning(f"Ephemeral secret not found")
            result = None

        else:
            logger.warning(f"Unknown action type: {action_type}")
            return None

        # Store result in action variables if requested
        output_var = action.get('output_var')
        if output_var and result is not None:
            self._action_vars[output_var] = result

        return result

    def on_iso_ready(self, iso_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]) -> str:
        """Execute on_iso_ready actions from manifest."""
        if not self.is_available:
            raise RuntimeError(self.unavailable_reason or "Plugin unavailable")

        actions = self.manifest.get('actions', {}).get('on_iso_ready', [])
        if not actions:
            return iso_path

        self.emit_event(
            event_type=PluginEventType.START,
            message=f"Processing ISO with {self.name}",
        )

        # Reset action variables
        self._action_vars = {'original_iso_path': iso_path}
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

    def on_write_complete(
        self, device_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]
    ) -> None:
        """Execute on_write_complete actions from manifest."""
        actions = self.manifest.get('actions', {}).get('on_write_complete', [])
        if not actions:
            return

        self.emit_event(
            event_type=PluginEventType.START,
            message=f"Post-write processing with {self.name}",
        )

        # Reset action variables
        self._action_vars = {}

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
    
    def get_ephemeral_secrets(self) -> Dict[str, str]:
        """Return all stored ephemeral secrets.
        
        Returns:
            Dictionary of secret keys and values stored in memory
        """
        return self._ephemeral_secrets.copy()
    
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
