"""Preset loading helpers for JSON SparkPlug manifests."""

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class JsonPluginPresetMixin:
    """Preset registration and JSON Feed parsing."""

    manifest: dict[str, Any]

    def register_presets(self) -> Dict[str, Any]:
        """Return presets defined in manifest, including those from remote feeds."""
        presets = {}

        # Load presets from remote feeds first.
        for feed_spec in self.manifest.get('preset_feeds', []):
            feed_url = feed_spec.get('url', '')
            if not feed_url.startswith('https://'):
                logger.warning(f"Skipping non-HTTPS feed: {feed_url}")
                continue

            try:
                feed_presets = self._fetch_preset_feed(feed_url)
                presets.update(feed_presets)
                logger.info(f"Loaded {len(feed_presets)} presets from {feed_url}")
            except Exception as e:
                logger.error(f"Failed to fetch preset feed {feed_url}: {e}")

        # Static presets override feed presets.
        for preset in self.manifest.get('presets', []):
            preset_id = preset.get('id')
            if preset_id:
                presets[preset_id] = {
                    'name': preset.get('name', ''),
                    'url': preset.get('url', ''),
                    'sha256': preset.get('sha256', ''),
                    'distro': preset.get('distro', ''),
                    **preset.get('metadata', {})
                }
        return presets

    def _fetch_preset_feed(self, feed_url: str) -> Dict[str, Any]:
        """Fetch and parse a JSON Feed 1.1 preset feed."""
        try:
            import requests
        except ImportError:
            logger.warning("requests library not available, skipping feed fetch")
            return {}

        try:
            response = requests.get(feed_url, timeout=10)
            response.raise_for_status()
            feed = response.json()

            if feed.get('version') != 'https://jsonfeed.org/version/1.1':
                logger.warning(f"Unknown feed version: {feed.get('version')}")

            presets = {}
            for item in feed.get('items', []):
                item_id = item.get('id', '')
                if not item_id.startswith('preset:'):
                    continue
                preset_id = item_id.replace('preset:', '', 1)

                url = ''
                sha256 = ''
                for attachment in item.get('attachments', []):
                    mime = attachment.get('mime_type', '')
                    title = attachment.get('title', '')

                    # Prefer torrent, fallback to direct ISO.
                    if 'torrent' in mime or 'torrent' in title.lower():
                        url = attachment.get('url', '')
                    elif not url and ('iso' in title.lower() or 'octet-stream' in mime):
                        url = attachment.get('url', '')

                    if 'sha256' in attachment:
                        sha256 = attachment['sha256']

                if not url:
                    logger.warning(f"No download URL found for preset {preset_id}")
                    continue

                distro = ''
                tags = item.get('tags', [])
                distro_tags = ['ubuntu', 'debian', 'proxmox', 'fedora', 'arch']
                for tag in tags:
                    if tag.lower() in distro_tags:
                        distro = tag.lower()
                        break

                presets[preset_id] = {
                    'name': item.get('title', preset_id),
                    'url': url,
                    'sha256': sha256,
                    'distro': distro,
                    'description': item.get('summary', ''),
                }

            return presets

        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to fetch feed: {e}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in feed: {e}")
