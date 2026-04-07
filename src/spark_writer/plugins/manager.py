import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from .base import EventEmitter, SparkPlug
from .json_plugin import JsonSparkPlug

logger = logging.getLogger(__name__)

class PluginManager:
    def __init__(self):
        self.plugins: List[SparkPlug] = []
        self.disabled_plugins = set()
        self._event_emitter: Optional[EventEmitter] = None
        self._auto_enable = {
            "Proxmox Tailscale": bool(os.environ.get("SPARK_ENABLE_PROXMOX")),
        }

    def set_event_emitter(self, emitter: Optional[EventEmitter]) -> None:
        """Allow the host to receive plugin events."""

        self._event_emitter = emitter
        for plugin in self.plugins:
            plugin.set_event_emitter(emitter)

    def set_plugin_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self.disabled_plugins.discard(name)
        else:
            self.disabled_plugins.add(name)

    def enable_plugin(self, name: str) -> None:
        self.set_plugin_enabled(name, True)

    def disable_plugin(self, name: str) -> None:
        self.set_plugin_enabled(name, False)

    def get_plugin(self, name: str) -> Optional[SparkPlug]:
        for plugin in self.plugins:
            if plugin.name == name:
                return plugin
        return None

    def is_plugin_enabled(self, plugin: SparkPlug) -> bool:
        return plugin.name not in self.disabled_plugins

    def load_plugins(self, package):
        """Load JSON-based plugins from package and user directory."""
        if isinstance(package, str):
            package_name = package
            try:
                package = importlib.import_module(package)
            except ModuleNotFoundError:
                logger.warning(f"Plugin package not found: {package_name}; skipping")
                return
        else:
            package_name = package.__name__
            
        # Auto-enable plugins from the 'installed' directory
        is_installed = ".installed" in package_name
        
        # Load JSON-based plugins from package
        self._load_json_plugins(package.__path__, is_installed)

        # Load JSON-based plugins from user directory
        data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        user_plugin_dir = os.path.join(data_home, "spark-writer", "plugins")
        if os.path.exists(user_plugin_dir):
             # Treat user plugins as "installed" (auto-enabled)
            self._load_json_plugins([user_plugin_dir], is_installed=True)

    def _prepare_plugin(self, plugin: SparkPlug) -> None:
        if not hasattr(plugin, "_event_emitter"):
            plugin._event_emitter = None  # type: ignore[attr-defined]
        plugin.set_event_emitter(self._event_emitter)

    def _load_json_plugins(self, package_paths: List[str], is_installed: bool) -> None:
        """Load JSON manifest plugins from package directories."""
        for package_path in package_paths:
            path_obj = Path(package_path)
            if not path_obj.exists():
                continue
            
            # Find all .json files (non-recursive for now)
            for json_file in path_obj.glob("*.json"):
                try:
                    plugin = JsonSparkPlug(str(json_file))
                    self._prepare_plugin(plugin)
                    self.plugins.append(plugin)
                    
                    # Apply enable/disable logic: auto-enable if installed or in auto_enable list
                    self.disabled_plugins.add(plugin.name)
                    if self._auto_enable.get(plugin.name) or is_installed:
                        self.disabled_plugins.discard(plugin.name)
                    
                    logger.info(f"Loaded JSON plugin: {plugin.name} from {json_file.name}")
                except Exception as e:
                    logger.error(f"Failed to load JSON plugin {json_file}: {e}")

    def get_all_presets(self) -> Dict[str, Any]:
        presets = {}
        for plugin in self.plugins:
            if self.is_plugin_enabled(plugin):
                presets.update(plugin.register_presets())
        return presets

    def notify_download_start(self, preset):
        for plugin in self.plugins:
            if self.is_plugin_enabled(plugin):
                plugin.on_download_start(preset)

    def process_iso(self, iso_path, preset, ui_values) -> str:
        current_path = iso_path
        for plugin in self.plugins:
            if self.is_plugin_enabled(plugin):
                current_path = plugin.on_iso_ready(current_path, preset, ui_values)
        return current_path

    def handle_uri(self, uri: str, window: Any) -> bool:
        for plugin in self.plugins:
            if self.is_plugin_enabled(plugin):
                if plugin.handle_uri(uri, window):
                    return True
        return False

    def notify_write_complete(self, device_path, preset, ui_values):
        for plugin in self.plugins:
            if self.is_plugin_enabled(plugin):
                plugin.on_write_complete(device_path, preset, ui_values)
