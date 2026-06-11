import logging
import threading
from pathlib import Path
from typing import Optional

from gi.repository import GLib

from .download_engine import (
    CancelToken,
    DownloadCancelled,
    DownloadEvent,
    download_source_image,
)

logger = logging.getLogger(__name__)


class Downloader:
    def __init__(self, download_dir):
        self.download_dir = Path(download_dir)
        self.is_downloading = False
        self._cancel_token: Optional[CancelToken] = None
        self.progress_callback = None
        self.completion_callback = None
        self.error_callback = None

    def start_download(
        self,
        url,
        save_name,
        on_progress=None,
        on_complete=None,
        on_error=None,
        *,
        acquire_kind=None,
        artifact=None,
    ):
        self._cancel_token = CancelToken()
        self.is_downloading = True
        self.progress_callback = on_progress
        self.completion_callback = on_complete
        self.error_callback = on_error

        threading.Thread(
            target=self._start_download_thread,
            args=(url, save_name, acquire_kind, artifact, self._cancel_token),
            daemon=True,
        ).start()

    def _start_download_thread(self, url, save_name, acquire_kind, artifact, cancel_token):
        try:
            path = download_source_image(
                url=url,
                download_dir=self.download_dir,
                save_name=save_name,
                acquire_kind=acquire_kind,
                artifact=artifact,
                progress_callback=self._emit_progress_event,
                cancel_token=cancel_token,
            )
        except DownloadCancelled:
            logger.info("Download cancelled")
            self.is_downloading = False
            return
        except Exception as exc:
            self.is_downloading = False
            if self.error_callback:
                GLib.idle_add(self.error_callback, str(exc))
            return

        self.is_downloading = False
        if self.completion_callback:
            GLib.idle_add(self.completion_callback, str(path))

    def _emit_progress_event(self, event: DownloadEvent) -> None:
        if self.progress_callback:
            GLib.idle_add(
                self.progress_callback,
                event.progress,
                event.speed_kbps,
                event.state,
            )

    def cancel(self):
        self.is_downloading = False
        if self._cancel_token:
            self._cancel_token.cancel()

    def pause(self):
        self.cancel()
