# SparkWriter

A native GTK4/LibAdwaita application designed to be modular and lightweight.

## Document Status And Provenance

This README is the practical usage and development guide.

- Treat `docs/SPARKWRITER_MANIFEST_AUTHORING.md` as the normative authoring guide for the SparkWriter manifest format.
- Treat the JSON schema and current test suite as the strongest implementation contract.
- Treat policy sections here as implementation notes unless they are explicitly marked stable.

If a README statement conflicts with code/tests, prefer code/tests and open a docs follow-up.

## What Is SparkWriter?

SparkWriter is a **modular USB provisioning tool** for bare-metal infrastructure. It:

1. Loads JSON SparkWriter manifests that define presets (ISOs) and automation workflows
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

## Manifest Overview

SparkWriter manifests are JSON files that extend SparkWriter with presets, forms, and lifecycle actions.

External authors should use `docs/SPARKWRITER_MANIFEST_AUTHORING.md` for the current manifest and runtime contract.

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

- Publish over HTTPS.
- Keep `metadata.id` stable across releases.
- If you host on GitHub, sign the manifest as described in `docs/SPARKWRITER_MANIFEST_AUTHORING.md`.
- Test both local-file install and URL install before sharing a link.

### Manifest Authoring Reference

The detailed SparkWriter manifest specification now lives in `docs/SPARKWRITER_MANIFEST_AUTHORING.md`.

That document covers:

- manifest shape and required fields
- trust, GitHub signing, and install-time behavior
- presets, remote feeds, and torrent support
- config fields, visibility rules, and template behavior
- lifecycle phases, action semantics, approvals, artifacts, and retired actions

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

Share SparkWriter manifests with users as direct links. They don't need to copy-paste, just click:

```html
<!-- Example: embed in a website -->
<a href="spark://plugin/add?manifest=https://example.com/my-plugin.json">
    Install My SparkWriter Manifest
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

## Installation Paths

#### Install from a manifest URL

Run:

```bash
spark-writer "spark://plugin/add?manifest=https%3A%2F%2Fexample.com%2Fmy-plugin.json"
```

Notes:

- `manifest=` and `url=` are both accepted query keys.
- URL-encode the manifest URL.
- URL installs are snapshot installs. Reinstall explicitly to pick up publisher changes.

#### Local development install

Put your manifest in:

```text
~/.local/share/spark-writer/plugins/
```

Then restart Spark Writer. JSON files in that directory are loaded automatically.

## Command Dependencies And Approval Model

Declare external commands under `requires.commands`.

Current behavior:

- Missing commands block plugin availability.
- Commands with `allow_plugin_specific: true` are disclosed during install.
- Actual approval is enforced at runtime per lifecycle phase.
- Approved commands are persisted under `XDG_STATE_HOME/spark-writer/approvals/` or `~/.local/state/spark-writer/approvals/` when `XDG_STATE_HOME` is unset.
- Legacy approval files next to the manifest are ignored unless they use the current `invocation-v2` approval model.

For exact approval file shape and phase behavior, use `docs/SPARKWRITER_MANIFEST_AUTHORING.md`.

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
# ~/.local/state/spark-writer/approvals/.my-plugin-id.approval
{
    "plugin_id": "my-plugin-id",
    "approval_model": "invocation-v2",
    "approved_commands": ["mkpasswd"]
}
```

Approval files stored next to the manifest are only honored when they use the current `invocation-v2` model.

### Running Tests

```bash
pytest tests/
```

Key test modules:

- `test_manifest_integration.py`: End-to-end JSON plugin loading and action execution
- `test_manifest_approval.py`: Command approval flow and security policies
- `test_writer_subprocess.py`: USB write subprocess and Crostini integration
- `test_notifications.py`: Desktop notifications and PipelineNotifier events
