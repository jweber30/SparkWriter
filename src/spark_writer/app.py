import sys
import os
import json
import shutil
import tempfile
import gi

# Prefer Wayland, fall back to X11
if 'GDK_BACKEND' not in os.environ:
    os.environ['GDK_BACKEND'] = 'wayland,x11'

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk, Pango

from .window import SparkWindow
from .plugins.signing import (
    build_manifest_download_request,
    extract_github_username_from_url,
    is_github_manifest_url,
    normalize_github_manifest_url,
    verify_github_signed_manifest,
)
from .plugins.manifest_assets import discover_template_sidecars, resolve_sidecar_url
from .plugins.manifest_download import parse_downloaded_manifest
from .plugins.manifest_schema import validate_manifest_schema
from .plugins.trust import evaluate_trust
from .plugins.plugin_install import create_plugin_stage_dir, record_install_origin

class SparkApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id="net.metalstrapper.SparkGTK",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)

    def do_activate(self):
        display = Gdk.Display.get_default()
        if display:
            print(f"Running on GDK Backend: {display.__class__.__name__}")
            
        win = self.props.active_window
        if not win:
            win = SparkWindow(self)
        win.present()
        
        if hasattr(self, '_pending_uri'):
            win.handle_uri(self._pending_uri)
            del self._pending_uri

    def do_command_line(self, command_line):
        args = command_line.get_arguments()
        if len(args) > 1:
            uri = args[1]
            if uri.startswith("spark://"):
                self.handle_uri(uri)
        
        self.activate()
        return 0

    def handle_uri(self, uri):
        print(f"Handling URI: {uri}")
        
        # Check for JSON plugin addition (new handler)
        if "plugin/add" in uri:
            import urllib.parse
            parsed = urllib.parse.urlparse(uri)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                manifest_url = query.get('manifest', [None])[0] or query.get('url', [None])[0]
                if manifest_url:
                    self.add_json_plugin(manifest_url)
            except Exception as e:
                print(f"Failed to add JSON plugin: {e}")
            return

        # Delegate others to Window/Plugins
        win = self.props.active_window
        if win:
            win.handle_uri(uri)
        else:
            self._pending_uri = uri



    def add_json_plugin(self, url: str):
        """Add a JSON-based SparkPlug from a manifest URL.
        
        This implements trust evaluation and user confirmation before
        downloading and installing JSON plugin manifests.
        """
        import json
        import urllib.request
        import urllib.parse
        import tempfile
        from pathlib import Path
        print(f"Adding JSON plugin from: {url}")
        
        # Step 1: Evaluate trust
        allow_insecure = False
        allowed, prompt_message = evaluate_trust(url, allow_insecure)
        
        if not allowed:
            self._show_error_dialog("Blocked", prompt_message or "Source not trusted")
            return
        
        # Step 2: Show trust confirmation if needed
        if prompt_message:
            def on_trust_response(dialog, response):
                dialog.destroy()
                if response == Gtk.ResponseType.YES:
                    # User approved - continue with download
                    GLib.idle_add(self._download_and_install_plugin, url)
            
            self._show_confirmation_dialog(
                "Install Plugin?",
                prompt_message,
                on_trust_response
            )
            return  # Async - will continue in callback
        
        # Auto-trusted source - proceed directly
        self._download_and_install_plugin(url)
    
    def _download_and_install_plugin(self, url: str, github_token: str | None = None):
        """Download and install plugin after trust confirmation."""
        import urllib.error
        import urllib.request
        tmp_path = None
        try:
            normalized_url = normalize_github_manifest_url(url)

            # Step 3: Download manifest
            print(f"Downloading manifest...")
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.json') as tmp:
                tmp_path = tmp.name
                request = build_manifest_download_request(normalized_url, github_token)
                with urllib.request.urlopen(request, timeout=30) as response:
                    content = response.read()
                    manifest = parse_downloaded_manifest(
                        content,
                        content_type=response.headers.get("Content-Type"),
                        final_url=response.geturl(),
                    )
                    tmp.write(content)

            # Step 4: Validate manifest
            # TODO(next sprint): Validate manifest config_fields.pattern regex strings
            # during install-time checks. For now, publishers must self-test patterns.

            # High-security online mode for GitHub-hosted manifests:
            # require URL-owner cross-check and valid SSH signature.
            if is_github_manifest_url(normalized_url):
                verified, reason = verify_github_signed_manifest(manifest, normalized_url)
                if not verified:
                    raise ValueError(
                        "GitHub manifest authorization failed: "
                        + reason
                    )

            legacy_secure_keys = [key for key in ("secure_manifest", "signature") if key in manifest]
            if legacy_secure_keys:
                raise ValueError(
                    "Unsupported manifest fields: "
                    + ", ".join(sorted(legacy_secure_keys))
                    + ". Secure manifest keys are deprecated; publish a plain manifest and reinstall it."
                )

            try:
                validate_manifest_schema(manifest)
            except ValueError as exc:
                raise ValueError(
                    "Manifest schema validation failed: "
                    + str(exc)
                ) from exc
            
            plugin_id = manifest.get('metadata', {}).get('id')
            plugin_name = manifest.get('metadata', {}).get('name', 'Unknown')
            plugin_desc = manifest.get('metadata', {}).get('description', '')
            
            if not plugin_id:
                raise ValueError("Manifest missing metadata.id field")

            record_install_origin(manifest, normalized_url)
            
            # Step 5: Check required commands
            required_cmds = manifest.get('requires', {}).get('commands', [])
            missing = []
            plugin_specific = []
            
            for cmd_spec in required_cmds:
                cmd_name = cmd_spec.get('name')
                if not cmd_name:
                    continue
                
                # Check if command exists
                if not shutil.which(cmd_name):
                    install_hint = cmd_spec.get('install_hint', '')
                    missing.append(f"{cmd_name}" + (f" ({install_hint})" if install_hint else ""))
                    continue
                
                # Categorize command
                if cmd_spec.get('allow_plugin_specific', True):
                    plugin_specific.append((cmd_name, cmd_spec.get('description', '')))
                else:
                    # Command not in whitelist and not marked as plugin-specific
                    self._show_error_dialog(
                        "Invalid Plugin Manifest",
                        f"Plugin attempts to use command '{cmd_name}' which is not allowed.\n\n"
                        f"The manifest explicitly disallows this command. Either add it to the "
                        f"global whitelist or remove 'allow_plugin_specific: false'."
                    )
                    os.unlink(tmp_path)
                    return
            
            if missing:
                msg = (
                    f"Plugin '{plugin_name}' requires the following commands:\n\n"
                    + "\n".join(f"  • {cmd}" for cmd in missing)
                    + "\n\nInstall these tools and try again."
                )
                self._show_error_dialog("Missing Dependencies", msg)
                os.unlink(tmp_path)
                return
            
            # Step 6: Show command disclosure dialog if plugin-specific commands exist
            if plugin_specific:
                cmd_details = "\n".join(
                    f"  • {cmd}" + (f": {desc}" if desc else "")
                    for cmd, desc in plugin_specific
                )
                
                approval_msg = (
                    f"Plugin: {plugin_name}\n"
                    + (f"{plugin_desc}\n\n" if plugin_desc else "\n")
                    + f"This plugin may execute the following commands at runtime:\n\n"
                    + cmd_details
                    + "\n\nYou will be prompted to approve commands at invocation time.\n"
                    + "Install this plugin now?"
                )
                
                def on_approval_response(dialog, response):
                    dialog.destroy()
                    if response == Gtk.ResponseType.YES:
                        # User approved - install plugin
                        self._finalize_plugin_install(
                            tmp_path,
                            plugin_id,
                            plugin_name,
                            manifest,
                            normalized_url,
                            github_token,
                        )
                    else:
                        # User rejected - clean up
                        os.unlink(tmp_path)
                        print(f"Plugin installation cancelled by user")
                
                self._show_confirmation_dialog(
                    "Command Disclosure",
                    approval_msg,
                    on_approval_response
                )
            else:
                # No plugin-specific commands - install directly
                self._finalize_plugin_install(
                    tmp_path,
                    plugin_id,
                    plugin_name,
                    manifest,
                    normalized_url,
                    github_token,
                )

        except urllib.error.HTTPError as e:
            needs_auth = (
                e.code in (401, 403)
                and is_github_manifest_url(url)
                and not github_token
            )
            if needs_auth:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                self._show_github_auth_dialog(url)
                return

            print(f"Failed to add JSON plugin: {e}")
            self._show_error_dialog("Installation Failed", f"HTTP error: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
                
        except Exception as e:
            print(f"Failed to add JSON plugin: {e}")
            import traceback
            traceback.print_exc()
            self._show_error_dialog("Installation Failed", str(e))
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _show_github_auth_dialog(self, url: str):
        """Prompt the user for a one-time GitHub token and retry download."""
        win = self.props.active_window
        normalized_url = normalize_github_manifest_url(url)
        expected_user = extract_github_username_from_url(normalized_url) or "unknown"

        dialog = Gtk.Dialog(
            transient_for=win,
            modal=True,
            title="GitHub Authentication Required",
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Continue", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label(
            label=(
                "This manifest appears to require GitHub authentication.\n\n"
                f"Expected GitHub owner: {expected_user}\n"
                "Provide a one-time GitHub token with read access to the repository."
            )
        )
        label.set_wrap(True)
        label.set_xalign(0)
        box.append(label)

        token_entry = Gtk.PasswordEntry()
        token_entry.set_placeholder_text("github_pat_... or ghp_...")
        box.append(token_entry)

        open_page = Gtk.Button(label="Open GitHub Fine-Grained Token Settings")

        def on_open_page(_button):
            Gio.AppInfo.launch_default_for_uri("https://github.com/settings/personal-access-tokens/new", None)

        open_page.connect("clicked", on_open_page)
        box.append(open_page)

        content.append(box)

        def on_response(dlg, response):
            dlg.destroy()
            if response != Gtk.ResponseType.OK:
                return

            token = token_entry.get_text().strip()
            if not token:
                self._show_error_dialog("Authentication Required", "No GitHub token provided.")
                return

            GLib.idle_add(self._download_and_install_plugin, url, token)

        dialog.connect("response", on_response)
        dialog.present()
    
    def _download_template_sidecars(
        self,
        *,
        manifest_url: str,
        manifest: dict,
        github_token: str | None,
    ) -> list[tuple[str, str]]:
        """Download manifest-referenced template sidecar files.

        Returns a list of (relative_path, temp_file_path) tuples.
        """
        import urllib.request

        sidecar_refs = discover_template_sidecars(manifest)
        downloaded: list[tuple[str, str]] = []

        for sidecar_ref in sidecar_refs:
            sidecar_url = resolve_sidecar_url(manifest_url, sidecar_ref)
            with tempfile.NamedTemporaryFile(mode='wb', delete=False) as tmp:
                request = build_manifest_download_request(sidecar_url, github_token)
                with urllib.request.urlopen(request, timeout=30) as response:
                    tmp.write(response.read())
                downloaded.append((sidecar_ref, tmp.name))

        return downloaded

    def _cleanup_paths(self, paths: list[str]) -> None:
        """Best-effort cleanup for temporary files and directories."""
        for path in paths:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass

    def _finalize_plugin_install(
        self,
        tmp_path: str,
        plugin_id: str,
        plugin_name: str,
        manifest: dict,
        manifest_url: str,
        github_token: str | None,
    ):
        """Complete plugin installation after disclosure and dependency checks."""
        downloaded_sidecars: list[tuple[str, str]] = []
        stage_dir = None
        try:
            downloaded_sidecars = self._download_template_sidecars(
                manifest_url=manifest_url,
                manifest=manifest,
                github_token=github_token,
            )

            # Install to plugins directory
            data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
            dst_dir = os.path.join(data_home, "spark-writer", "plugins")
            os.makedirs(dst_dir, exist_ok=True)

            # Keep staging on the destination filesystem so os.replace remains
            # atomic even when /tmp and XDG_DATA_HOME are separate mounts.
            stage_dir = create_plugin_stage_dir(dst_dir, plugin_id)
            stage_manifest = os.path.join(stage_dir, f"{plugin_id}.json")
            shutil.move(tmp_path, stage_manifest)
            with open(stage_manifest, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
                handle.write("\n")

            for rel_path, temp_file in downloaded_sidecars:
                target = os.path.join(stage_dir, rel_path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(temp_file, target)

            dst_file = os.path.join(dst_dir, f"{plugin_id}.json")

            # Replace manifest and copy sidecars from staging.
            os.replace(stage_manifest, dst_file)
            for rel_path, _temp_file in downloaded_sidecars:
                source = os.path.join(stage_dir, rel_path)
                destination = os.path.join(dst_dir, rel_path)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                os.replace(source, destination)

            if stage_dir:
                shutil.rmtree(stage_dir, ignore_errors=True)

            print(f"JSON plugin installed to {dst_file}")
            if downloaded_sidecars:
                print(f"Downloaded {len(downloaded_sidecars)} template sidecar file(s)")
            print("Runtime command approval will be requested on first invocation")
            
            # Reload plugins
            win = self.props.active_window
            if win:
                GLib.idle_add(
                    win.reload_plugins,
                    f"Installed {plugin_name}",
                    plugin_id,
                )
                
        except Exception as e:
            print(f"Failed to finalize plugin installation: {e}")
            import traceback
            traceback.print_exc()
            self._show_error_dialog("Installation Failed", str(e))
            cleanup_paths = [tmp_path]
            cleanup_paths.extend(temp_path for _rel, temp_path in downloaded_sidecars)
            if stage_dir:
                cleanup_paths.append(stage_dir)
            self._cleanup_paths(cleanup_paths)
    
    def _show_confirmation_dialog(self, title: str, message: str, callback):
        """Show a yes/no confirmation dialog."""
        def show():
            win = self.props.active_window
            
            dialog = Gtk.MessageDialog(
                transient_for=win,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text=title,
            )
            dialog.props.secondary_text = message
            dialog.connect("response", callback)
            dialog.present()
        
        GLib.idle_add(show)
    
    def _show_error_dialog(self, title: str, message: str):
        """Show an error dialog with selectable, copyable details."""
        print(f"ERROR - {title}: {message}")
        
        def show():
            win = self.props.active_window

            dialog = Gtk.Dialog(
                transient_for=win,
                modal=True,
                title=title,
            )
            dialog.set_default_size(520, -1)
            dialog.add_button("Copy Details", Gtk.ResponseType.APPLY)
            dialog.add_button("Close", Gtk.ResponseType.CLOSE)

            content = dialog.get_content_area()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(18)
            box.set_margin_bottom(18)
            box.set_margin_start(18)
            box.set_margin_end(18)

            heading = Gtk.Label(label=title)
            heading.add_css_class("title-2")
            heading.set_xalign(0)
            box.append(heading)

            details = Gtk.Label(label=message)
            details.set_selectable(True)
            details.set_wrap(True)
            details.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            details.set_xalign(0)
            details.set_max_width_chars(80)
            box.append(details)
            content.append(box)

            def on_response(current_dialog, response):
                if response == Gtk.ResponseType.APPLY:
                    display = Gdk.Display.get_default()
                    if display:
                        display.get_clipboard().set(message)
                    return
                current_dialog.destroy()

            dialog.connect("response", on_response)
            dialog.present()
            
        GLib.idle_add(show)


def main():
    from usb_writer_core.preflight import enforce_preflight
    enforce_preflight()
    app = SparkApplication()
    return app.run(sys.argv)

if __name__ == '__main__':
    sys.exit(main())
