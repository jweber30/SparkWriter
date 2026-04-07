from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from usb_writer_core.notifications import PipelineStage
else:  # pragma: no cover - optional at runtime
    PipelineStage = Any  # type: ignore


@dataclass
class ConfigOption:
    """Selectable option for a configuration field."""

    value: Any
    label: str


@dataclass
class ConfigField:
    """Declarative schema describing a plugin configuration field."""

    id: str
    label: str
    type: str
    default: Any = ""
    required: bool = False
    description: Optional[str] = None
    placeholder: Optional[str] = None
    options: List[ConfigOption] = field(default_factory=list)
    big: bool = False  # For multiline fields: use full-width layout


class PluginEventType(str, Enum):
    """Lifecycle notifications emitted by plugins."""

    START = "start"
    UPDATE = "update"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class PluginEvent:
    """Structured event that hosts can surface to users."""

    message: str
    stage: Optional[Union["PipelineStage", str]] = None
    progress: Optional[int] = None
    event_type: PluginEventType = PluginEventType.UPDATE
    metadata: Dict[str, Any] = field(default_factory=dict)


EventEmitter = Callable[[PluginEvent], None]

class SparkPlug(ABC):
    """Base class for SparkGTK plugins."""

    def __init__(self) -> None:
        # Sub-classes can emit structured events without knowing the host.
        self._event_emitter: Optional[EventEmitter] = None

    @property
    def is_available(self) -> bool:
        """Return False if runtime requirements are missing."""

        return True

    @property
    def unavailable_reason(self) -> Optional[str]:
        """Optional human-readable reason when unavailable."""

        return None

    def requires_processing(self) -> bool:
        """Return True if this plugin requires ISO processing (PROCESS stage).
        
        Override this to return True if your plugin's on_iso_ready() method
        performs actual work (e.g., modifying the ISO, injecting files).
        If False, the PROCESS stage will be skipped for this plugin.
        """
        return False

    def supports_save_iso(self) -> bool:
        """Return True if this plugin's functionality can be saved as an ISO file.
        
        Return False if the plugin requires USB-device-specific operations
        that cannot be captured in an ISO (e.g., creating auxiliary partitions
        like CIDATA after the ISO is written).
        
        If True, the plugin's on_iso_ready() processing will be included when
        saving an ISO. If False, the "Save ISO" button will save the original
        unmodified ISO, or be disabled if the plugin requires processing.
        
        Default: True if requires_processing(), False otherwise.
        This means plugins that modify the ISO can be saved, but plugins that
        only do post-write operations cannot.
        """
        return self.requires_processing()

    # ------------------------------------------------------------------
    # Core identification & configuration
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name."""
        pass

    def register_presets(self) -> Dict[str, Any]:
        """
        Return a dictionary of OS presets.
        Format: {'id': {'name': 'Display Name', 'url': 'magnet/http url', ...}}
        """
        return {}

    def get_config_fields(self) -> List[Union[ConfigField, Dict[str, Any]]]:
        """
        Return a list of configuration fields to generate UI for.
        Format:
        [
            {
                'id': 'auth_key',
                'label': 'Auth Key',
                'type': 'text' | 'password' | 'select',
                'default': '',
                'options': [] # for select
            }
        ]
        """
        return []

    def get_config_schema(self) -> List[ConfigField]:
        """Return config fields normalized to ConfigField objects."""

        normalized: List[ConfigField] = []
        for field in self.get_config_fields():
            normalized.append(self._coerce_config_field(field))
        return normalized

    @staticmethod
    def _coerce_config_field(field: Union[ConfigField, Dict[str, Any]]) -> ConfigField:
        if isinstance(field, ConfigField):
            return field

        options: List[ConfigOption] = []
        for option in field.get("options", []):
            if isinstance(option, ConfigOption):
                options.append(option)
            elif isinstance(option, dict):
                options.append(
                    ConfigOption(
                        value=option.get("value"),
                        label=option.get("label", str(option.get("value"))),
                    )
                )
            else:
                options.append(ConfigOption(value=option, label=str(option)))

        return ConfigField(
            id=field.get("id") or field.get("key") or "",
            label=field.get("label", ""),
            type=field.get("type", "text"),
            default=field.get("default", ""),
            required=field.get("required", False),
            description=field.get("description"),
            placeholder=field.get("placeholder"),
            options=options,
            big=field.get("big", False),
        )

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def set_event_emitter(self, emitter: Optional[EventEmitter]) -> None:
        """Set the callback used to surface plugin events to the host UI."""

        self._event_emitter = emitter

    def emit_event(
        self,
        *,
        stage: Optional[Union["PipelineStage", str]] = None,
        message: str,
        progress: Optional[int] = None,
        event_type: PluginEventType = PluginEventType.UPDATE,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Helper for subclasses to publish lifecycle events."""

        if self._event_emitter is None:
            return

        event = PluginEvent(
            stage=stage,
            message=message,
            progress=progress,
            event_type=event_type,
            metadata=metadata or {},
        )
        self._event_emitter(event)

    def on_download_start(self, preset: Dict[str, Any]) -> None:
        """Called before download starts."""
        pass

    def on_iso_ready(self, iso_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]) -> str:
        """
        Called when ISO is ready for processing.
        Return path to processed ISO (or original path if no changes).
        """
        return iso_path

    def handle_uri(self, uri: str, window: Any) -> bool:
        """
        Handle a spark:// URI.
        Return True if handled.
        """
        return False

    def on_write_complete(self, device_path: str, preset: Dict[str, Any], ui_values: Dict[str, Any]) -> None:
        """Called after USB write is complete."""
        pass

    def should_show_ui(self, preset_id: str, preset_data: Dict[str, Any]) -> bool:
        """
        Return True if the plugin UI should be shown for the selected preset.
        """
        return True
