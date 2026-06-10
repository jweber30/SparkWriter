# SparkWriter Manifest Authoring

This is the concise author guide for the SparkWriter manifest format.

If this document and the runtime disagree, trust the code, schema, and tests:

- `src/spark_writer/plugins/schema/sparkplug_manifest.schema.json`
- `src/spark_writer/plugins/json_plugin.py`
- `src/spark_writer/plugins/forms.py`
- `src/spark_writer/window.py`
- `tests/test_sources.py`
- `tests/test_manifest_integration.py`
- `tests/test_manifest_approval.py`

## What A Manifest Does

A SparkWriter manifest defines an installable workflow that can:

- declare its installation Source/media flavor
- expose configuration fields in the UI
- arrange those fields into wizard pages
- render templates with the collected values
- run actions during ISO processing or after USB write

Installed manifests are SparkWriter's Source catalog. When a manifest declares
`source`, SparkWriter shows that manifest-owned Source and opens the matching
flavor-specific form.

## Minimal Manifest

```json
{
  "version": "1.0",
  "metadata": {
    "id": "example-manifest",
    "name": "Example Manifest"
  },
  "requires": {
    "commands": []
  }
}
```

Required rules:

- `version` must be `"1.0"`
- `metadata.id` must match `^[a-z0-9-]+$`
- `metadata.name` is the display name
- `requires.commands` may be empty, but `requires` must exist

Useful optional metadata:

- `metadata.version`
- `metadata.author`
- `metadata.description`
- `metadata.homepage`
- `metadata.github_username`
- `metadata.signature`

Rejected legacy keys:

- top-level `secure_manifest`
- top-level `signature`

## Installation Model

SparkWriter currently supports two install paths:

1. URL install with `spark://plugin/add?manifest=...`
2. Local development install by dropping a `.json` file into `~/.local/share/spark-writer/plugins/`

URL installs are snapshot installs. Reinstall to pick up remote changes.

## Source And Outputs

New manifests should define one top-level `source`. This keeps flavor-specific
media data beside the form, templates, and actions that know how to use it.

```json
{
  "source": {
    "id": "ubuntu-24.04-server-autoinstall",
    "name": "Ubuntu 24.04 LTS Server Autoinstall",
    "family": "ubuntu",
    "version": "24.04",
    "url": "https://example.com/ubuntu.iso",
    "installer_scheme": "ubuntu-nocloud",
    "capabilities": ["cloud-init-nocloud"]
  },
  "outputs": {
    "usb": true,
    "iso": true
  }
}
```

`outputs.usb` controls whether the workflow can be written to a USB device.
`outputs.iso` controls whether SparkWriter offers Save ISO for the workflow.

## Legacy Presets And Remote Feeds

Manifests may still define install media through legacy fields:

- `presets`
- `preset_feeds`

Current support includes:

- direct ISO URLs
- `.torrent` URLs
- magnet links
- JSON Feed 1.1 preset feeds over HTTPS

Static presets override feed-provided presets with the same ID.

## Config Fields

Use `config_fields` to describe the values SparkWriter should collect.

Supported field types:

- `text`
- `password`
- `select`
- `multiline`
- `info`

Common field properties:

- `id`
- `label`
- `type`
- `default`
- `required`
- `description`
- `placeholder`
- `options` for `select`
- `big` for wide `multiline` layout
- `standard_field`
- `storage`

Current behavior:

- required fields must be non-empty before the user can continue
- `select` options use `{ "value": ..., "label": ... }`
- `info` shows non-editable text using `default`
- `description` is shown as helper text or tooltip, depending on widget type
- `standard_field` and `storage` are meaningful to the host UI for profile save/fill behavior

## Wizard Pages

`wizard.pages` lets a manifest split its `config_fields` into multiple steps.

Each page has:

- `id`
- `title`
- optional `description`
- `fields`: ordered field IDs from `config_fields`

Example:

```json
{
  "config_fields": [
    { "id": "hostname", "label": "Hostname", "type": "text", "required": true },
    { "id": "email", "label": "Email", "type": "text" },
    { "id": "ssh_keys", "label": "SSH Keys", "type": "multiline", "big": true }
  ],
  "wizard": {
    "pages": [
      {
        "id": "identity",
        "title": "Identity",
        "description": "Basic machine settings.",
        "fields": ["hostname", "email"]
      },
      {
        "id": "access",
        "title": "Access",
        "fields": ["ssh_keys"]
      }
    ]
  }
}
```

Validation rules enforced at load time:

- `wizard` must be an object
- `wizard.pages` must be an array
- each page must be an object with a unique `id`
- each listed field must exist in `config_fields`
- a field may appear on at most one wizard page

Current host behavior:

- pages are shown in manifest order
- a page's fields are shown in the listed order
- the Continue button stays disabled until required fields on that page are filled
- the last page button changes to Review
- any `config_fields` not listed in `wizard.pages` are automatically collected into a final fallback page named `<Manifest Name> Configuration`
- if a manifest has fields but no wizard pages, SparkWriter still creates a single fallback configuration page

### Profile Save And Auto-Fill

SparkWriter can offer to reuse saved profile values on a page-by-page basis.

To participate, a field needs:

- a recognized `standard_field`
- `storage.scope` set to `profile`, or `storage.persist` set to `true`

Known `standard_field` values:

- `user.name`
- `user.email`
- `user.ssh_public_keys`
- `network.hostname`
- `network.wifi.ssid`
- `network.wifi.password`
- `locale.timezone`
- `locale.keyboard`

Current behavior:

- when a wizard page opens, SparkWriter may prompt to fill empty fields from the saved profile
- values are matched by `standard_field`, not by the manifest field ID
- SparkWriter saves non-empty values when the user advances through wizard pages and when starting a flash/save flow

## Visibility Rules

Use `ui_visibility.when` to control when a manifest is offered for the current Source.

Supported selectors:

- `source_family`
- `source_id`
- `installer_scheme`
- `source_capabilities`
- `preset_distro`
- `preset_id`

The source-based selectors are the most important ones for current SparkWriter flows.

## Templates

`templates` is a mapping from template name to one of:

- inline string
- array of lines
- `{ "file": "relative/path" }`
- `{ "asset": "asset-name" }`

Template rendering uses SparkWriter's dependency-free renderer.

That means:

- variable substitution works with `{{field_id}}`
- truthy conditionals work with `{% if field_id %}`, optional `{% else %}`, and `{% endif %}`
- undefined values fail fast
- template sidecars must stay relative to the manifest and may not escape its directory

Current template context includes:

- config field defaults and collected UI values
- action outputs from the current phase
- `iso_path`
- `device_path`
- `preset_id`
- `preset_name`

Hyphenated field IDs can be referenced directly, such as `{{root-password}}`, and are also available through underscore aliases such as `{{root_password}}`.

## Actions

Actions live under:

- `actions.on_iso_ready`
- `actions.on_write_complete`

Currently supported action types:

- `render_template`
- `run_command`
- `compute_file_hash`
- `create_partition`
- `write_partition_files`
- `generate_receipt`
- `format_yaml_list`
- `generate_ephemeral_password`
- `store_ephemeral_secret`
- `show_ephemeral_secret_button`
- `create_artifact`
- `prepare_installer_iso`

Notes:

- `on_iso_ready` runs during ISO processing
- `on_write_complete` runs after the USB write finishes
- `when` conditions can skip an action
- `output_var` stores an action result for later actions in the same phase
- plugin-specific external commands must be declared in `requires.commands`
- `prepare_installer_iso` uses `installer_scheme` plus generic `artifact_map` and `options`; scheme handlers interpret role names such as `user-data`, `meta-data`, `answer-file`, or `first-boot`

For exact per-action fields, use the schema and runtime as the source of truth.

## Trust And Distribution

Current install-time trust behavior:

- local paths and `file://` manifests are trusted
- `localhost` manifests are allowed with confirmation
- plain `http://` manifests are blocked by default
- non-GitHub `https://` manifests require confirmation
- GitHub-hosted manifests require identity and signature verification

GitHub-hosted currently includes:

- `github.com`
- `raw.githubusercontent.com`
- `gist.githubusercontent.com`
- `*.github.io`

If you publish a GitHub-hosted manifest, include:

- `metadata.github_username`
- `metadata.signature.openssh`
- `metadata.signature.algorithm` set to `openssh-ssh-ed25519`

## A Good Authoring Pattern

For most manifests, keep the structure simple:

1. Declare a small set of `config_fields`
2. Group them into a few `wizard.pages`
3. Render one or more templates
4. Materialize artifacts or run host-owned actions
5. Limit custom commands to cases where SparkWriter does not already own the workflow

That shape matches the current UI and is the easiest path to a stable manifest.
