import pytest

from spark_writer.plugins.manifest_download import parse_downloaded_manifest


def test_parse_downloaded_manifest_accepts_json_with_generic_content_type():
    manifest = parse_downloaded_manifest(
        b'{"version": "1.6"}',
        content_type="application/octet-stream",
        final_url="https://example.test/manifest",
    )

    assert manifest == {"version": "1.6"}


def test_parse_downloaded_manifest_reports_html_login_page():
    with pytest.raises(ValueError) as exc_info:
        parse_downloaded_manifest(
            b"<!doctype html><html><title>Sign in</title></html>",
            content_type="text/html; charset=utf-8",
            final_url="https://example.test/login",
        )

    message = str(exc_info.value)
    assert "returned an HTML page instead of JSON" in message
    assert "may require sign-in or browser-session authentication" in message
    assert "Final URL: https://example.test/login" in message


def test_parse_downloaded_manifest_reports_empty_response():
    with pytest.raises(ValueError, match="returned an empty response"):
        parse_downloaded_manifest(b" \n")


def test_parse_downloaded_manifest_reports_malformed_json():
    with pytest.raises(ValueError) as exc_info:
        parse_downloaded_manifest(
            b'{"version": }',
            content_type="application/json",
        )

    message = str(exc_info.value)
    assert "not valid JSON (line 1, column 13" in message
    assert "Content-Type: application/json" in message


def test_parse_downloaded_manifest_requires_top_level_object():
    with pytest.raises(ValueError, match="object at its top level"):
        parse_downloaded_manifest(b"[]", content_type="application/json")
