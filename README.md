# SparkGTK

A native GTK4/LibAdwaita frontend for Spark Writer, designed to be modular and lightweight.

## Features

- **Native UI**: Uses GTK4 and LibAdwaita for a modern Linux desktop experience.
- **SparkPlug System**: Modular architecture for extending functionality (JSON SparkPlug manifests).
- **Core Logic**: Bundles `usb_writer_core` — the USB writing and notification library lives inside Spark Writer's own source tree.

## SparkPlug Guide (JSON Manifests)

Python-file SparkPlugs are being deprecated. External SparkPlug authors should create JSON manifests.

### Publisher Quickstart (2 Minutes)

1. Create `my-plugin.json` with:
    - `version: "1.0"`
    - `metadata.id`, `metadata.name`
    - `requires.commands` (empty array if none)
2. Add at least one preset in `presets`.
3. Host the manifest at an HTTPS URL.
4. Install it locally to test:

    ```bash
    spark-writer "spark://plugin/add?manifest=https%3A%2F%2Fexample.com%2Fmy-plugin.json"
    ```

5. Verify it appears, can be selected, and runs through a full write flow.

Current publishing recommendation:

- Publish plain manifests.
- Do not include `secure_manifest` or `signature`; these fields are deprecated and rejected.

This guide covers:

- Required manifest structure
- Available fields and lifecycle hooks
- Installation and trust behavior
- Dependency approvals and command safety
- A working minimal example

### 1. Quick Start

1. Create a JSON file with `version`, `metadata`, and `requires`.
2. Add either `presets` and/or `preset_feeds`.
3. Add optional `config_fields`, `templates`, and `actions`.
4. Host the JSON at an HTTPS URL (or use a local file during development).
5. Install via `spark://plugin/add?manifest=...`.

### 2. Manifest Shape

At minimum:

```json
{
    "version": "1.0",
    "metadata": {
        "id": "example-plugin",
        "name": "Example Plugin"
    },
    "requires": {
        "commands": []
    }
}
```

Required top-level keys:

- `version`: must be `"1.0"`
- `metadata`: must include `id` and `name`
- `requires`: declared runtime dependencies (especially external commands)

Important metadata fields:

- `metadata.id`: lowercase letters, digits, and hyphens only (`^[a-z0-9-]+$`)
- `metadata.name`: display name shown in UI
- `metadata.version`, `author`, `description`, `homepage`: optional but recommended

### 3. Distribution And Trust Rules

When installing from URL (`spark://plugin/add?...`), Spark Writer applies trust checks:

- `file://` and local paths: trusted
- `localhost`: allowed with confirmation
- `http://`: blocked by default
- `https://` trusted hosts (auto-trusted):
    - `github.io`
    - `raw.githubusercontent.com`
    - `gitlab.com`
    - `gist.githubusercontent.com`
- Other `https://` hosts: allowed, but user sees confirmation prompt

Recommended for external distribution:

- Host manifests on HTTPS
- Use a stable URL per release
- Keep plugin ID stable across updates

### 3.1 Manifest Update Model

Spark Writer installs manifests as local snapshots.

Current behavior:

- No automatic remote manifest updates after install.
- Publisher changes are picked up only when the user explicitly reinstalls the updated manifest URL.
- Legacy secure-manifest keys (`secure_manifest`, `signature`) are deprecated and rejected.

### 4. Installation Paths

#### Install from a manifest URL

Run:

```bash
spark-writer "spark://plugin/add?manifest=https%3A%2F%2Fexample.com%2Fmy-plugin.json"
```

Notes:

- `manifest=` and `url=` are both accepted query keys.
- URL-encode the manifest URL.

#### Local development install

Put your manifest in:

```text
~/.local/share/spark-writer/plugins/
```

Then restart Spark Writer. JSON files in that directory are loaded automatically.

### 5. Command Dependencies And Approval Model

Declare external commands under `requires.commands`:

```json
"requires": {
    "commands": [
        {
            "name": "mkpasswd",
            "description": "Generate password hashes",
            "install_hint": "apt install whois",
            "allow_plugin_specific": true
        }
    ]
}
```

Behavior:

- Missing commands block plugin availability.
- Commands marked `allow_plugin_specific: true` are shown for user approval at install time.
- Approved commands are stored in a local approval file (`.<plugin-id>.approval`) next to the installed manifest.
- `run_command` actions can only execute commands that were explicitly approved for that plugin.

### 6. Presets: Static And Remote

#### Static presets (`presets`)

Each preset can provide:

- `id`, `name`, `url`
- optional: `sha256`, `distro`, `metadata`

#### Remote feeds (`preset_feeds`)

- Feed URLs must be HTTPS.
- Feed format is JSON Feed 1.1.
- Presets are read from `items` where `id` starts with `preset:`.
- Static `presets` in your manifest override feed entries with the same ID.

### 6.1 Torrent And Magnet ISO Downloads

Yes, torrent-based ISO downloading still exists and is active.

Supported preset URL formats:

- `magnet:?xt=...`
- `https://.../image.iso.torrent` (or `http://.../image.iso.torrent`)
- direct ISO links: `https://.../image.iso`

Current behavior:

- Downloads are handled by the built-in downloader using `libtorrent` for magnet/torrent and HTTP(S) for direct files.
- In JSON Feed imports, attachment parsing prefers torrent links first, then falls back to direct ISO links.
- The resolved downloaded target prefers `.iso` files when a torrent contains multiple files.

Publisher notes:

- Torrent support depends on `libtorrent` being available in the Spark Writer runtime environment.
- For `.torrent` URLs, keep a clean `.torrent` suffix in the URL path for best compatibility.
- It is still good practice to provide `sha256` when available, even when distributing via torrent.

### 7. UI Configuration Fields

Use `config_fields` to collect user input.

Supported field types:

- `text`
- `password`
- `select`
- `multiline`

Common properties:

- `id`, `label`, `type`
- `required`, `default`, `description`, `placeholder`
- `options` for `select`
- `big` for expanded multiline layout

Template references use `{{field_id}}`.

### 8. Templates

`templates` is a key-value object where each value is a string template.

Example:

```json
"templates": {
    "post_write_note": "Wrote {{preset_name}} to {{device_path}}"
}
```

Templates can be consumed by actions like `render_template` and `write_file`.

### 9. Lifecycle Hooks And Actions

Hooks:

- `actions.on_iso_ready`: runs before write, while processing ISO artifacts
- `actions.on_write_complete`: runs after USB write completes

Stable schema action types:

- `render_template`
- `run_command`
- `write_file`
- `compute_file_hash`
- `create_partition`
- `write_partition_files`
- `generate_receipt`

Useful action fields:

- `id`, `type`
- `when` condition (`not_empty`, `empty`, `equals`, `not_equals`, `in`, `not_in`)
- `emit_event` with `message` and `progress`
- `output_var` to store action results for later actions

Compatibility note:

- Runtime may include additional internal action types.
- External plugins should target the schema-listed action types above for forward compatibility.

### 10. Minimal End-To-End Example

```json
{
    "version": "1.0",
    "metadata": {
        "id": "hello-receipt",
        "name": "Hello Receipt",
        "version": "0.1.0",
        "author": "Example Org",
        "description": "Demonstrates config fields, templates, and post-write actions"
    },
    "requires": {
        "commands": []
    },
    "presets": [
        {
            "id": "ubuntu-24-04",
            "name": "Ubuntu 24.04",
            "url": "https://releases.ubuntu.com/noble/ubuntu-24.04.3-live-server-amd64.iso",
            "distro": "ubuntu"
        }
    ],
    "config_fields": [
        {
            "id": "operator_name",
            "label": "Operator Name",
            "type": "text",
            "required": true,
            "placeholder": "alice"
        }
    ],
    "templates": {
        "write_note": "Preset {{preset_name}} written by {{operator_name}}"
    },
    "actions": {
        "on_write_complete": [
            {
                "id": "render_note",
                "type": "render_template",
                "template": "write_note",
                "output_var": "note_text",
                "emit_event": {
                    "message": "Rendering write note",
                    "progress": 70
                }
            },
            {
                "id": "write_note_file",
                "type": "write_file",
                "path": "/tmp/spark-{{preset_id}}-note.txt",
                "content": "{{note_text}}",
                "permissions": "644",
                "emit_event": {
                    "message": "Writing note file",
                    "progress": 100
                }
            }
        ]
    }
}
```

### 11. Validation Checklist

Before publishing:

1. Validate JSON syntax.
2. Ensure `version` is `1.0`.
3. Ensure `metadata.id` matches `^[a-z0-9-]+$`.
4. Verify every required command exists and has an install hint.
5. Test install from both local file and HTTPS URL.
6. Confirm plugin appears in UI and presets load.
7. Run a full write flow and verify hook behavior.

### 12. Migration Notes (From Python SparkPlugs)

- Move plugin identity to `metadata`.
- Move preset registration logic to `presets`/`preset_feeds`.
- Replace imperative code with declarative `actions` pipelines.
- Declare all external binaries under `requires.commands`.
- Use templates and `output_var` values instead of ad-hoc Python state.

For future compatibility, new external SparkPlugs should be JSON-only.

## Development

1.  Install dependencies:
    ```bash
    pip install -e .
    ```
2.  Run the application:
    ```bash
    spark-writer
    ```
