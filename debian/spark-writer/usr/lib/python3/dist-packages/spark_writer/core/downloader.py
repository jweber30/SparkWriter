import libtorrent as lt
import logging
import os
import threading
import time
from typing import Optional

from gi.repository import GLib

logger = logging.getLogger(__name__)

class Downloader:
    def __init__(self, download_dir):
        self.download_dir = download_dir
        self.session = lt.session()
        self.session.listen_on(6881, 6891)
        self.handle = None
        self.is_downloading = False
        self.progress_callback = None
        self.completion_callback = None
        self.error_callback = None

    def start_download(self, url, save_name, on_progress=None, on_complete=None, on_error=None):
        self.progress_callback = on_progress
        self.completion_callback = on_complete
        self.error_callback = on_error
        
        if not os.path.exists(self.download_dir):
            os.makedirs(self.download_dir)

        # Run setup in a thread to avoid blocking UI
        threading.Thread(
            target=self._start_download_thread,
            args=(url, save_name),
            daemon=True,
        ).start()

    def _start_download_thread(self, url, save_name):
        params = {
            'save_path': self.download_dir,
            'storage_mode': lt.storage_mode_t(2),
        }
        
        try:
            if url.startswith("magnet:"):
                self.handle = lt.add_magnet_uri(self.session, url, params)
            elif url.startswith("http") and url.endswith(".torrent"):
                import requests
                # Set a timeout to prevent hanging indefinitely
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                info = lt.torrent_info(response.content)
                self.handle = self.session.add_torrent({'ti': info, 'save_path': self.download_dir})
            elif url.startswith("http") or url.startswith("https"):
                # Direct HTTP download
                self._download_http(url, save_name)
                return
            else:
                raise ValueError("Could not parse download URL: {}".format(url))

            self.is_downloading = True
            self._monitor_download(save_name)
            
        except Exception as e:
            if self.error_callback:
                # Ensure callback runs on main thread if needed, but usually callbacks handle GLib.idle_add
                self.error_callback(str(e))

    def _download_http(self, url, save_name):
        import requests
        try:
            local_filename = os.path.join(self.download_dir, save_name + ".iso")  # Simple naming
            
            with requests.get(url, stream=True, timeout=10) as r:
                r.raise_for_status()
                total_length = int(r.headers.get('content-length', 0))
                dl = 0
                with open(local_filename, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk: 
                            dl += len(chunk)
                            f.write(chunk)
                            if total_length and self.progress_callback:
                                GLib.idle_add(self.progress_callback, (dl / total_length) * 100, 0, "Downloading")
            
            logger.info("HTTP download complete: %s", local_filename)
            if self.completion_callback:
                GLib.idle_add(self.completion_callback, local_filename)
        except Exception as e:
            if self.error_callback:
                GLib.idle_add(self.error_callback, str(e))

    def _monitor_download(self, save_name):
        while self.is_downloading:
            s = self.handle.status()
            
            progress = s.progress * 100
            state = s.state
            download_rate = s.download_rate / 1000
            
            if self.progress_callback:
                GLib.idle_add(self.progress_callback, progress, download_rate, str(state))

            if s.is_seeding:
                self.is_downloading = False
                # For simplicity, we'll just return the path to the first file.
                torrent_info = self.handle.torrent_file()
                file_path = None
                if torrent_info:
                    file_path = self._resolve_completed_target(torrent_info)

                if file_path and os.path.exists(file_path):
                    logger.info("Torrent download complete: %s", file_path)
                    if self.completion_callback:
                        GLib.idle_add(self.completion_callback, file_path)
                else:
                    logger.error("Unable to resolve downloaded ISO path")
                    if self.error_callback:
                        GLib.idle_add(self.error_callback, "Downloaded files missing or incomplete")
                break
            
            time.sleep(1)

    def cancel(self):
        self.is_downloading = False
        if self.handle:
            self.session.remove_torrent(self.handle)

    def _resolve_completed_target(self, torrent_info: lt.torrent_info) -> Optional[str]:
        storage = torrent_info.files()
        candidates = []
        for idx in range(storage.num_files()):
            rel_path = storage.file_path(idx)
            abs_path = os.path.abspath(os.path.join(self.download_dir, rel_path))
            if os.path.isdir(abs_path):
                continue
            candidates.append(abs_path)

        iso_candidates = [path for path in candidates if path.lower().endswith(".iso")]
        if iso_candidates:
            return iso_candidates[0]

        if candidates:
            return candidates[0]

        # Final fallback: scan download_dir for ISO files
        for root, _dirs, files in os.walk(self.download_dir):
            for filename in files:
                if filename.lower().endswith(".iso"):
                    return os.path.abspath(os.path.join(root, filename))

        return None
