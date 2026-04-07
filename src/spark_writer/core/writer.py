import threading
import logging
from pathlib import Path
from gi.repository import GLib

try:
    from usb_writer_core import writer
except ImportError:
    raise ImportError(
        "usb_writer_core not found. Run: pip install -e apps/spark-writer"
    )

logger = logging.getLogger(__name__)

class USBWriter:
    def __init__(self):
        self.is_writing = False
        self.progress_callback = None
        self.completion_callback = None
        self.error_callback = None

    def write_iso(self, iso_path, device_path, on_progress=None, on_complete=None, on_error=None):
        self.progress_callback = on_progress
        self.completion_callback = on_complete
        self.error_callback = on_error
        self.is_writing = True

        thread = threading.Thread(target=self._write_task, args=(iso_path, device_path))
        thread.daemon = True
        thread.start()

    def list_drives(self):
        return writer.list_removable_drives()

    def _emit_progress(self, progress, rate, state):
        if self.progress_callback:
            GLib.idle_add(self.progress_callback, progress, rate, state)

    def _emit_error(self, msg):
        if self.error_callback:
            GLib.idle_add(self.error_callback, msg)

    def _emit_complete(self):
        if self.completion_callback:
            GLib.idle_add(self.completion_callback)

    def _write_task(self, iso_path, device_path):
        try:
            self._emit_progress(0, 0, "Wiping device...")
            
            writer.wipe_device(device_path)
            
            self._emit_progress(0, 0, "Writing ISO...")

            def progress_handler(written, total):
                percent = (written / total) * 100
                self._emit_progress(percent, 0, f"Writing: {int(percent)}%")

            writer.write_iso_to_device(Path(iso_path), device_path, progress_callback=progress_handler)
            
            self._emit_progress(100, 0, "Complete")
            
            self._emit_complete()

        except Exception as e:
            logger.error(f"Write failed: {e}")
            self._emit_error(str(e))
        finally:
            self.is_writing = False
