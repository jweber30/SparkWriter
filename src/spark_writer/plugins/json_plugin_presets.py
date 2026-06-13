"""Preset loading helpers for JSON SparkPlug manifests."""

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class JsonPluginPresetMixin:
    """Preset registration and JSON Feed parsing."""

    manifest: dict[str, Any]

    def _manifest_outputs(self) -> Dict[str, bool]:
        outputs = self.manifest.get('outputs', {})
        if not isinstance(outputs, dict):
            outputs = {}
        return {
            'usb': bool(outputs.get('usb', True)),
            'iso': bool(outputs.get('iso', True)),
        }

    def _plugin_id_for_source(self) -> str:
        metadata = self.manifest.get('metadata', {})
        return str(metadata.get('id') or metadata.get('name') or '').strip()

    def _plugin_source_metadata(self) -> Dict[str, str]:
        metadata = self.manifest.get('metadata', {})
        if not isinstance(metadata, dict):
            return {}
        return {
            'sparkplug_name': str(metadata.get('name') or '').strip(),
            'manifest_origin': str(metadata.get('installed_from') or '').strip(),
        }

    def register_sources(self) -> list[Dict[str, Any]]:
        """Return manifest-owned installation Sources.

        New manifests should define one top-level ``source``. Legacy manifests
        that still declare ``presets`` are normalized into Source-shaped records
        so older installs continue to appear in the UI.
        """
        sources: list[Dict[str, Any]] = []
        outputs = self._manifest_outputs()
        owner_id = self._plugin_id_for_source()
        source_metadata = self._plugin_source_metadata()

        source = self.manifest.get('source')
        if isinstance(source, dict) and source.get('id'):
            normalized = dict(source)
            normalized.setdefault('sparkplug_id', owner_id)
            normalized.update({key: value for key, value in source_metadata.items() if value})
            normalized.setdefault('outputs', outputs)
            sources.append(normalized)
            return sources

        for preset_id, preset in self.register_presets().items():
            normalized = {
                'id': preset_id,
                'name': preset.get('name', preset_id),
                'url': preset.get('url', ''),
                'family': preset.get('family') or preset.get('distro', ''),
                'sha256': preset.get('sha256', ''),
                'installer_scheme': preset.get('installer_scheme', ''),
                'capabilities': preset.get('capabilities', []),
                'sparkplug_id': owner_id,
                'outputs': outputs,
                **{key: value for key, value in source_metadata.items() if value},
            }
            if preset.get('version'):
                normalized['version'] = preset['version']
            if preset.get('acquire_kind'):
                normalized['acquire'] = {
                    'kind': preset['acquire_kind'],
                    'url': normalized['url'],
                }
            sources.append(normalized)

        return sources

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
                    'family': preset.get('family', preset.get('distro', '')),
                    'version': preset.get('version', ''),
                    **preset.get('metadata', {})
                }
        source = self.manifest.get('source')
        if isinstance(source, dict):
            source_id = source.get('id')
            if source_id:
                presets[source_id] = {
                    'name': source.get('name', ''),
                    'url': source.get('url', ''),
                    'sha256': source.get('sha256', ''),
                    'distro': source.get('family', source.get('distro', '')),
                    'family': source.get('family', source.get('distro', '')),
                    'version': source.get('version', ''),
                    'installer_scheme': source.get('installer_scheme', ''),
                    'capabilities': source.get('capabilities', []),
                    'source_id': source_id,
                    'source_name': source.get('name', ''),
                    'source_family': source.get('family', source.get('distro', '')),
                    'source_url': source.get('url', ''),
                    **source.get('metadata', {}),
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
