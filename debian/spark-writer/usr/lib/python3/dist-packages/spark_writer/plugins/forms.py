import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import ConfigField


@dataclass
class FieldBinding:
    field: ConfigField
    widget: Gtk.Widget
    row: Gtk.Widget


class ConfigFormBuilder:
    """Render ConfigField definitions into Adwaita widgets and collect values."""

    def __init__(self, on_change_callback=None) -> None:
        self._bindings: Dict[str, FieldBinding] = {}
        self._current_group: Optional[Adw.PreferencesGroup] = None
        self._on_change_callback = on_change_callback

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def reset(self, page: Adw.PreferencesPage) -> None:
        if self._current_group:
            page.remove(self._current_group)
            self._current_group = None
        self._bindings.clear()

    def add_fields(self, fields: List[ConfigField], page: Adw.PreferencesPage) -> None:
        # Create a preferences group for the fields
        self._current_group = Adw.PreferencesGroup(title="Configuration")
        page.add(self._current_group)
        
        for field in fields:
            self.add_field(field, self._current_group)

    def add_field(self, field: ConfigField, group: Adw.PreferencesGroup) -> None:
        if not field.id:
            return

        widget = self._build_input_widget(field)
        if widget is None:
            return

        group.add(widget)
        
        # Store the actual input widget for value extraction
        input_widget = widget
        if isinstance(widget, Adw.ActionRow):
            # Find the input widget in the suffix
            # This is a bit hacky but Adw.ActionRow doesn't expose suffixes easily
            # We rely on knowing what we built in _build_input_widget
            
            # For multiline (ScrolledWindow -> TextView)
            suffix = widget.get_last_child() # Usually the suffix is last?
            # Actually, let's just store the widget we created and added as suffix
            # We need to return it from _build_input_widget or handle it there.
            pass

        self._bindings[field.id] = FieldBinding(field=field, widget=widget, row=widget)

    # ------------------------------------------------------------------
    # Value collection
    # ------------------------------------------------------------------

    def get_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        for key, binding in self._bindings.items():
            values[key] = self._extract_value(binding.field, binding.widget)
        return values

    def are_required_fields_filled(self) -> bool:
        """Check if all required fields have non-empty values."""
        for binding in self._bindings.values():
            if binding.field.required:
                value = self._extract_value(binding.field, binding.widget)
                # Check if value is empty (empty string, None, or whitespace-only)
                if not value or (isinstance(value, str) and not value.strip()):
                    return False
        return True

    def _notify_change(self, *args) -> None:
        """Trigger the on_change callback when any field changes."""
        if self._on_change_callback:
            self._on_change_callback()

    # ------------------------------------------------------------------
    # Widget builders
    # ------------------------------------------------------------------

    def _build_input_widget(self, field: ConfigField) -> Optional[Gtk.Widget]:
        field_type = (field.type or "text").lower()
        default_text = "" if field.default is None else str(field.default)

        if field_type == "multiline":
            text_view = Gtk.TextView()
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)  # Better wrapping
            text_view.get_buffer().set_text(default_text)
            text_view.set_hexpand(True)
            text_view.set_vexpand(False)
            text_view.add_css_class("card")
            # Connect change signal
            text_view.get_buffer().connect("changed", self._notify_change)
            # Don't set explicit size_request on TextView to avoid measurement warnings
            
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_child(text_view)
            scrolled.set_propagate_natural_height(True)
            scrolled.set_propagate_natural_width(True)  # Allow natural width
            scrolled.set_hexpand(True)
            scrolled.set_max_content_height(300)  # Prevent excessive height
            
            # Use full-width layout for "big" fields
            if getattr(field, 'big', False):
                # Create an expandable row for full-width layout
                row = Adw.ExpanderRow(title=field.label)
                row.set_expanded(True)  # Start expanded
                
                if field.description:
                    row.set_subtitle(field.description)
                
                # Create inner box for the text area
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                box.set_margin_top(6)
                box.set_margin_bottom(12)
                box.set_margin_start(12)
                box.set_margin_end(12)
                box.set_hexpand(True)  # Allow full width
                
                # Configure scrolled window for bigger area (no TextView size_request)
                scrolled.set_min_content_height(150)
                scrolled.set_max_content_height(300)
                
                box.append(scrolled)
                row.add_row(box)
                
                # Monkey-patch to hold reference to text_view for extraction
                row._input_widget = text_view
                return row
            else:
                # Use compact ActionRow layout for smaller fields
                row = Adw.ActionRow(title=field.label)
                if field.description:
                    row.set_subtitle(field.description)
                
                # Use minimum sizes instead of fixed to avoid compositor warnings
                text_view.set_valign(Gtk.Align.CENTER)
                scrolled.set_min_content_height(80)
                scrolled.set_max_content_height(200)
                scrolled.set_min_content_width(300)  # Minimum instead of fixed
                scrolled.set_size_request(300, -1)  # Fallback minimum
                
                row.add_suffix(scrolled)
                
                # Monkey-patch the row to hold reference to text_view for extraction
                row._input_widget = text_view 
                return row

        if field_type == "select" and field.options:
            row = Adw.ActionRow(title=field.label)
            if field.description:
                row.set_subtitle(field.description)
                
            string_list = Gtk.StringList()
            default_index = 0
            for idx, option in enumerate(field.options):
                string_list.append(str(option.label))
                if str(option.value) == default_text:
                    default_index = idx
            
            dropdown = Gtk.DropDown(model=string_list)
            dropdown.set_selected(default_index)
            dropdown.set_valign(Gtk.Align.CENTER)
            dropdown.connect("notify::selected", self._notify_change)
            
            row.add_suffix(dropdown)
            row._input_widget = dropdown
            return row

        if field_type == "info":
            row = Adw.ActionRow(title=field.label)
            if field.description:
                row.set_subtitle(field.description)
            lbl = Gtk.Label(label=default_text)
            row.add_suffix(lbl)
            return row

        # Default: EntryRow
        if field_type == "password":
             if hasattr(Adw, "PasswordEntryRow"):
                row = Adw.PasswordEntryRow(title=field.label)
                row.set_text(default_text)
                row.connect("notify::text", self._notify_change)
             else:
                # Fallback
                row = Adw.ActionRow(title=field.label)
                entry = Gtk.PasswordEntry()
                entry.set_text(default_text)
                entry.set_valign(Gtk.Align.CENTER)
                entry.get_buffer().connect("notify::text", self._notify_change)
                row.add_suffix(entry)
                row._input_widget = entry
                return row
        else:
            row = Adw.EntryRow(title=field.label)
            row.set_text(default_text)
            row.connect("notify::text", self._notify_change)

        if field.description:
            row.set_tooltip_text(field.description)

        return row

    def _extract_value(self, field: ConfigField, widget: Gtk.Widget) -> Any:
        # Handle monkey-patched input widgets (both ActionRow and Box containers)
        if hasattr(widget, "_input_widget"):
            real_widget = widget._input_widget
            if isinstance(real_widget, Gtk.TextView):
                buffer = real_widget.get_buffer()
                return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            if isinstance(real_widget, Gtk.PasswordEntry):
                return real_widget.get_text()
            if isinstance(real_widget, Gtk.DropDown):
                selected = real_widget.get_selected()
                if selected == Gtk.INVALID_LIST_POSITION:
                    return ""
                if field.options and selected < len(field.options):
                    return field.options[selected].value
                return ""

        # Handle Adw.EntryRow / PasswordEntryRow
        if isinstance(widget, Adw.EntryRow):
            return widget.get_text()
        if hasattr(Adw, "PasswordEntryRow") and isinstance(widget, Adw.PasswordEntryRow):
             return widget.get_text()
            
        # Handle Adw.ComboRow
        if isinstance(widget, Adw.ComboRow):
            selected = widget.get_selected()
            if selected == Gtk.INVALID_LIST_POSITION:
                return ""
            if field.options and selected < len(field.options):
                return field.options[selected].value
            return ""

        return "" 
