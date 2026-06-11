import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from spark_writer.plugins.json_plugin import JsonSparkPlug
from spark_writer.plugins.template_engine import SparkTemplateEngine
from spark_writer.plugins.base import SparkPlug


def test_render_supports_if_with_hyphenated_keys():
    engine = SparkTemplateEngine()
    template = (
        "[global]\n"
        "{% if root-password-hashed %}"
        "root-password-hashed = \"{{root-password-hashed}}\"\n"
        "{% else %}"
        "root-password = \"{{root-password}}\"\n"
        "{% endif %}"
    )

    rendered = engine.render(
        template,
        {
            "root-password-hashed": "$6$abcdef",
            "root-password": "plaintext",
        },
    )

    assert "root-password-hashed = \"$6$abcdef\"" in rendered
    assert "root-password = \"plaintext\"" not in rendered


def test_render_supports_else_path_with_hyphenated_keys():
    engine = SparkTemplateEngine()
    template = "{% if root-password-hashed %}A{% else %}{{root-password}}{% endif %}"

    rendered = engine.render(
        template,
        {
            "root-password-hashed": "",
            "root-password": "fallback",
        },
    )

    assert rendered == "fallback"


def test_render_raises_on_undefined_variable():
    engine = SparkTemplateEngine()

    with pytest.raises(ValueError):
        engine.render("value={{missing}}", {})


# ---------------------------------------------------------------------------
# _resolve_template_string  (array and file: forms)
# ---------------------------------------------------------------------------

def _minimal_manifest(templates: dict, tmp_path: Path) -> JsonSparkPlug:
    """Build a minimal loadable manifest JSON and return a JsonSparkPlug for it."""
    manifest = {
        "version": "1.4",
        "metadata": {"id": "test-plugin", "name": "Test Plugin", "version": "1.4"},
        "requires": {"commands": []},
        "config_fields": [],
        "templates": templates,
        "actions": {},
    }
    p = tmp_path / "test-plugin.json"
    p.write_text(json.dumps(manifest))
    return JsonSparkPlug(str(p))


def test_resolve_template_string_from_array(tmp_path):
    lines = ["line one", "line two", "line three"]
    plugin = _minimal_manifest({"t": lines}, tmp_path)
    result = plugin._resolve_template_string(lines)
    assert result == "line one\nline two\nline three"


def test_resolve_template_string_from_file(tmp_path):
    sidecar = tmp_path / "body.sh"
    sidecar.write_text("#!/bin/bash\necho hello\n")

    manifest = {
        "version": "1.4",
        "metadata": {"id": "test-plugin", "name": "Test Plugin", "version": "1.4"},
        "requires": {"commands": []},
        "config_fields": [],
        "templates": {"script": {"file": "body.sh"}},
        "actions": {},
    }
    mp = tmp_path / "test-plugin.json"
    mp.write_text(json.dumps(manifest))
    plugin = JsonSparkPlug(str(mp))
    result = plugin._resolve_template_string({"file": "body.sh"})
    assert result == "#!/bin/bash\necho hello\n"


def test_resolve_template_string_from_named_asset(tmp_path):
    sidecar = tmp_path / "body.sh"
    sidecar.write_text("#!/bin/bash\necho asset\n")

    manifest = {
        "version": "1.4",
        "metadata": {"id": "test-plugin", "name": "Test Plugin", "version": "1.4"},
        "requires": {"commands": []},
        "assets": {
            "setup_script": {
                "path": "body.sh",
                "sha256": "0" * 64,
            }
        },
        "config_fields": [],
        "templates": {"script": {"asset": "setup_script"}},
        "actions": {},
    }
    mp = tmp_path / "test-plugin.json"
    mp.write_text(json.dumps(manifest))
    plugin = JsonSparkPlug(str(mp))
    result = plugin._resolve_template_string({"asset": "setup_script"})
    assert result == "#!/bin/bash\necho asset\n"


def test_resolve_template_string_file_missing_raises(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "test-plugin", "name": "Test Plugin", "version": "1.4"},
        "requires": {"commands": []},
        "config_fields": [],
        "templates": {"t": {"file": "no-such-file.sh"}},
        "actions": {},
    }
    mp = tmp_path / "test-plugin.json"
    mp.write_text(json.dumps(manifest))
    # Plugin loads but template resolution at render time should raise
    plugin = JsonSparkPlug(str(mp))
    with pytest.raises(ValueError, match="Cannot read template file"):
        plugin._resolve_template_string({"file": "no-such-file.sh"})


def test_resolve_template_string_unknown_asset_raises(tmp_path):
    manifest = {
        "version": "1.4",
        "metadata": {"id": "test-plugin", "name": "Test Plugin", "version": "1.4"},
        "requires": {"commands": []},
        "assets": {},
        "config_fields": [],
        "templates": {"t": {"asset": "missing"}},
        "actions": {},
    }
    mp = tmp_path / "test-plugin.json"
    mp.write_text(json.dumps(manifest))
    plugin = JsonSparkPlug(str(mp))
    with pytest.raises(ValueError, match="Unknown asset reference"):
        plugin._resolve_template_string({"asset": "missing"})


def test_array_template_renders_correctly(tmp_path):
    lines = ["hello = \"{{name}}\"", "world"]
    plugin = _minimal_manifest({"greet": lines}, tmp_path)
    context = plugin._build_template_context({"name": "Alice"}, {}, None, None)
    result = plugin.template_engine.render(plugin._resolve_template_string(lines), context)
    assert result == 'hello = "Alice"\nworld'


def test_config_field_coercion_preserves_standard_field_and_storage():
    field = SparkPlug._coerce_config_field(
        {
            "id": "ssh-keys",
            "label": "SSH Public Keys",
            "type": "multiline",
            "standard_field": "user.ssh_public_keys",
            "storage": {
                "persist": True,
                "scope": "user",
                "secret": False,
            },
        }
    )

    assert field.standard_field == "user.ssh_public_keys"
    assert field.storage == {
        "persist": True,
        "scope": "user",
        "secret": False,
    }
