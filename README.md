# SparkWriter

A native GTK4/LibAdwaita application designed to be modular and lightweight.

## Document Status And Provenance

This README is the practical usage guide and may include behavior that is still evolving.

- Treat the JSON schema and current test suite as the strongest current contract.
- Treat policy sections here as current implementation notes unless they are explicitly marked stable.
- Track evolving design decisions and rationale in `docs/DECISIONS.md`.

If a README statement conflicts with code/tests, prefer code/tests and open a docs follow-up.

## Features


## What Is SparkWriter?

SparkWriter is a **modular USB provisioning tool** for bare-metal infrastructure. It:

1. Loads JSON "SparkPlug" manifests that define presets (ISOs) and automation workflows
2. Generates dynamic forms based on manifest `config_fields` to collect user input
3. Renders templates using the collected data
4. Executes lifecycle actions (before/after write)
5. Writes ISOs to removable devices using `dd` (with Crostini support)

**In one sentence:** A fancy wrapper around `dd` that lets you define provisioning workflows in JSON.

**Use cases:**

- Bare-metal cluster provisioning (define via manifest, share via URL)
- Cloud-init image delivery with custom configs
- Automated hardware imaging pipelines
- Chrome OS Crostini-friendly USB creation

## SparkPlug Guide (JSON Manifests)

SparkPlugs are installed as JSON manifests distributed via HTTPS URLs. External authors should follow the manifest specification below.

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


## URI Handler & Chromebook Integration

SparkWriter registers as the handler for `spark://` URIs at the system level.

### How It Works

When a user clicks a `spark://plugin/add?manifest=...` link in their browser:

1. The browser recognizes the `spark://` scheme (registered by the `.desktop` file)
2. The system launches SparkWriter with the full URI as an argument
3. SparkWriter parses the URI, evaluates trust, and prompts the user before downloading
4. The manifest is downloaded, validated, and installed locally

This works on:
- Desktop Linux (GTK4 environments)
- Chrome OS (Crostini Linux container with proper Wayland/X11 support)
- Any distro with FreeDesktop.org application support

### For Users: Clickable Links

Share plugin manifests with users as direct links. They don't need to copy-paste—just click:

```html
<!-- Example: embed in a website -->
<a href="spark://plugin/add?manifest=https://example.com/my-plugin.json">
    Install My SparkWriter Plugin
</a>
```

### For Developers: Adding URI Handlers

The URI handler is registered via the `.desktop` file:

```ini
# spark-writer.desktop
MimeType=x-scheme-handler/spark;
Exec=/usr/bin/spark-writer %u
```

The `%u` placeholder receives the full URI. SparkWriter's `app.py` intercepts this in `do_command_line()`, parses the manifest query parameter, and routes to `handle_uri()` which implements trust evaluation before download.

See the Installation Paths section below for where downloaded manifests are stored.

This guide covers:


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

## Development

1.  Install dependencies:
    ```bash
    pip install -e .
    ```
2.  Run the application:
    ```bash
    spark-writer
    ```

### Project Architecture

**Layers:**

| Layer | Component | Purpose |
|-------|-----------|---------|
| UI | `src/spark_writer/` (GTK4/Adwaita) | Plugin discovery, form generation, download progress, device selection |
| Plugin Runtime | `src/spark_writer/plugins/` | `JsonSparkPlug` execution, template rendering, action lifecycle |
| Core Writers | `src/usb_writer_core/` | Low-level disk operations, Crostini support, notifications, session receipts |
| CLI/IPC | System `.desktop` file | URI handler registration, app activation, CLI argument parsing |

**Data Flow:**

```
User clicks spark://plugin/add?manifest=URL
    ↓
.desktop file + app.py do_command_line()
    ↓
handle_uri() → evaluate_trust() → show confirmation
    ↓
Download manifest JSON
    ↓
JsonSparkPlug loads manifest
    ↓
ConfigFormBuilder renders config_fields as Adwaita widgets
    ↓
User fills form + selects preset/device
    ↓
on_iso_ready actions (if any) + on_write_complete actions
    ↓
usb_writer_core.writer uses dd, wipefs, mount via subprocess
```

### System Dependencies

SparkWriter requires system packages (not pip):

- `libgtk-4-1` (GTK4 runtime)
- `libadwaita-1-0` (Adwaita widgets)
- `libtorrent-rasterbar-dev` (torrent/magnet downloads)
- `util-linux` (lsblk, wipefs, mount)
- `python3-gi` (PyGObject bindings)

On Ubuntu 24.04:

```bash
sudo apt install libgtk-4-1 libadwaita-1-0 libtorrent-rasterbar-dev \
    util-linux python3-gi python3-gi-cairo python3-dev
```

Then install SparkWriter in development mode:

```bash
pip install -e .
```

### Testing URI Handlers Locally

1. Install the package: `pip install -e .`
2. Register the `.desktop` file manually (normally done by .deb):
     ```bash
     mkdir -p ~/.local/share/applications
     cp spark-writer.desktop ~/.local/share/applications/net.metalstrapper.SparkGTK.desktop
     update-desktop-database ~/.local/share/applications
     ```
3. Test by running:
     ```bash
     spark-writer "spark://plugin/add?manifest=file:///path/to/my-plugin.json"
     ```
4. For browser integration, the `.desktop` file must be in `/usr/share/applications/` (installed via .deb)

### Testing Plugins Locally

Drop a JSON manifest into `~/.local/share/spark-writer/plugins/` and restart the app:

```bash
mkdir -p ~/.local/share/spark-writer/plugins/
cp my-plugin.json ~/.local/share/spark-writer/plugins/
spark-writer
```

Or use the manifest approval file to pre-approve commands:

```bash
# ~/.local/share/spark-writer/plugins/.my-plugin-id.approval
{
    "commands": ["mkpasswd"]
}
```

### Running Tests

```bash
pytest tests/
```

Key test modules:

- `test_manifest_integration.py`: End-to-end JSON plugin loading and action execution
- `test_manifest_approval.py`: Command approval flow and security policies
- `test_writer_subprocess.py`: USB write subprocess and Crostini integration
- `test_notifications.py`: Desktop notifications and PipelineNotifier events
