import sys
import os
import logging
import threading
from typing import Any, Dict, List, Optional
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GObject, GLib

from .plugins.base import ConfigField
from .plugins.forms import ConfigFormBuilder
from .plugins.manager import PluginManager
from .core.downloader import Downloader

logger = logging.getLogger(__name__)

try:
    from usb_writer_core.notifications import (
        DesktopNotifier,
        Notification,
        NotificationLevel,
        PipelineNotifier,
        PipelineStage,
    )
    from usb_writer_core.writer import list_removable_drives, write_iso_to_device
except ImportError:
    print("Warning: usb_writer_core.notifications not found. Notifications disabled.")

    def list_removable_drives():
        return []

    def write_iso_to_device(*args, **kwargs):
        pass

    class DesktopNotifier:  # type: ignore
        def __init__(self, app_name):
            pass

        def send_notification(self, *args, **kwargs):
            pass

    class Notification:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

    class NotificationLevel:  # type: ignore
        INFO = "low"
        NORMAL = "normal"
        CRITICAL = "critical"

    class PipelineStage:  # type: ignore
        DOWNLOAD = "DOWNLOAD"
        PROCESS = "PROCESS"
        WRITE = "WRITE"
        VERIFY = "VERIFY"
        FINALIZE = "FINALIZE"

    class PipelineNotifier:  # type: ignore
        def __init__(self, *_, **__):
            pass

        def start(self, *_args, **_kwargs):
            pass

        def update_stage(self, *_args, **_kwargs):
            pass

        def complete_stage(self, *_args, **_kwargs):
            pass

        def success(self, *_args, **_kwargs):
            pass

        def failure(self, *_args, **_kwargs):
            pass

class SparkWindow(Adw.ApplicationWindow):
    __gsignals__ = {}

    def __init__(self, app: Adw.Application, **kwargs):
        super().__init__(application=app, title="Spark Writer", **kwargs)
        
        # Set window icon (Wayland/Crostini compatibility)
        self.set_icon_name("spark-writer")
        
        # 1. Setup the "Pure Adwaita" Layout
        self.set_default_size(900, 700)
        
        # Navigation View (Replaces Stack)
        self.nav_view = Adw.NavigationView()
        self.set_content(self.nav_view)
        
        # Managers
        self._plugin_manager = PluginManager()
        self._form_builder = ConfigFormBuilder(on_change_callback=self._update_flash_button_state)
        self.downloader = Downloader(os.path.expanduser("~/ISO-Downloads"))
        # Pipeline will be initialized per-flash based on required stages
        self.pipeline: Optional[PipelineNotifier] = None
        self.drives: List[Dict[str, Any]] = []
        self.all_presets: List[Dict[str, Any]] = []
        self._plugin_entries: List[Dict[str, Any]] = []
        self._flash_in_progress = False
        
        # Create Pages
        self.config_page = self._create_config_page()
        self.nav_view.add(self.config_page)
        
        self.progress_page = self._create_progress_page()
        # We don't add progress page yet, we push it later
        
        # Load Plugins
        self._plugin_manager.load_plugins("spark_writer.plugins.installed")
        
        # Load All Presets
        self._load_all_presets()
            
        # Load Drives
        self._refresh_drives()
    
    def reload_plugins(self, message: str = "Plugins reloaded"):
        """Reload all plugins and refresh the UI."""
        import importlib
        import sys
        
        # Clear plugin state
        self._plugin_manager.plugins.clear()
        self._plugin_manager.disabled_plugins.clear()
        
        # Reload plugin modules to pick up new files
        for module_name in list(sys.modules.keys()):
            if 'spark_writer.plugins' in module_name:
                try:
                    importlib.reload(sys.modules[module_name])
                except Exception as e:
                    logger.warning(f"Failed to reload {module_name}: {e}")
        
        # Reload plugins
        self._plugin_manager.load_plugins("spark_writer.plugins.installed")
        
        # Refresh presets
        self._load_all_presets()
        
        # Show notification
        if self.pipeline:
            self.pipeline.success(message)
        
        logger.info(message)
    
    def refresh_presets(self):
        """Refresh presets without full plugin reload (for feed additions)."""
        self._load_all_presets()

    def _load_all_presets(self):
        self.all_presets = []
        model = Gtk.StringList()
        
        # Iterate all enabled plugins to gather presets
        for plugin in self._plugin_manager.plugins:
            if not self._plugin_manager.is_plugin_enabled(plugin):
                continue
                
            presets = plugin.register_presets()
            for pid, pdata in presets.items():
                pdata['id'] = pid
                pdata['source_plugin'] = plugin
                self.all_presets.append(pdata)
                model.append(pdata.get('name', pid))
                
        self._preset_row.set_model(model)
        if self.all_presets:
            self._preset_row.set_selected(0)
            self._on_preset_changed(self._preset_row)
        else:
            logger.warning("No presets available. Enable a SparkPlug or install one via spark:// URI")
            self._preset_row.set_selected(Gtk.INVALID_LIST_POSITION)
            self._plugin_row.set_sensitive(False)
            self._plugin_row.set_subtitle("Add a SparkPlug to access presets.")
            self._form_builder.reset(self._pref_page)

        self._update_flash_button_state()

    def _create_config_page(self):
        page = Adw.NavigationPage(title="Spark Writer", tag="config")
        
        toolbar_view = Adw.ToolbarView()
        page.set_child(toolbar_view)
        
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)
        
        self._pref_page = Adw.PreferencesPage()
        toolbar_view.set_content(self._pref_page)
        
        # 2. Preset Section
        self._preset_group = Adw.PreferencesGroup(
            title="Operating System",
            description="Select the target OS preset."
        )
        self._pref_page.add(self._preset_group)
        
        # Preset Row (First)
        self._preset_row = Adw.ComboRow(title="Preset")
        self._preset_row.set_icon_name("computer-symbolic")
        self._preset_row.connect("notify::selected", self._on_preset_changed)
        self._preset_group.add(self._preset_row)

        # Plugin Row (Second)
        self._plugin_row = Adw.ComboRow(title="Plugin")
        self._plugin_row.set_icon_name("toy-brick-symbolic")
        self._plugin_row.connect("notify::selected", self._on_plugin_changed)
        self._preset_group.add(self._plugin_row)
        
        # 3. Target Device Section
        self._drive_group = Adw.PreferencesGroup(
            title="Target Device",
            description="Select the USB drive to flash."
        )
        self._pref_page.add(self._drive_group)

        self._drive_row = Adw.ComboRow(title="Drive")
        self._drive_row.set_icon_name("drive-removable-media-symbolic")
        self._drive_group.add(self._drive_row)
        
        # Refresh button as a separate action row so it stays enabled
        refresh_row = Adw.ActionRow(title="Refresh Drives")
        refresh_row.set_icon_name("view-refresh-symbolic")
        refresh_row.set_activatable(True)
        refresh_row.connect("activated", self._refresh_drives)
        self._drive_group.add(refresh_row)
        
        # Action Bar
        self._action_bar = Gtk.ActionBar()
        toolbar_view.add_bottom_bar(self._action_bar)
        
        self._flash_btn = Gtk.Button(label="Flash Drive")
        self._flash_btn.add_css_class("suggested-action")
        self._flash_btn.add_css_class("pill")
        self._flash_btn.connect("clicked", self._on_flash_clicked)
        self._action_bar.pack_end(self._flash_btn)
        
        self._save_iso_btn = Gtk.Button(label="Save ISO")
        self._save_iso_btn.add_css_class("pill")
        self._save_iso_btn.connect("clicked", self._on_save_iso_clicked)
        self._action_bar.pack_end(self._save_iso_btn)
        
        return page

    def _create_progress_page(self):
        page = Adw.NavigationPage(title="Flashing...", tag="progress")
        
        toolbar_view = Adw.ToolbarView()
        page.set_child(toolbar_view)
        
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)
        
        self._status_page = Adw.StatusPage(
            title="Flashing...",
            description="Please wait while the operation completes.",
            icon_name="system-run-symbolic"
        )
        
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        progress_box.set_halign(Gtk.Align.CENTER)
        progress_box.set_valign(Gtk.Align.CENTER)
        progress_box.set_margin_top(24)
        
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_size_request(300, -1)
        progress_box.append(self.progress_bar)
        
        self.status_label = Gtk.Label(label="Initializing...")
        progress_box.append(self.status_label)
        
        self._status_page.set_child(progress_box)
        toolbar_view.set_content(self._status_page)
        
        return page

    def _refresh_drives(self, *args):
        drives = list_removable_drives()
        self.drives = drives  # Store for later use
        
        model = Gtk.StringList()
        if not drives:
            model.append("No drives found")
            self._drive_row.set_sensitive(False)
            self._drive_row.set_selected(Gtk.INVALID_LIST_POSITION)
        else:
            self._drive_row.set_sensitive(True)
            for drive in drives:
                # Format: "SanDisk Ultra (32GB) - /dev/sda"
                label = f"{drive.get('model', 'Unknown')} ({drive.get('size', '?')}) - {drive.get('path')}"
                model.append(label)
            self._drive_row.set_selected(0)
        
        self._drive_row.set_model(model)
        self._update_flash_button_state()

    def _on_preset_changed(self, *args):
        idx = self._preset_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_presets):
            self.current_preset = None
            self._plugin_row.set_model(Gtk.StringList())
            self._plugin_row.set_sensitive(False)
            self._plugin_row.set_subtitle("Select a preset to configure plugins.")
            self._form_builder.reset(self._pref_page)
            self._update_flash_button_state()
            return

        preset = self.all_presets[idx]
        self.current_preset = preset

        self._plugin_entries = []
        plugin_model = Gtk.StringList()

        for plugin in self._plugin_manager.plugins:
            if not self._plugin_manager.is_plugin_enabled(plugin):
                continue

            if plugin.should_show_ui(preset['id'], preset):
                plugin_model.append(plugin.name)
                self._plugin_entries.append(
                    {
                        "plugin": plugin,
                        "available": bool(getattr(plugin, "is_available", True)),
                        "reason": getattr(plugin, "unavailable_reason", None),
                    }
                )

        self._plugin_row.set_model(plugin_model)

        if not self._plugin_entries:
            self._plugin_row.set_sensitive(False)
            warning_msg = f"Preset {preset['name']} has no compatible plugins"
            logger.warning(warning_msg)
            self._plugin_row.set_subtitle("No compatible plugins for this preset.")
            self._plugin_row.set_selected(Gtk.INVALID_LIST_POSITION)
            self.current_plugin = None
            self._form_builder.reset(self._pref_page)
            return

        has_available = any(entry["available"] for entry in self._plugin_entries)
        self._plugin_row.set_sensitive(has_available)

        if not has_available:
            reason = self._plugin_entries[0]["reason"] or "Plugin unavailable. Install required tooling to enable it."
            self._plugin_row.set_subtitle(reason or "Plugin unavailable")
            self._plugin_row.set_selected(0)
            self.current_plugin = None
            self._form_builder.reset(self._pref_page)
            self._show_plugin_notice(reason)
            return

        self._plugin_row.set_subtitle("")
        selected_index = 0
        for idx_entry, entry in enumerate(self._plugin_entries):
            if entry["available"]:
                selected_index = idx_entry
                break
        self._plugin_row.set_selected(selected_index)
        self._on_plugin_changed(self._plugin_row)

    def _on_plugin_changed(self, *args):
        idx = self._plugin_row.get_selected()
        if (
            idx == Gtk.INVALID_LIST_POSITION
            or idx >= len(self._plugin_entries)
            or not self._plugin_entries
        ):
            self.current_plugin = None
            self._form_builder.reset(self._pref_page)
            return

        entry = self._plugin_entries[idx]
        if not entry["available"]:
            reason = entry["reason"] or "Plugin unavailable."
            self.current_plugin = None
            self._form_builder.reset(self._pref_page)
            self._plugin_row.set_subtitle(reason or "Plugin unavailable")
            self._show_plugin_notice(reason)
            return

        plugin = entry["plugin"]
        self.current_plugin = plugin
        self._plugin_row.set_subtitle("")

        # Update Config UI
        self._form_builder.reset(self._pref_page)
        self._form_builder.add_fields(plugin.get_config_schema(), self._pref_page)

    def _on_flash_clicked(self, btn):
        idx = self._preset_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_presets):
            return

        preset = self.all_presets[idx]
        self.current_preset = preset

        drive_idx = self._drive_row.get_selected()
        if drive_idx == Gtk.INVALID_LIST_POSITION or not self.drives:
            logger.warning("Flash requested without selecting a drive")
            return

        target_drive = self.drives[drive_idx]
        self.ui_values = self._form_builder.get_values()

        self._flash_in_progress = True
        self._update_flash_button_state()

        # Calculate which stages this flash will need
        stages = [PipelineStage.DOWNLOAD]
        if self.current_plugin and self.current_plugin.requires_processing():
            stages.append(PipelineStage.PROCESS)
        stages.append(PipelineStage.WRITE)
        # VERIFY and FINALIZE not yet implemented, omit for now
        
        # Initialize pipeline with required stages
        self.pipeline = PipelineNotifier(app_name="SparkGTK", stages=stages)

        # Switch to progress page
        if self.nav_view.get_visible_page() != self.progress_page:
            self.nav_view.push(self.progress_page)
        self.progress_page.set_title(f"Flashing {preset['name']}")
        self._status_page.set_title(f"Flashing {preset['name']}")
        self.progress_bar.set_fraction(0.1)
        self.pipeline.start(f"Provisioning {preset['name']}")
        self._active_stage = PipelineStage.DOWNLOAD
        self.pipeline.update_stage(PipelineStage.DOWNLOAD, "Starting download", 0)


        # Start download/flash process
        if self.current_plugin:
            logger.info("Plugin %s active", self.current_plugin.name)
            
        # Trigger download
        self.downloader.start_download(
            preset['url'], 
            preset['name'], 
            self._on_progress, 
            self._on_download_complete, 
            self._on_error
        )

    def _on_save_iso_clicked(self, btn):
        """Handle Save ISO button click - first choose save location, then download."""
        idx = self._preset_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_presets):
            return

        preset = self.all_presets[idx]
        self.current_preset = preset
        self.ui_values = self._form_builder.get_values()

        # Show file picker FIRST to choose destination
        self._prompt_iso_save_location(preset)

    def _prompt_iso_save_location(self, preset):
        """Prompt user to choose where to save the ISO file before downloading."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Save ISO File")
        
        # Set suggested filename based on preset name
        suggested_name = f"{preset['name']}.iso"
        dialog.set_initial_name(suggested_name)
        
        # Set initial folder to Downloads or home
        downloads_path = Path.home() / "Downloads"
        if downloads_path.exists():
            downloads_file = Gio.File.new_for_path(str(downloads_path))
            dialog.set_initial_folder(downloads_file)
        
        dialog.save(self, None, self._on_iso_save_location_chosen, preset)

    def _on_iso_save_location_chosen(self, dialog, result, preset):
        """Handle save location selection - start download if location chosen."""
        try:
            file = dialog.save_finish(result)
            if not file:
                logger.info("Save cancelled by user")
                return
                
            dest_path = file.get_path()
            logger.info(f"User chose save location: {dest_path}")
            
            # Store the destination path for later
            self._iso_save_dest_path = dest_path
            
            # Now start the download/processing workflow
            self._start_iso_save_workflow(preset)
            
        except GLib.Error as e:
            # User cancelled or error occurred
            if e.code != 2:  # 2 = dismissed/cancelled
                logger.error(f"Error in file dialog: {e}")
                self._on_error(f"Failed to select save location: {e.message}")
            else:
                logger.info("Save cancelled by user")

    def _start_iso_save_workflow(self, preset):
        """Start the download and processing workflow after save location is chosen."""
        self._flash_in_progress = True
        self._update_flash_button_state()

        # Calculate stages for ISO save (no WRITE stage)
        stages = [PipelineStage.DOWNLOAD]
        if self.current_plugin and self.current_plugin.requires_processing():
            stages.append(PipelineStage.PROCESS)
        
        # Initialize pipeline
        self.pipeline = PipelineNotifier(app_name="SparkGTK", stages=stages)

        # Switch to progress page
        if self.nav_view.get_visible_page() != self.progress_page:
            self.nav_view.push(self.progress_page)
        self.progress_page.set_title(f"Saving {preset['name']}")
        self._status_page.set_title(f"Saving {preset['name']}")
        self.progress_bar.set_fraction(0.1)
        self.pipeline.start(f"Downloading {preset['name']}")
        self._active_stage = PipelineStage.DOWNLOAD
        self.pipeline.update_stage(PipelineStage.DOWNLOAD, "Starting download", 0)

        if self.current_plugin:
            logger.info("Plugin %s active for ISO save", self.current_plugin.name)
            
        # Trigger download (will call _on_iso_save_download_complete)
        self.downloader.start_download(
            preset['url'], 
            preset['name'], 
            self._on_progress, 
            self._on_iso_save_download_complete, 
            self._on_error
        )

    def _on_iso_save_download_complete(self, file_path):
        """Handle download completion for Save ISO operation."""
        if not file_path:
            self._on_error("Download did not produce an image path")
            return
        logger.info("ISO download complete: %s", file_path)
        
        # Complete download stage
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.DOWNLOAD)

        if self.current_plugin and self.current_plugin.requires_processing():
            # Start PROCESS stage
            GLib.idle_add(self.status_label.set_text, "Processing ISO...")
            if self.pipeline:
                self.pipeline.update_stage(PipelineStage.PROCESS, "Processing ISO...", 0)
            threading.Thread(
                target=self._process_and_save_iso,
                args=(file_path,),
                daemon=True,
            ).start()
        else:
            # No processing needed, save directly
            GLib.idle_add(self._save_iso_to_destination, file_path)

    def _process_and_save_iso(self, file_path):
        """Process ISO with plugin and then save to chosen destination."""
        try:
            if self.current_plugin and hasattr(self.current_plugin, 'on_iso_ready'):
                if self.pipeline:
                    GLib.idle_add(
                        lambda: self.pipeline.update_stage(PipelineStage.PROCESS, "Running plugin...", 50) if self.pipeline else None
                    )
                
                new_path = self.current_plugin.on_iso_ready(
                    file_path, 
                    self.current_preset, 
                    self.ui_values
                )
                if new_path:
                    file_path = new_path

            resolved = Path(file_path).expanduser()
            if not resolved.exists():
                raise FileNotFoundError(f"Processed ISO missing: {resolved}")
            
            # Complete PROCESS stage
            if self.pipeline:
                GLib.idle_add(
                    lambda: self.pipeline.complete_stage(PipelineStage.PROCESS) if self.pipeline else None
                )
            
            # Save to pre-chosen destination
            GLib.idle_add(self._save_iso_to_destination, str(resolved))
            
        except Exception as e:
            logger.exception("Error processing ISO")
            GLib.idle_add(self._on_error, f"Plugin processing failed: {e}")

    def _save_iso_to_destination(self, source_path):
        """Save ISO to the pre-selected destination path."""
        dest_path = getattr(self, '_iso_save_dest_path', None)
        if not dest_path:
            logger.error("No destination path set for ISO save")
            self._on_error("Internal error: No save destination specified")
            return
        
        logger.info(f"Saving ISO from {source_path} to {dest_path}")
        self.status_label.set_text("Saving ISO...")
        
        # Copy in background thread
        threading.Thread(
            target=self._copy_iso_file,
            args=(source_path, dest_path),
            daemon=True,
        ).start()

    def _copy_iso_file(self, source_path, dest_path):
        """Copy ISO file to destination in background thread."""
        try:
            import shutil
            shutil.copy2(source_path, dest_path)
            logger.info(f"ISO saved successfully to {dest_path}")
            
            GLib.idle_add(self._on_iso_save_complete, dest_path)
        except Exception as e:
            logger.exception("Failed to copy ISO")
            GLib.idle_add(self._on_error, f"Failed to save ISO: {e}")

    def _on_iso_save_complete(self, dest_path):
        """Handle successful ISO save completion."""
        self.status_label.set_text(f"ISO saved to {Path(dest_path).name}")
        self._status_page.set_icon_name("emblem-ok-symbolic")
        self._status_page.set_title("ISO Saved Successfully")
        self._status_page.set_description(f"Saved to: {dest_path}")
        
        if self.pipeline:
            self.pipeline.finish(f"ISO saved to {dest_path}")
        
        self._reset_flash_state()

    def _on_progress(self, progress, speed, state):
        GLib.idle_add(self._update_progress_ui, progress, speed, state)

    def _update_progress_ui(self, progress, speed, state):
        fraction = max(0.0, min(1.0, (progress or 0) / 100.0))
        self.progress_bar.set_fraction(fraction)
        speed_display = f"{speed:.1f} kB/s" if isinstance(speed, (int, float)) else str(speed)
        percent = int(fraction * 100)
        
        # Create detailed progress text with speed and state info
        progress_text = f"{percent}% • {speed_display}"
        if state and state != "active":
            progress_text += f" • {state}"
        
        self.status_label.set_text(f"Downloading: {progress_text}")
        if self.pipeline:
            self.pipeline.update_stage(PipelineStage.DOWNLOAD, progress_text, percent)

    def _on_download_complete(self, file_path):
        if not file_path:
            self._on_error("Download did not produce an image path")
            return
        logger.info("Download complete: %s", file_path)
        
        # Complete download stage
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.DOWNLOAD)

        if self.current_plugin and self.current_plugin.requires_processing():
            # Start PROCESS stage
            GLib.idle_add(self.status_label.set_text, "Processing ISO...")
            if self.pipeline:
                self.pipeline.update_stage(PipelineStage.PROCESS, "Processing ISO...", 0)
            threading.Thread(
                target=self._process_iso_in_thread,
                args=(file_path,),
                daemon=True,
            ).start()
        else:
            # Skip processing, go straight to write
            GLib.idle_add(self._start_write_process, file_path)

    def _process_iso_in_thread(self, file_path):
        try:
            if self.current_plugin and hasattr(self.current_plugin, 'on_iso_ready'):
                # Update to show processing in progress
                if self.pipeline:
                    GLib.idle_add(
                        lambda: self.pipeline.update_stage(PipelineStage.PROCESS, "Running plugin...", 50) if self.pipeline else None
                    )
                
                new_path = self.current_plugin.on_iso_ready(
                    file_path, 
                    self.current_preset, 
                    self.ui_values
                )
                if new_path:
                    file_path = new_path

            resolved = Path(file_path).expanduser()
            if not resolved.exists():
                raise FileNotFoundError(f"Processed ISO missing: {resolved}")
            
            # Complete PROCESS stage
            if self.pipeline:
                GLib.idle_add(
                    lambda: self.pipeline.complete_stage(PipelineStage.PROCESS) if self.pipeline else None
                )
            
            GLib.idle_add(self._start_write_process, str(resolved))
            
        except Exception as e:
            logger.exception("Error processing ISO")
            self._on_error(f"Plugin processing failed: {e}")

    def _start_write_process(self, file_path):
        iso_path = Path(file_path).expanduser()
        if not iso_path.exists():
            logger.error("Downloaded ISO missing: %s", iso_path)
            self._on_error(f"Downloaded ISO missing: {iso_path}")
            return

        self.status_label.set_text("Writing to USB...")
        self.progress_bar.set_fraction(0)
        if self.pipeline:
            self.pipeline.update_stage(PipelineStage.WRITE, "Writing to USB...", 0)
        
        # Get the target drive path
        idx = self._drive_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or not self.drives:
            self._on_error("No drive selected for writing")
            return
            
        target_drive = self.drives[idx]
        device_path = target_drive['path']
        
        # Start write thread
        threading.Thread(
            target=self._write_thread_func, 
            args=(iso_path, device_path),
            daemon=True,
        ).start()

    def _write_thread_func(self, iso_path, device_path):
        try:
            iso_path = Path(iso_path)
            def progress_cb(bytes_written, total_bytes):
                if total_bytes > 0:
                    fraction = bytes_written / total_bytes
                    GLib.idle_add(self._update_write_progress, fraction)

            logger.info(f"Starting write of {iso_path} to {device_path}")
            write_iso_to_device(
                iso_path, 
                device_path, 
                progress_callback=progress_cb
            )
            
            GLib.idle_add(self._on_write_complete)
            
        except Exception as e:
            logger.exception("Write failed")
            GLib.idle_add(self._on_error, str(e))

    def _update_write_progress(self, fraction):
        self.progress_bar.set_fraction(fraction)
        percent = int(fraction * 100)
        progress_text = f"{percent}%"
        self.status_label.set_text(f"Writing: {progress_text}")
        if self.pipeline:
            self.pipeline.update_stage(PipelineStage.WRITE, progress_text, percent)

    def _on_write_complete(self):
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.WRITE)
            self.pipeline.success("Flash completed successfully!")
        self.status_label.set_text("Done!")
        self._status_page.set_title("Success")
        self._status_page.set_icon_name("emblem-ok-symbolic")
        self._reset_flash_state()

    def _on_error(self, error_msg):
        logger.error("Error: %s", error_msg)
        GLib.idle_add(self._show_error_ui, error_msg)

    def _show_error_ui(self, error_msg):
        self.status_label.set_text(f"Error: {error_msg}")
        self._status_page.set_icon_name("dialog-error-symbolic")
        if self.pipeline:
            self.pipeline.failure(error_msg)
        self._reset_flash_state()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _show_plugin_notice(self, message: str) -> None:
        notice_field = ConfigField(
            id="plugin-notice",
            label="Plugin unavailable",
            type="info",
            default=message,
        )
        self._form_builder.add_fields([notice_field], self._pref_page)

    def _update_flash_button_state(self) -> None:
        # Check if required fields are filled
        required_fields_filled = self._form_builder.are_required_fields_filled()
        
        # Flash Drive button: requires preset, drive, required fields, and not in progress
        flash_enabled = (
            bool(self.all_presets) 
            and bool(self.drives) 
            and required_fields_filled
            and not self._flash_in_progress
        )
        self._flash_btn.set_sensitive(flash_enabled)
        
        # Save ISO button: requires preset, required fields, plugin support, and not in progress
        save_enabled = (
            bool(self.all_presets) 
            and required_fields_filled
            and not self._flash_in_progress
            and (not self.current_plugin or self.current_plugin.supports_save_iso())
        )
        self._save_iso_btn.set_sensitive(save_enabled)
        
        # Update tooltips
        if self.current_plugin and not self.current_plugin.supports_save_iso():
            self._save_iso_btn.set_tooltip_text(
                f"The {self.current_plugin.name} plugin requires USB device operations "
                "that cannot be saved in an ISO file"
            )
        elif not required_fields_filled:
            self._save_iso_btn.set_tooltip_text("Please fill in all required fields")
        else:
            self._save_iso_btn.set_tooltip_text("Download and save the ISO file")
        
        if not required_fields_filled:
            self._flash_btn.set_tooltip_text("Please fill in all required fields")
        elif not self.drives:
            self._flash_btn.set_tooltip_text("No USB drives detected")
        else:
            self._flash_btn.set_tooltip_text("Write ISO to the selected USB drive")

    def _reset_flash_state(self) -> None:
        self._flash_in_progress = False
        self._update_flash_button_state()


