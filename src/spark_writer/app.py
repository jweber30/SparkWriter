import sys
import os
import shutil
import gi

# Prefer Wayland, fall back to X11
if 'GDK_BACKEND' not in os.environ:
    os.environ['GDK_BACKEND'] = 'wayland,x11'

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk

from .window import SparkWindow
from .plugins.trust import evaluate_trust

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
    
    def _download_and_install_plugin(self, url: str):
        """Download and install plugin after trust confirmation."""
        import json
        import urllib.request
        import tempfile
        from pathlib import Path
        try:
            # Step 3: Download manifest
            print(f"Downloading manifest...")
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.json') as tmp:
                tmp_path = tmp.name
                with urllib.request.urlopen(url, timeout=30) as response:
                    content = response.read()
                    tmp.write(content)
            
            # Step 4: Parse and validate manifest
            with open(tmp_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)

            legacy_secure_keys = [key for key in ("secure_manifest", "signature") if key in manifest]
            if legacy_secure_keys:
                raise ValueError(
                    "Unsupported manifest fields: "
                    + ", ".join(sorted(legacy_secure_keys))
                    + ". Secure manifest keys are deprecated; publish a plain manifest and reinstall it."
                )
            
            plugin_id = manifest.get('metadata', {}).get('id')
            plugin_name = manifest.get('metadata', {}).get('name', 'Unknown')
            plugin_desc = manifest.get('metadata', {}).get('description', '')
            
            if not plugin_id:
                raise ValueError("Manifest missing metadata.id field")
            
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
            
            # Step 6: Show command approval dialog if plugin-specific commands exist
            if plugin_specific:
                cmd_details = "\n".join(
                    f"  • {cmd}" + (f": {desc}" if desc else "")
                    for cmd, desc in plugin_specific
                )
                
                approval_msg = (
                    f"Plugin: {plugin_name}\n"
                    + (f"{plugin_desc}\n\n" if plugin_desc else "\n")
                    + f"This plugin requires permission to execute:\n\n"
                    + cmd_details
                    + "\n\nThese commands will only be executed when this plugin runs.\n"
                    + "Do you want to install this plugin?"
                )
                
                def on_approval_response(dialog, response):
                    dialog.destroy()
                    if response == Gtk.ResponseType.YES:
                        # User approved - install plugin
                        self._finalize_plugin_install(tmp_path, plugin_id, plugin_name, manifest)
                    else:
                        # User rejected - clean up
                        os.unlink(tmp_path)
                        print(f"Plugin installation cancelled by user")
                
                self._show_confirmation_dialog(
                    "Command Approval Required",
                    approval_msg,
                    on_approval_response
                )
            else:
                # No plugin-specific commands - install directly
                self._finalize_plugin_install(tmp_path, plugin_id, plugin_name, manifest)
                
        except Exception as e:
            print(f"Failed to add JSON plugin: {e}")
            import traceback
            traceback.print_exc()
            self._show_error_dialog("Installation Failed", str(e))
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def _finalize_plugin_install(self, tmp_path: str, plugin_id: str, plugin_name: str, manifest: dict):
        """Complete plugin installation after all approvals."""
        import json
        try:
            # Install to plugins directory
            data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
            dst_dir = os.path.join(data_home, "spark-writer", "plugins")
            os.makedirs(dst_dir, exist_ok=True)
            
            dst_file = os.path.join(dst_dir, f"{plugin_id}.json")
            
            # Create approval metadata file with approved commands
            approved_commands = []
            for cmd in manifest.get('requires', {}).get('commands', []):
                cmd_name = cmd.get('name')
                if not cmd_name:
                    continue
                if cmd.get('allow_plugin_specific', True):
                    approved_commands.append(cmd_name)
            
            approval_data = {
                'plugin_id': plugin_id,
                'plugin_name': plugin_name,
                'approved_commands': approved_commands,
                'approved_at': str(__import__('datetime').datetime.now())
            }
            
            approval_file = os.path.join(dst_dir, f".{plugin_id}.approval")
            with open(approval_file, 'w') as f:
                json.dump(approval_data, f, indent=2)
            
            # Move manifest to final location
            shutil.move(tmp_path, dst_file)
            print(f"JSON plugin installed to {dst_file}")
            print(f"Command approvals saved to {approval_file}")
            
            # Reload plugins
            win = self.props.active_window
            if win:
                GLib.idle_add(win.reload_plugins, f"Installed {plugin_name}")
                
        except Exception as e:
            print(f"Failed to finalize plugin installation: {e}")
            import traceback
            traceback.print_exc()
            self._show_error_dialog("Installation Failed", str(e))
    
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
        """Show an error dialog to the user."""
        print(f"ERROR - {title}: {message}")
        
        def show():
            win = self.props.active_window
            # If no window is active yet, we can't easily show a modal dialog attached to it.
            # But we can try to get the active window or just show a detached one (less ideal).
            
            dialog = Gtk.MessageDialog(
                transient_for=win,
                modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=title,
            )
            dialog.props.secondary_text = message
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.present()
            
        GLib.idle_add(show)


def main():
    app = SparkApplication()
    return app.run(sys.argv)

if __name__ == '__main__':
    sys.exit(main())
