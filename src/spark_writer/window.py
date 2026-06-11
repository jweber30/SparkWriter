import sys
import os
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GObject, GLib, Gdk
from gi.repository import Pango

from .plugins.base import ConfigField
from .plugins.forms import ConfigFormBuilder
from .plugins.json_plugin import RuntimeApprovalRequiredError
from .plugins.manager import PluginManager
from .profile import ProfileStore
from .receipts import build_receipt_payload
from .return_delivery import (
    build_return_delivery_payload,
    deliver_return_payload,
    is_secure_return_url,
)
from .sources import Source
from .core.downloader import Downloader
from usb_writer_core.receipts import current_timestamp

logger = logging.getLogger(__name__)


class BackgroundDownloadStatus(str, Enum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class BackgroundDownloadState:
    status: BackgroundDownloadStatus = BackgroundDownloadStatus.IDLE
    source_id: str = ""
    source_url: str = ""
    file_path: Optional[str] = None
    progress: float = 0.0
    speed: float = 0.0
    state: str = ""
    error: Optional[str] = None

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
        self._profile_store = ProfileStore()
        self.downloader = Downloader(os.path.expanduser("~/ISO-Downloads"))
        # Pipeline will be initialized per-flash based on required stages
        self.pipeline: Optional[PipelineNotifier] = None
        self.drives: List[Dict[str, Any]] = []
        self.all_sources: List[Source] = []
        self._plugin_entries: List[Dict[str, Any]] = []
        self._sparkplug_rows: List[Gtk.Widget] = []
        self.selected_sparkplugs: List[Any] = []
        self.current_source: Optional[Source] = None
        self._selection_error: Optional[str] = None
        self._latest_receipt_payload: Optional[Dict[str, Any]] = None
        self._flash_in_progress = False
        self._background_download = BackgroundDownloadState()
        self._pending_download_intent: Optional[str] = None
        self._wizard_pages: List[Adw.NavigationPage] = []
        self._wizard_builders: List[ConfigFormBuilder] = []
        self._wizard_page_ids: List[str] = []
        self._profile_prompted_pages: set[str] = set()
        self._return_endpoint_options: List[Dict[str, str]] = []
        
        # Create Pages
        self.config_page = self._create_config_page()
        self.nav_view.add(self.config_page)

        self.final_page = self._create_final_page()
        
        self.progress_page = self._create_progress_page()
        # We don't add progress page yet, we push it later
        
        # Load Plugins
        self._plugin_manager.load_plugins("spark_writer.plugins.installed")
        
        # Load all Sources
        self._load_all_sources()
            
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
        
        # Refresh Sources
        self._load_all_sources()
        
        # Show notification
        if self.pipeline:
            self.pipeline.success(message)
        
        logger.info(message)
    
    def refresh_sources(self):
        """Refresh built-in Sources and derived UI state."""
        self._load_all_sources()

    def _load_all_sources(self):
        self.all_sources = self._plugin_manager.get_manifest_sources()
        model = Gtk.StringList()

        for source in self.all_sources:
            model.append(source.name)

        self._source_row.set_model(model)
        if self.all_sources:
            self._source_row.set_selected(0)
            self._on_source_changed(self._source_row)
        else:
            logger.warning("No Sources available.")
            self._source_row.set_selected(Gtk.INVALID_LIST_POSITION)
            self.current_source = None
            self._clear_sparkplug_rows()
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
        
        # 2. Source Section
        self._source_group = Adw.PreferencesGroup(
            title="Installation Source",
            description="Select the upstream installer image."
        )
        self._pref_page.add(self._source_group)

        self._source_row = Adw.ComboRow(title="Source")
        self._source_row.set_icon_name("computer-symbolic")
        self._source_row.connect("notify::selected", self._on_source_changed)
        self._source_group.add(self._source_row)

        self._sparkplug_group = Adw.PreferencesGroup(
            title="SparkPlugs",
            description="Enable the compatible customization layers to apply."
        )
        self._pref_page.add(self._sparkplug_group)

        self._download_status_group = Adw.PreferencesGroup(title="Download")
        self._pref_page.add(self._download_status_group)
        self._download_status_row = Adw.ActionRow(
            title="Ready to download",
            subtitle="Torrent-backed Sources can download while you configure.",
        )
        self._download_status_row.set_icon_name("folder-download-symbolic")
        self._download_status_group.add(self._download_status_row)

        # Action Bar
        self._action_bar = Gtk.ActionBar()
        toolbar_view.add_bottom_bar(self._action_bar)

        self._download_continue_btn = Gtk.Button(label="Download and Continue")
        self._download_continue_btn.add_css_class("suggested-action")
        self._download_continue_btn.add_css_class("pill")
        self._download_continue_btn.connect("clicked", self._on_download_continue_clicked)
        self._action_bar.pack_end(self._download_continue_btn)

        self._reset_form_btn = Gtk.Button(label="Reset Form")
        self._reset_form_btn.add_css_class("pill")
        self._reset_form_btn.connect("clicked", self._on_reset_form_clicked)
        self._action_bar.pack_start(self._reset_form_btn)
        
        return page

    def _create_final_page(self):
        page = Adw.NavigationPage(title="Ready to Write", tag="final")

        toolbar_view = Adw.ToolbarView()
        page.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self._final_pref_page = Adw.PreferencesPage()
        toolbar_view.set_content(self._final_pref_page)

        self._drive_group = Adw.PreferencesGroup(
            title="Target Device",
            description="Select the USB drive to flash."
        )
        self._final_pref_page.add(self._drive_group)

        self._drive_row = Adw.ComboRow(title="Drive")
        self._drive_row.set_icon_name("drive-removable-media-symbolic")
        self._drive_group.add(self._drive_row)

        refresh_row = Adw.ActionRow(title="Refresh Drives")
        refresh_row.set_icon_name("view-refresh-symbolic")
        refresh_row.set_activatable(True)
        refresh_row.connect("activated", self._refresh_drives)
        self._drive_group.add(refresh_row)

        self._final_status_group = Adw.PreferencesGroup(title="Download Status")
        self._final_pref_page.add(self._final_status_group)
        self._final_download_status_row = Adw.ActionRow(title="No download started")
        self._final_download_status_row.set_icon_name("folder-download-symbolic")
        self._final_status_group.add(self._final_download_status_row)

        self._return_delivery_group = Adw.PreferencesGroup(
            title="Return Delivery",
            description="Send declared SparkPlug secrets and receipt context to a secure endpoint after write."
        )
        self._final_pref_page.add(self._return_delivery_group)

        self._return_endpoint_row = Adw.ComboRow(title="Endpoint")
        self._return_endpoint_row.set_icon_name("network-workgroup-symbolic")
        self._return_endpoint_row.connect("notify::selected", self._on_return_endpoint_changed)
        self._return_delivery_group.add(self._return_endpoint_row)

        self._return_endpoint_url_row = Adw.EntryRow(title="Endpoint URL")
        self._return_endpoint_url_row.connect("notify::text", lambda *_: self._update_flash_button_state())
        self._return_delivery_group.add(self._return_endpoint_url_row)

        if hasattr(Adw, "PasswordEntryRow"):
            self._return_bearer_token_row = Adw.PasswordEntryRow(title="Bearer Token")
        else:
            self._return_bearer_token_row = Adw.EntryRow(title="Bearer Token")
        self._return_bearer_token_row.connect("notify::text", lambda *_: self._update_flash_button_state())
        self._return_delivery_group.add(self._return_bearer_token_row)
        self._return_delivery_group.set_visible(False)

        final_action_bar = Gtk.ActionBar()
        toolbar_view.add_bottom_bar(final_action_bar)

        self._flash_btn = Gtk.Button(label="Flash Drive")
        self._flash_btn.add_css_class("suggested-action")
        self._flash_btn.add_css_class("pill")
        self._flash_btn.connect("clicked", self._on_flash_clicked)
        final_action_bar.pack_end(self._flash_btn)

        self._save_iso_btn = Gtk.Button(label="Save ISO")
        self._save_iso_btn.add_css_class("pill")
        self._save_iso_btn.connect("clicked", self._on_save_iso_clicked)
        final_action_bar.pack_end(self._save_iso_btn)

        reset_btn = Gtk.Button(label="Reset Form")
        reset_btn.add_css_class("pill")
        reset_btn.connect("clicked", self._on_reset_form_clicked)
        final_action_bar.pack_start(reset_btn)

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
        self.status_label.set_selectable(True)
        self.status_label.set_wrap(True)
        self.status_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status_label.set_xalign(0.5)
        self.status_label.set_justify(Gtk.Justification.CENTER)
        self.status_label.set_max_width_chars(96)
        progress_box.append(self.status_label)
        
        # Add secrets display container
        self._secrets_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._secrets_container.set_halign(Gtk.Align.CENTER)
        self._secrets_container.set_visible(False)
        progress_box.append(self._secrets_container)
        
        self._status_page.set_child(progress_box)
        toolbar_view.set_content(self._status_page)

        progress_action_bar = Gtk.ActionBar()
        toolbar_view.add_bottom_bar(progress_action_bar)

        back_btn = Gtk.Button(label="Back to Setup")
        back_btn.add_css_class("pill")
        back_btn.connect("clicked", self._on_back_to_setup_clicked)
        progress_action_bar.pack_start(back_btn)

        reset_from_progress_btn = Gtk.Button(label="Reset Form")
        reset_from_progress_btn.add_css_class("pill")
        reset_from_progress_btn.connect("clicked", self._on_reset_form_clicked)
        progress_action_bar.pack_end(reset_from_progress_btn)
        
        return page

    def _on_back_to_setup_clicked(self, *_args) -> None:
        self._return_to_config_page()

    def _return_to_config_page(self) -> None:
        if self.nav_view.get_visible_page() == self.progress_page:
            self.nav_view.pop()

        self._update_flash_button_state()

    def _on_reset_form_clicked(self, *_args) -> None:
        """Reset selected SparkPlug form fields to manifest defaults and refresh state."""
        self._flash_in_progress = False
        self._pending_download_intent = None
        self._pause_background_download("Reset")
        self._clear_secrets_display()
        self._clear_wizard_pages()
        self._rebuild_selected_config_form()

        while self.nav_view.get_visible_page() != self.config_page:
            self.nav_view.pop()
        self._refresh_drives()
        self._update_flash_button_state()

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

    def _clear_sparkplug_rows(self) -> None:
        for row in self._sparkplug_rows:
            self._sparkplug_group.remove(row)
        self._sparkplug_rows = []

    def _on_source_changed(self, *args):
        idx = self._source_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_sources):
            self.current_source = None
            self._plugin_entries = []
            self.selected_sparkplugs = []
            self._pause_background_download("Source cleared")
            if self._background_download.status != BackgroundDownloadStatus.DOWNLOADING:
                self._background_download = BackgroundDownloadState()
            self._clear_sparkplug_rows()
            self._form_builder.reset(self._pref_page)
            self._update_flash_button_state()
            return

        new_source = self.all_sources[idx]
        if self._background_download.source_id and self._background_download.source_id != new_source.id:
            if self._background_download.status == BackgroundDownloadStatus.DOWNLOADING:
                self._pause_background_download("Source changed")
            else:
                self._background_download = BackgroundDownloadState()

        self.current_source = new_source
        self._rebuild_sparkplug_rows()
        self._refresh_selected_sparkplugs()
        self._update_download_status_ui()

    def _rebuild_sparkplug_rows(self) -> None:
        self._clear_sparkplug_rows()
        self._plugin_entries = []

        if self.current_source is None:
            return

        source_data = self.current_source.to_dict()
        for plugin in self._plugin_manager.get_compatible_plugins(source_data):
            available = bool(getattr(plugin, "is_available", True))
            reason = getattr(plugin, "unavailable_reason", None)
            row = Adw.SwitchRow(title=plugin.name)
            if available:
                row.set_active(True)
            else:
                row.set_sensitive(False)
                row.set_subtitle(reason or "SparkPlug unavailable.")
            row.connect("notify::active", self._on_sparkplug_toggled, plugin)
            self._sparkplug_group.add(row)
            self._sparkplug_rows.append(row)
            self._plugin_entries.append(
                {
                    "plugin": plugin,
                    "row": row,
                    "available": available,
                    "reason": reason,
                }
            )

        if not self._plugin_entries:
            placeholder = Adw.ActionRow(
                title="No compatible SparkPlugs",
                subtitle="This Source can still be saved or flashed unmodified.",
            )
            placeholder.set_sensitive(False)
            self._sparkplug_group.add(placeholder)
            self._sparkplug_rows.append(placeholder)

    def _on_sparkplug_toggled(self, *_args) -> None:
        self._refresh_selected_sparkplugs()

    def _refresh_selected_sparkplugs(self) -> None:
        selected: List[Any] = []
        for entry in self._plugin_entries:
            row = entry["row"]
            if entry["available"] and isinstance(row, Adw.SwitchRow) and row.get_active():
                selected.append(entry["plugin"])

        self.selected_sparkplugs = self._plugin_manager.sort_plugins(selected)
        self._selection_error = self._plugin_manager.validate_plugin_selection(self.selected_sparkplugs)
        self._clear_wizard_pages()
        self._rebuild_selected_config_form()
        self._update_return_delivery_ui()
        self._update_flash_button_state()

    def _rebuild_selected_config_form(self) -> None:
        self._form_builder.reset(self._pref_page)

        if self._selection_error:
            self._show_plugin_notice(self._selection_error)

    def _clear_wizard_pages(self) -> None:
        if hasattr(self, "nav_view"):
            while self.nav_view.get_visible_page() not in (self.config_page, self.progress_page):
                self.nav_view.pop()
        self._wizard_pages = []
        self._wizard_builders = []
        self._wizard_page_ids = []
        self._profile_prompted_pages.clear()

    def _build_config_wizard_specs(self) -> List[Dict[str, Any]]:
        specs: List[Dict[str, Any]] = []

        for plugin in self.selected_sparkplugs:
            fields = plugin.get_config_schema()
            if not fields:
                continue

            fields_by_id = {field.id: field for field in fields if field.id}
            used_fields: set[str] = set()
            plugin_id = str(getattr(plugin, "plugin_id", getattr(plugin, "name", "plugin")))

            for raw_page in plugin.get_wizard_pages():
                page_fields: List[ConfigField] = []
                for raw_field_id in raw_page.get("fields", []):
                    field_id = str(raw_field_id)
                    field = fields_by_id.get(field_id)
                    if field is None:
                        continue
                    page_fields.append(field)
                    used_fields.add(field_id)
                if page_fields:
                    specs.append(
                        {
                            "id": f"{plugin_id}:{raw_page.get('id', 'page')}",
                            "title": str(raw_page.get("title") or plugin.name),
                            "description": raw_page.get("description"),
                            "fields": page_fields,
                        }
                    )

            unlisted = [field for field in fields if field.id not in used_fields]
            if unlisted:
                specs.append(
                    {
                        "id": f"{plugin_id}:configuration",
                        "title": f"{plugin.name} Configuration",
                        "description": None,
                        "fields": unlisted,
                    }
                )

        return specs

    def _create_config_wizard_page(self, spec: Dict[str, Any], index: int, total: int) -> Adw.NavigationPage:
        page = Adw.NavigationPage(title=spec["title"], tag=f"wizard-{index}")
        toolbar_view = Adw.ToolbarView()
        page.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        pref_page = Adw.PreferencesPage()
        toolbar_view.set_content(pref_page)

        builder = ConfigFormBuilder()
        builder.add_fields(
            spec["fields"],
            pref_page,
            title=spec["title"],
            description=spec.get("description"),
        )

        action_bar = Gtk.ActionBar()
        toolbar_view.add_bottom_bar(action_bar)

        next_label = "Review" if index == total - 1 else "Continue"
        next_btn = Gtk.Button(label=next_label)
        next_btn.add_css_class("suggested-action")
        next_btn.add_css_class("pill")
        next_btn.set_sensitive(builder.are_required_fields_filled())
        builder.set_on_change_callback(
            lambda: next_btn.set_sensitive(builder.are_required_fields_filled())
        )

        def on_next(_btn):
            if not builder.are_required_fields_filled():
                return
            self._save_profile_values_from_builder(builder)
            self._push_wizard_or_final(index + 1)

        next_btn.connect("clicked", on_next)
        action_bar.pack_end(next_btn)

        self._wizard_builders.append(builder)
        self._wizard_page_ids.append(str(spec["id"]))
        return page

    def _prepare_wizard_pages(self) -> None:
        self._clear_wizard_pages()
        specs = self._build_config_wizard_specs()
        total = len(specs)
        self._wizard_pages = [
            self._create_config_wizard_page(spec, index, total)
            for index, spec in enumerate(specs)
        ]

    def _push_wizard_or_final(self, index: int = 0) -> None:
        if index < len(self._wizard_pages):
            page = self._wizard_pages[index]
            if self.nav_view.get_visible_page() != page:
                self.nav_view.push(page)
            self._maybe_prompt_profile_autofill(index)
            return

        if self.nav_view.get_visible_page() != self.final_page:
            self.nav_view.push(self.final_page)
        self._refresh_drives()
        self._update_return_delivery_ui()
        self._update_download_status_ui()
        self._update_flash_button_state()

    def _maybe_prompt_profile_autofill(self, index: int) -> None:
        if index >= len(self._wizard_builders):
            return
        page_id = self._wizard_page_ids[index]
        if page_id in self._profile_prompted_pages:
            return

        saved = self._profile_store.load_values()
        builder = self._wizard_builders[index]
        fill_values: Dict[str, Any] = {}
        labels: List[str] = []
        for field in builder.get_fields():
            standard_field = field.standard_field
            if standard_field and standard_field in saved:
                fill_values[field.id] = saved[standard_field]
                labels.append(field.label or field.id)

        if not fill_values:
            return

        self._profile_prompted_pages.add(page_id)
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Use Saved Profile Values?",
        )
        dialog.props.secondary_text = (
            "Spark Writer has saved values for this step:\n\n"
            + "\n".join(f"  - {label}" for label in labels)
            + "\n\nFill empty fields with these values?"
        )

        def on_response(dlg, result):
            if result == Gtk.ResponseType.YES:
                builder.set_values(fill_values, only_empty=True)
            dlg.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _collect_ui_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for builder in self._wizard_builders:
            values.update(builder.get_values())
        values.update(self._form_builder.get_values())
        return values

    def _save_profile_values_from_builder(self, builder: ConfigFormBuilder) -> None:
        values = builder.get_values()
        profile_values: Dict[str, Any] = {}
        for field in builder.get_fields():
            storage = field.storage or {}
            if not field.standard_field:
                continue
            if storage.get("scope") != "profile" and not storage.get("persist"):
                continue
            value = values.get(field.id)
            if value not in (None, ""):
                profile_values[field.standard_field] = value
        self._profile_store.save_values(profile_values)

    def _save_profile_values_from_all_builders(self) -> None:
        for builder in self._wizard_builders:
            self._save_profile_values_from_builder(builder)

    def _supports_early_download(self, source: Optional[Source]) -> bool:
        if source is None:
            return False
        url = (source.url or "").lower()
        return (
            source.acquire_kind == "torrent"
            or url.startswith("magnet:")
            or url.endswith(".torrent")
        )

    def _on_download_continue_clicked(self, *_args) -> None:
        if self.current_source is None or self._selection_error:
            return
        if not self._supports_early_download(self.current_source):
            return

        self._start_or_reuse_background_download(self.current_source)
        self._prepare_wizard_pages()
        self._push_wizard_or_final(0)

    def _start_or_reuse_background_download(self, source: Source) -> None:
        state = self._background_download
        if state.source_id == source.id and state.source_url == source.url:
            if state.status in {
                BackgroundDownloadStatus.DOWNLOADING,
                BackgroundDownloadStatus.COMPLETE,
            }:
                self._update_download_status_ui()
                return

        if state.status == BackgroundDownloadStatus.DOWNLOADING:
            self._pause_background_download("Starting a different download")

        self._background_download = BackgroundDownloadState(
            status=BackgroundDownloadStatus.DOWNLOADING,
            source_id=source.id,
            source_url=source.url,
        )
        self._update_download_status_ui()
        self.downloader.start_download(
            source.url,
            source.name,
            self._on_background_download_progress,
            self._on_background_download_complete,
            self._on_background_download_error,
            acquire_kind=source.acquire_kind,
            artifact=source.acquire_artifact,
        )

    def _pause_background_download(self, reason: str) -> None:
        if self._background_download.status != BackgroundDownloadStatus.DOWNLOADING:
            return
        logger.info("Pausing background download: %s", reason)
        self.downloader.pause()
        self._background_download.status = BackgroundDownloadStatus.PAUSED
        self._background_download.state = reason
        self._pending_download_intent = None
        self._update_download_status_ui()

    def _on_background_download_progress(self, progress, speed, state):
        self._background_download.progress = float(progress or 0)
        self._background_download.speed = float(speed or 0)
        self._background_download.state = str(state or "")
        GLib.idle_add(self._update_download_status_ui)

        if self._pending_download_intent:
            GLib.idle_add(self._update_progress_ui, progress, speed, state)

    def _on_background_download_complete(self, file_path):
        if not file_path:
            self._on_background_download_error("Download did not produce an image path")
            return

        self._background_download.status = BackgroundDownloadStatus.COMPLETE
        self._background_download.file_path = file_path
        self._background_download.progress = 100.0
        self._background_download.error = None
        GLib.idle_add(self._update_download_status_ui)

        if self._pending_download_intent == "flash":
            self._pending_download_intent = None
            GLib.idle_add(self._on_download_complete, file_path)
        elif self._pending_download_intent == "save":
            self._pending_download_intent = None
            GLib.idle_add(self._on_iso_save_download_complete, file_path)

    def _on_background_download_error(self, error_msg):
        self._background_download.status = BackgroundDownloadStatus.FAILED
        self._background_download.error = str(error_msg)
        GLib.idle_add(self._update_download_status_ui)
        if self._pending_download_intent:
            self._pending_download_intent = None
            self._on_error(error_msg)

    def _update_download_status_ui(self) -> None:
        state = self._background_download
        if state.status == BackgroundDownloadStatus.IDLE:
            title = "Ready to download"
            subtitle = "Torrent-backed Sources can download while you configure."
        elif state.status == BackgroundDownloadStatus.DOWNLOADING:
            title = f"Downloading {int(state.progress)}%"
            speed = f"{state.speed:.1f} kB/s" if state.speed else "Starting"
            subtitle = f"{speed} - {state.state or 'active'}"
        elif state.status == BackgroundDownloadStatus.PAUSED:
            title = "Download paused"
            subtitle = "Partial files remain in the download folder."
        elif state.status == BackgroundDownloadStatus.COMPLETE:
            title = "Download ready"
            subtitle = Path(state.file_path or "").name
        else:
            title = "Download failed"
            subtitle = state.error or "Unknown error"

        for row_name in ("_download_status_row", "_final_download_status_row"):
            row = getattr(self, row_name, None)
            if row is not None:
                row.set_title(title)
                row.set_subtitle(subtitle)

    def _background_download_matches(self, source: Source) -> bool:
        return (
            self._background_download.source_id == source.id
            and self._background_download.source_url == source.url
        )

    def _consume_or_start_download(
        self,
        *,
        source: Source,
        intent: str,
        on_complete: Callable[[str], None],
    ) -> None:
        if self._background_download_matches(source):
            if (
                self._background_download.status == BackgroundDownloadStatus.COMPLETE
                and self._background_download.file_path
            ):
                GLib.idle_add(on_complete, self._background_download.file_path)
                return

            if self._background_download.status == BackgroundDownloadStatus.DOWNLOADING:
                self._pending_download_intent = intent
                self._update_download_status_ui()
                return

        self._pending_download_intent = None
        self.downloader.start_download(
            source.url,
            source.name,
            self._on_progress,
            on_complete,
            self._on_error,
            acquire_kind=source.acquire_kind,
            artifact=source.acquire_artifact,
        )

    def _on_flash_clicked(self, btn):
        idx = self._source_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_sources):
            return

        source = self.all_sources[idx]
        self.current_source = source

        drive_idx = self._drive_row.get_selected()
        if drive_idx == Gtk.INVALID_LIST_POSITION or not self.drives:
            logger.warning("Flash requested without selecting a drive")
            return

        self.ui_values = self._collect_ui_values()
        self._save_profile_values_from_all_builders()

        self._run_preflight_runtime_approvals(
            include_write_phase=True,
            on_approved=lambda: self._start_flash_workflow(source),
        )

    def _start_flash_workflow(self, source: Source) -> None:
        self._flash_in_progress = True
        self._update_flash_button_state()
        self._clear_secrets_display()
        self._latest_receipt_payload = None
        self._last_original_iso_path = None
        self._last_processed_iso_path = None

        # Calculate which stages this flash will need
        stages = [PipelineStage.DOWNLOAD]
        if any(plugin.requires_processing() for plugin in self.selected_sparkplugs):
            stages.append(PipelineStage.PROCESS)
        stages.append(PipelineStage.WRITE)
        # VERIFY and FINALIZE not yet implemented, omit for now
        
        # Initialize pipeline with required stages
        self.pipeline = PipelineNotifier(app_name="SparkGTK", stages=stages)

        # Switch to progress page
        if self.nav_view.get_visible_page() != self.progress_page:
            self.nav_view.push(self.progress_page)
        self.progress_page.set_title(f"Flashing {source.name}")
        self._status_page.set_title(f"Flashing {source.name}")
        self.progress_bar.set_fraction(0.1)
        self.pipeline.start(f"Provisioning {source.name}")
        self._active_stage = PipelineStage.DOWNLOAD
        self.pipeline.update_stage(PipelineStage.DOWNLOAD, "Starting download", 0)


        # Start download/flash process
        if self.selected_sparkplugs:
            logger.info(
                "SparkPlugs active: %s",
                ", ".join(plugin.name for plugin in self.selected_sparkplugs),
            )
            
        self._consume_or_start_download(
            source=source,
            intent="flash",
            on_complete=self._on_download_complete,
        )

    def _on_save_iso_clicked(self, btn):
        """Handle Save ISO button click - first choose save location, then download."""
        idx = self._source_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.all_sources):
            return

        source = self.all_sources[idx]
        self.current_source = source
        self.ui_values = self._collect_ui_values()
        self._save_profile_values_from_all_builders()

        self._run_preflight_runtime_approvals(
            include_write_phase=False,
            on_approved=lambda: self._prompt_iso_save_location(source),
        )

    def _run_preflight_runtime_approvals(
        self,
        *,
        include_write_phase: bool,
        on_approved: Callable[[], None],
    ) -> None:
        """Evaluate runtime approvals at action start so download can proceed uninterrupted."""
        if self._selection_error:
            self._on_error(self._selection_error)
            return

        pending: List[tuple[Any, Any]] = []
        for plugin in self.selected_sparkplugs:
            if not hasattr(plugin, 'get_pending_phase_approval'):
                continue
            if plugin.requires_processing():
                iso_pending = plugin.get_pending_phase_approval("on_iso_ready")
                if iso_pending:
                    pending.append((plugin, iso_pending))

            if include_write_phase:
                write_pending = plugin.get_pending_phase_approval("on_write_complete")
                if write_pending:
                    pending.append((plugin, write_pending))

        if not pending:
            on_approved()
            return

        def prompt_next(index: int) -> None:
            if index >= len(pending):
                on_approved()
                return

            plugin, phase_pending = pending[index]
            self._prompt_runtime_approval_async(
                plugin_label=getattr(plugin, 'name', 'plugin'),
                phase_name=phase_pending.phase_name,
                commands=phase_pending.commands,
                callback=lambda approved: self._on_preflight_prompt_result(
                    approved=approved,
                    plugin=plugin,
                    commands=phase_pending.commands,
                    next_prompt=lambda: prompt_next(index + 1),
                    phase_name=phase_pending.phase_name,
                ),
            )

        prompt_next(0)

    def _on_preflight_prompt_result(
        self,
        *,
        approved: bool,
        plugin: Any,
        commands: List[str],
        next_prompt: Callable[[], None],
        phase_name: str,
    ) -> None:
        if not approved:
            self._on_error(f"Runtime approval canceled by user for phase '{phase_name}'.")
            return

        try:
            plugin.approve_runtime_commands(commands)
        except Exception as exc:
            self._on_error(f"Plugin processing failed: {exc}")
            return

        next_prompt()

    def _prompt_iso_save_location(self, source: Source):
        """Prompt user to choose where to save the ISO file before downloading."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Save ISO File")
        
        # Set suggested filename based on Source name
        suggested_name = f"{source.name}.iso"
        dialog.set_initial_name(suggested_name)
        
        # Set initial folder to Downloads or home
        downloads_path = Path.home() / "Downloads"
        if downloads_path.exists():
            downloads_file = Gio.File.new_for_path(str(downloads_path))
            dialog.set_initial_folder(downloads_file)
        
        dialog.save(self, None, self._on_iso_save_location_chosen, source)

    def _on_iso_save_location_chosen(self, dialog, result, source: Source):
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
            self._start_iso_save_workflow(source)
            
        except GLib.Error as e:
            # User cancelled or error occurred
            if e.code != 2:  # 2 = dismissed/cancelled
                logger.error(f"Error in file dialog: {e}")
                self._on_error(f"Failed to select save location: {e.message}")
            else:
                logger.info("Save cancelled by user")

    def _start_iso_save_workflow(self, source: Source):
        """Start the download and processing workflow after save location is chosen."""
        self._flash_in_progress = True
        self._update_flash_button_state()
        self._clear_secrets_display()
        self._latest_receipt_payload = None
        self._last_original_iso_path = None
        self._last_processed_iso_path = None

        # Calculate stages for ISO save (no WRITE stage)
        stages = [PipelineStage.DOWNLOAD]
        if any(plugin.requires_processing() for plugin in self.selected_sparkplugs):
            stages.append(PipelineStage.PROCESS)
        
        # Initialize pipeline
        self.pipeline = PipelineNotifier(app_name="SparkGTK", stages=stages)

        # Switch to progress page
        if self.nav_view.get_visible_page() != self.progress_page:
            self.nav_view.push(self.progress_page)
        self.progress_page.set_title(f"Saving {source.name}")
        self._status_page.set_title(f"Saving {source.name}")
        self.progress_bar.set_fraction(0.1)
        self.pipeline.start(f"Downloading {source.name}")
        self._active_stage = PipelineStage.DOWNLOAD
        self.pipeline.update_stage(PipelineStage.DOWNLOAD, "Starting download", 0)

        if self.selected_sparkplugs:
            logger.info(
                "SparkPlugs active for ISO save: %s",
                ", ".join(plugin.name for plugin in self.selected_sparkplugs),
            )
            
        self._consume_or_start_download(
            source=source,
            intent="save",
            on_complete=self._on_iso_save_download_complete,
        )

    def _on_iso_save_download_complete(self, file_path):
        """Handle download completion for Save ISO operation."""
        if not file_path:
            self._on_error("Download did not produce an image path")
            return
        logger.info("ISO download complete: %s", file_path)
        self._last_original_iso_path = file_path
        
        # Complete download stage
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.DOWNLOAD)

        if any(plugin.requires_processing() for plugin in self.selected_sparkplugs):
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
        """Process ISO with selected SparkPlugs and then save to chosen destination."""
        try:
            if self.selected_sparkplugs:
                if self.pipeline:
                    GLib.idle_add(
                        lambda: self.pipeline.update_stage(PipelineStage.PROCESS, "Running SparkPlugs...", 50) if self.pipeline else None
                    )
                
                new_path = self._run_iso_phase_with_runtime_approval(file_path)
                if new_path:
                    file_path = new_path

            resolved = Path(file_path).expanduser()
            if not resolved.exists():
                raise FileNotFoundError(f"Processed ISO missing: {resolved}")
            self._last_processed_iso_path = str(resolved)
            
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
        self._latest_receipt_payload = self._build_receipt_payload()
        secrets = self._collect_completion_secrets()
        if secrets:
            self._update_secrets_display(secrets)

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
        self._last_original_iso_path = file_path
        
        # Complete download stage
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.DOWNLOAD)

        if any(plugin.requires_processing() for plugin in self.selected_sparkplugs):
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
            if self.selected_sparkplugs:
                # Update to show processing in progress
                if self.pipeline:
                    GLib.idle_add(
                        lambda: self.pipeline.update_stage(PipelineStage.PROCESS, "Running SparkPlugs...", 50) if self.pipeline else None
                    )
                
                new_path = self._run_iso_phase_with_runtime_approval(file_path)
                if new_path:
                    file_path = new_path

            resolved = Path(file_path).expanduser()
            if not resolved.exists():
                raise FileNotFoundError(f"Processed ISO missing: {resolved}")
            self._last_processed_iso_path = str(resolved)
            
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
            
            GLib.idle_add(self._on_write_complete, device_path)
            
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

    def _on_write_complete(self, device_path: str):
        self.status_label.set_text("Running post-write plugin actions...")
        threading.Thread(
            target=self._post_write_processing_thread,
            args=(device_path,),
            daemon=True,
        ).start()

    def _post_write_processing_thread(self, device_path: str) -> None:
        try:
            if self.selected_sparkplugs:
                self._run_write_complete_with_runtime_approval(device_path)
            GLib.idle_add(self._finalize_success)
        except Exception as e:
            logger.exception("Post-write plugin processing failed")
            GLib.idle_add(self._on_error, f"Plugin processing failed: {e}")

    def _finalize_success(self) -> None:
        if self.pipeline:
            self.pipeline.complete_stage(PipelineStage.WRITE)
            self.pipeline.success("Flash completed successfully!")

        device_info = self._selected_drive_info()
        self._latest_receipt_payload = self._build_receipt_payload(device_info=device_info)
        secrets = self._collect_completion_secrets()
        delivery_message = self._deliver_return_payload(secrets, device_info)
        if secrets:
            self._update_secrets_display(secrets)
        
        self.status_label.set_text(delivery_message or "Done!")
        self._status_page.set_title("Success")
        self._status_page.set_icon_name("emblem-ok-symbolic")
        self._reset_flash_state()

    def _run_iso_phase_with_runtime_approval(self, file_path: str) -> str:
        if not self.current_source:
            return file_path

        current_path = file_path
        source_data = self.current_source.to_dict()
        for plugin in self.selected_sparkplugs:
            current_path = self._run_with_runtime_approval(
                plugin=plugin,
                phase_name="on_iso_ready",
                run_action=lambda plugin=plugin, current_path=current_path: plugin.on_iso_ready(
                    current_path,
                    source_data,
                    self.ui_values,
                ),
            )
        return current_path

    def _run_write_complete_with_runtime_approval(self, device_path: str) -> None:
        if not self.current_source:
            return

        source_data = self.current_source.to_dict()
        for plugin in self.selected_sparkplugs:
            self._run_with_runtime_approval(
                plugin=plugin,
                phase_name="on_write_complete",
                run_action=lambda plugin=plugin: plugin.on_write_complete(
                    device_path,
                    source_data,
                    self.ui_values,
                ),
            )

    def _run_with_runtime_approval(self, *, plugin: Any, phase_name: str, run_action):
        retries = 0
        while True:
            try:
                return run_action()
            except RuntimeApprovalRequiredError as approval_error:
                if retries >= 1:
                    raise RuntimeError(
                        f"Runtime approval retry failed for phase '{phase_name}'."
                    ) from approval_error

                approved = self._prompt_runtime_approval(approval_error)
                if not approved:
                    raise RuntimeError(
                        f"Runtime approval canceled by user for phase '{phase_name}'."
                    ) from approval_error

                if not hasattr(plugin, 'approve_runtime_commands'):
                    raise RuntimeError("Selected plugin cannot persist runtime approvals")

                plugin.approve_runtime_commands(approval_error.pending.commands)
                retries += 1

    def _prompt_runtime_approval(self, approval_error: RuntimeApprovalRequiredError) -> bool:
        """Prompt user for runtime command approval and block worker thread for response."""
        response: Dict[str, bool] = {"approved": False}
        done = threading.Event()

        command_lines = "\n".join(f"  - {command}" for command in approval_error.pending.commands)
        plugin_label = approval_error.plugin_id or "plugin"
        message = (
            f"Plugin: {plugin_label}\n"
            f"Phase: {approval_error.pending.phase_name}\n\n"
            "This plugin needs approval to execute the following commands:\n\n"
            f"{command_lines}\n\n"
            "Approve and continue?"
        )

        self._prompt_runtime_approval_async(
            plugin_label=plugin_label,
            phase_name=approval_error.pending.phase_name,
            commands=approval_error.pending.commands,
            callback=lambda approved: (response.__setitem__("approved", approved), done.set()),
        )
        done.wait()
        return response["approved"]

    def _prompt_runtime_approval_async(
        self,
        *,
        plugin_label: str,
        phase_name: str,
        commands: List[str],
        callback: Callable[[bool], None],
    ) -> None:
        command_lines = "\n".join(f"  - {command}" for command in commands)
        message = (
            f"Plugin: {plugin_label}\n"
            f"Phase: {phase_name}\n\n"
            "This plugin needs approval to execute the following commands:\n\n"
            f"{command_lines}\n\n"
            "Approve and continue?"
        )

        def show_dialog():
            dialog = Gtk.MessageDialog(
                transient_for=self,
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Runtime Command Approval",
            )
            dialog.props.secondary_text = message

            def on_response(dlg, result):
                approved = result == Gtk.ResponseType.YES
                dlg.destroy()
                callback(approved)

            dialog.connect("response", on_response)
            dialog.present()
            return False

        GLib.idle_add(show_dialog)

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
            label="SparkPlug notice",
            type="info",
            default=message,
        )
        self._form_builder.add_fields([notice_field], self._pref_page)

    def _update_flash_button_state(self) -> None:
        # Check if required fields are filled
        required_fields_filled = self._form_builder.are_required_fields_filled()
        for builder in self._wizard_builders:
            if not builder.are_required_fields_filled():
                required_fields_filled = False
        has_source = bool(self.all_sources) and self.current_source is not None
        save_supported = all(plugin.supports_save_iso() for plugin in self.selected_sparkplugs)
        usb_supported = all(plugin.supports_usb_write() for plugin in self.selected_sparkplugs)
        early_supported = self._supports_early_download(self.current_source)
        return_delivery_ready = self._is_return_delivery_ready()

        if hasattr(self, "_download_continue_btn"):
            download_enabled = (
                has_source
                and early_supported
                and not self._selection_error
                and not self._flash_in_progress
            )
            self._download_continue_btn.set_sensitive(download_enabled)
            if self._selection_error:
                self._download_continue_btn.set_tooltip_text(self._selection_error)
            elif not early_supported:
                self._download_continue_btn.set_tooltip_text(
                    "Download and Continue is available for torrent Sources"
                )
            else:
                self._download_continue_btn.set_tooltip_text(
                    "Start the torrent download and continue configuration"
                )
        
        # Flash Drive button: requires Source, drive, valid selection, required fields, and not in progress
        flash_enabled = (
            has_source
            and bool(self.drives) 
            and required_fields_filled
            and not self._selection_error
            and not self._flash_in_progress
            and usb_supported
            and bool(self.current_source and self.current_source.can_write_usb)
            and return_delivery_ready
        )
        self._flash_btn.set_sensitive(flash_enabled)
        
        # Save ISO button: requires Source, valid selection, required fields, and not in progress
        save_enabled = (
            has_source
            and required_fields_filled
            and not self._selection_error
            and not self._flash_in_progress
            and save_supported
            and bool(self.current_source and self.current_source.can_export_iso)
        )
        self._save_iso_btn.set_sensitive(save_enabled)
        
        # Update tooltips
        if self._selection_error:
            self._save_iso_btn.set_tooltip_text(self._selection_error)
        elif self.current_source and not self.current_source.can_export_iso:
            self._save_iso_btn.set_tooltip_text("This manifest does not export an ISO")
        elif not save_supported:
            self._save_iso_btn.set_tooltip_text(
                "One or more selected SparkPlugs require USB device operations "
                "that cannot be saved in an ISO file"
            )
        elif not required_fields_filled:
            self._save_iso_btn.set_tooltip_text("Please fill in all required fields")
        else:
            self._save_iso_btn.set_tooltip_text("Download and save the selected Source")
        
        if self._selection_error:
            self._flash_btn.set_tooltip_text(self._selection_error)
        elif not required_fields_filled:
            self._flash_btn.set_tooltip_text("Please fill in all required fields")
        elif not self.drives:
            self._flash_btn.set_tooltip_text("No USB drives detected")
        elif not return_delivery_ready:
            self._flash_btn.set_tooltip_text("Enter an HTTPS or localhost return delivery endpoint")
        else:
            self._flash_btn.set_tooltip_text("Write ISO to the selected USB drive")

    def _reset_flash_state(self) -> None:
        self._flash_in_progress = False
        self._refresh_drives()
        self._update_flash_button_state()

    def _selected_drive_info(self) -> Optional[Dict[str, Any]]:
        idx = self._drive_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.drives):
            return None
        return dict(self.drives[idx])

    def _build_receipt_payload(self, device_info: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if self.current_source is None:
            return None
        return build_receipt_payload(
            source=self.current_source,
            sparkplugs=self.selected_sparkplugs,
            original_iso_path=getattr(self, "_last_original_iso_path", None),
            processed_iso_path=getattr(self, "_last_processed_iso_path", None),
            device_info=device_info,
        )

    def _return_delivery_required(self) -> bool:
        return any(
            bool(getattr(plugin, "requires_return_delivery", lambda: False)())
            for plugin in self.selected_sparkplugs
        )

    def _collect_return_endpoint_options(self) -> List[Dict[str, str]]:
        options: List[Dict[str, str]] = []
        seen: set[str] = set()
        for plugin in self.selected_sparkplugs:
            spec = getattr(plugin, "get_return_delivery_spec", lambda: {})()
            for endpoint in spec.get("endpoints", []):
                if not isinstance(endpoint, dict):
                    continue
                endpoint_id = str(endpoint.get("id", "")).strip()
                url = str(endpoint.get("url", "")).strip()
                if not endpoint_id or endpoint_id in seen:
                    continue
                seen.add(endpoint_id)
                options.append(
                    {
                        "id": endpoint_id,
                        "label": str(endpoint.get("label") or endpoint_id),
                        "url": url,
                    }
                )
        options.append({"id": "custom", "label": "Custom", "url": ""})
        return options

    def _update_return_delivery_ui(self) -> None:
        if not hasattr(self, "_return_delivery_group"):
            return
        required = self._return_delivery_required()
        self._return_delivery_group.set_visible(required)
        if not required:
            return

        current_url = self._return_endpoint_url_row.get_text().strip()
        self._return_endpoint_options = self._collect_return_endpoint_options()
        model = Gtk.StringList()
        for option in self._return_endpoint_options:
            model.append(option["label"])
        self._return_endpoint_row.set_model(model)
        self._return_endpoint_row.set_selected(0 if self._return_endpoint_options else Gtk.INVALID_LIST_POSITION)

        selected = self._selected_return_endpoint_option()
        if selected and selected.get("url") and not current_url:
            self._return_endpoint_url_row.set_text(selected["url"])

    def _selected_return_endpoint_option(self) -> Optional[Dict[str, str]]:
        selected = self._return_endpoint_row.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return None
        if selected >= len(self._return_endpoint_options):
            return None
        return self._return_endpoint_options[selected]

    def _on_return_endpoint_changed(self, *_args) -> None:
        selected = self._selected_return_endpoint_option()
        if selected and selected.get("url"):
            self._return_endpoint_url_row.set_text(selected["url"])
        self._update_flash_button_state()

    def _return_endpoint_url(self) -> str:
        if not hasattr(self, "_return_endpoint_url_row"):
            return ""
        return self._return_endpoint_url_row.get_text().strip()

    def _return_bearer_token(self) -> str:
        if not hasattr(self, "_return_bearer_token_row"):
            return ""
        return self._return_bearer_token_row.get_text().strip()

    def _is_return_delivery_ready(self) -> bool:
        if not self._return_delivery_required():
            return True
        return is_secure_return_url(self._return_endpoint_url())

    def _collect_declared_return_secrets(self) -> Dict[str, Dict[str, str]]:
        payload: Dict[str, Dict[str, str]] = {}
        for plugin in self.selected_sparkplugs:
            spec = getattr(plugin, "get_return_delivery_spec", lambda: {})()
            secret_keys = spec.get("secrets", [])
            if not secret_keys:
                continue
            try:
                plugin_secrets = plugin.get_ephemeral_secrets()
            except Exception as exc:
                logger.warning("Failed to retrieve return delivery secrets: %s", exc)
                continue
            selected: Dict[str, str] = {}
            for key in secret_keys:
                key_name = str(key).strip()
                value = plugin_secrets.get(key_name)
                if value is not None and str(value).strip():
                    selected[key_name] = str(value)
            if selected:
                plugin_id = str(getattr(plugin, "plugin_id", getattr(plugin, "name", "plugin")))
                payload[plugin_id] = selected
        return payload

    def _delivery_sparkplug_identities(self) -> List[Dict[str, Any]]:
        identities: List[Dict[str, Any]] = []
        for plugin in self.selected_sparkplugs:
            if not getattr(plugin, "requires_return_delivery", lambda: False)():
                continue
            metadata = getattr(plugin, "manifest", {}).get("metadata", {})
            identity: Dict[str, Any] = {
                "id": getattr(plugin, "plugin_id", getattr(plugin, "name", "unknown")),
                "name": getattr(plugin, "name", "Unknown SparkPlug"),
            }
            if metadata.get("version"):
                identity["version"] = metadata["version"]
            identities.append(identity)
        return identities

    def _deliver_return_payload(
        self,
        secrets: Dict[str, str],
        device_info: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        if not self._return_delivery_required():
            return None

        endpoint_url = self._return_endpoint_url()
        if not is_secure_return_url(endpoint_url):
            return "Done. Return delivery skipped: enter an HTTPS or localhost endpoint."

        return_secrets = self._collect_declared_return_secrets()
        if not return_secrets:
            return "Done. Return delivery skipped: no declared secrets were available."

        receipt_payload = self._latest_receipt_payload or {}
        generated_at = str(
            receipt_payload.get("identity", {}).get("generated_at")
            if isinstance(receipt_payload, dict)
            else ""
        ) or current_timestamp()

        payload = build_return_delivery_payload(
            sparkplugs=self._delivery_sparkplug_identities(),
            secrets=return_secrets,
            receipt=self._latest_receipt_payload,
            source=self.current_source.to_dict() if self.current_source else None,
            device=device_info,
            generated_at=generated_at,
        )

        try:
            result = deliver_return_payload(
                endpoint_url=endpoint_url,
                payload=payload,
                bearer_token=self._return_bearer_token(),
            )
        except ValueError as exc:
            logger.warning("Return delivery skipped: %s", exc)
            return f"Done. Return delivery skipped: {exc}"

        if result.success:
            return "Done. Return delivery succeeded."

        logger.warning("Return delivery failed: %s", result.message)
        return f"Done. {result.message}"

    def _collect_completion_secrets(self) -> Dict[str, str]:
        """Collect secrets to display on completion, including root password fallbacks."""
        secrets: Dict[str, str] = {}

        for plugin in self.selected_sparkplugs:
            try:
                plugin_secrets = plugin.get_ephemeral_secrets()
                if isinstance(plugin_secrets, dict):
                    secrets.update({str(k): str(v) for k, v in plugin_secrets.items() if str(v).strip()})
            except Exception as e:
                logger.warning(f"Failed to retrieve ephemeral secrets: {e}")

        ui_values = getattr(self, 'ui_values', {})
        if isinstance(ui_values, dict):
            # Ensure root credentials are shown even when provided by user input.
            root_password = str(ui_values.get('root-password') or '').strip()
            root_password_hashed = str(ui_values.get('root-password-hashed') or '').strip()

            has_root_secret = any(
                str(key).lower() in {'proxmox_root_password', 'root_password'}
                for key in secrets.keys()
            )

            if root_password and not has_root_secret:
                secrets['root_password'] = root_password
            elif root_password_hashed and 'root_password_hashed' not in secrets:
                secrets['root_password_hashed'] = root_password_hashed

        return secrets

    def _clear_secrets_display(self) -> None:
        """Clear any previously rendered secret widgets."""
        child = self._secrets_container.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self._secrets_container.remove(child)
            child = next_child
        self._secrets_container.set_visible(False)

    def _update_secrets_display(self, secrets: Dict[str, str]) -> None:
        """Update the secrets display with the provided secrets dictionary."""
        # Clear existing secret widgets
        self._clear_secrets_display()
        
        if not secrets:
            self._secrets_container.set_visible(False)
            return
        
        # Add a separator for visual distinction
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self._secrets_container.append(separator)
        
        # Add heading
        heading = Gtk.Label(label="Generated Credentials")
        heading.set_markup("<b>Generated Credentials</b>")
        heading.set_halign(Gtk.Align.CENTER)
        self._secrets_container.append(heading)
        
        # Add each secret
        for secret_key, secret_value in secrets.items():
            secret_entry = self._create_ephemeral_secret_entry(secret_key, secret_value)
            self._secrets_container.append(secret_entry)
        
        self._secrets_container.set_visible(True)

    def _create_ephemeral_secret_entry(self, key: str, value: str) -> Gtk.Widget:
        """Create a single secret entry widget with toggle visibility and copy button."""
        # Main container
        entry_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        entry_box.set_margin_top(8)
        entry_box.set_margin_bottom(8)
        entry_box.set_margin_start(16)
        entry_box.set_margin_end(16)
        
        # Label for the secret name
        label = Gtk.Label(label=key)
        label.set_markup(f"<small>{key}</small>")
        label.set_halign(Gtk.Align.START)
        entry_box.append(label)
        
        # Secret value display (wrapped, selectable text)
        secret_display_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        secret_display_box.set_halign(Gtk.Align.CENTER)
        
        # Create a frame for the secret text
        frame = Gtk.Frame()
        frame_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame_inner.set_margin_top(4)
        frame_inner.set_margin_bottom(4)
        frame_inner.set_margin_start(8)
        frame_inner.set_margin_end(8)
        
        secret_label = Gtk.Label(label="•" * len(value))
        secret_label.set_selectable(True)
        secret_label.set_wrap(True)
        secret_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        secret_label.set_max_width_chars(48)
        
        # Store the actual value and visibility state in the label's data
        secret_label.set_data("secret_value", value)
        secret_label.set_data("is_visible", False)
        
        frame_inner.append(secret_label)
        frame.set_child(frame_inner)
        secret_display_box.append(frame)
        
        # Button container (vertical to stack toggle and copy)
        button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Toggle visibility button
        def toggle_visibility(*args):
            is_visible = secret_label.get_data("is_visible")
            is_visible = not is_visible
            secret_label.set_data("is_visible", is_visible)
            
            if is_visible:
                secret_label.set_label(value)
                toggle_btn.set_label("Hide")
            else:
                secret_label.set_label("•" * len(value))
                toggle_btn.set_label("Show")
        
        toggle_btn = Gtk.Button(label="Show")
        toggle_btn.set_size_request(70, -1)
        toggle_btn.connect("clicked", toggle_visibility)
        button_box.append(toggle_btn)
        
        # Copy button
        def copy_to_clipboard(*args):
            clipboard = Gdk.Clipboard.get_default()
            clipboard.set_text(value)
            # Brief feedback
            copy_btn.set_label("Copied!")
            GLib.timeout_add(2000, lambda: copy_btn.set_label("Copy") or False)
        
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.set_size_request(70, -1)
        copy_btn.connect("clicked", copy_to_clipboard)
        button_box.append(copy_btn)
        
        secret_display_box.append(button_box)
        entry_box.append(secret_display_box)
        
        return entry_box
