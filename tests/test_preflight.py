"""Tests for usb_writer_core.preflight — startup pre-flight checks.

Design intent: every check is unit-tested in isolation so failures can be
attributed precisely.  enforce_preflight() is tested with injected results
so no subprocess or environment side-effects leak between cases.
"""

import os
import sys

import pytest
from unittest.mock import patch

from usb_writer_core.preflight import (
    CheckResult,
    MINIMUM_PYTHON,
    REQUIRED_TOOLS,
    check_crostini_environment,
    check_not_running_as_root,
    check_python_version,
    check_recommended_tools,
    check_required_tools,
    enforce_preflight,
    run_preflight,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env_without(key: str) -> dict:
    """Return os.environ as a dict with *key* removed."""
    return {k: v for k, v in os.environ.items() if k != key}


# ---------------------------------------------------------------------------
# check_python_version
# ---------------------------------------------------------------------------

class TestCheckPythonVersion:
    def test_passes_with_running_interpreter(self):
        """The interpreter running this test suite must satisfy the minimum."""
        result = check_python_version()
        assert result.passed
        assert result.fatal

    def test_fails_on_python_39(self):
        with patch.object(sys, "version_info", (3, 9, 0, "final", 0)):
            result = check_python_version()
        assert not result.passed
        assert result.fatal
        assert "3.9" in result.message

    def test_passes_on_exact_minimum(self):
        exact = MINIMUM_PYTHON + (0, "final", 0)
        with patch.object(sys, "version_info", exact):
            result = check_python_version()
        assert result.passed

    def test_passes_on_future_version(self):
        future = (4, 0, 0, "final", 0)
        with patch.object(sys, "version_info", future):
            result = check_python_version()
        assert result.passed

    def test_message_contains_current_and_minimum(self):
        with patch.object(sys, "version_info", (3, 8, 0, "final", 0)):
            result = check_python_version()
        assert "3.8" in result.message
        assert "3.10" in result.message


# ---------------------------------------------------------------------------
# check_required_tools
# ---------------------------------------------------------------------------

class TestCheckRequiredTools:
    def _find(self, results, tool_name):
        return next(r for r in results if r.name == f"tool_{tool_name}")

    def test_all_present_when_which_returns_path(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value="/usr/bin/found"):
            results = check_required_tools()
        assert all(r.passed for r in results)

    def test_all_fatal(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value=None):
            results = check_required_tools()
        assert all(r.fatal for r in results)

    def test_lsblk_missing_fails(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value=None):
            results = check_required_tools()
        r = self._find(results, "lsblk")
        assert not r.passed
        assert "lsblk" in r.message
        assert "util-linux" in r.message

    def test_dd_missing_fails(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value=None):
            results = check_required_tools()
        r = self._find(results, "dd")
        assert not r.passed
        assert "coreutils" in r.message

    def test_covers_all_required_tool_names(self):
        """Every entry in REQUIRED_TOOLS must produce exactly one CheckResult."""
        with patch("usb_writer_core.preflight.shutil.which", return_value="/bin/x"):
            results = check_required_tools()
        result_names = {r.name for r in results}
        expected_names = {f"tool_{t}" for t, _ in REQUIRED_TOOLS}
        assert result_names == expected_names


# ---------------------------------------------------------------------------
# check_not_running_as_root
# ---------------------------------------------------------------------------

class TestCheckNotRunningAsRoot:
    def test_passes_as_normal_user(self):
        with patch("usb_writer_core.preflight.os.geteuid", return_value=1000):
            result = check_not_running_as_root()
        assert result.passed
        assert result.fatal

    def test_fails_as_root(self):
        with patch("usb_writer_core.preflight.os.geteuid", return_value=0):
            result = check_not_running_as_root()
        assert not result.passed
        assert result.fatal
        assert "root" in result.message.lower()

    def test_passes_as_uid_1(self):
        """Any non-zero UID is acceptable."""
        with patch("usb_writer_core.preflight.os.geteuid", return_value=1):
            result = check_not_running_as_root()
        assert result.passed


# ---------------------------------------------------------------------------
# check_crostini_environment
# ---------------------------------------------------------------------------

class TestCheckCrostiniEnvironment:
    def test_detected_when_env_var_present(self):
        with patch.dict("os.environ", {"CROS_CONTAINER_VERSION": "108"}):
            result = check_crostini_environment()
        assert result.passed
        assert "108" in result.message

    def test_not_fatal_when_absent(self):
        with patch.dict("os.environ", _env_without("CROS_CONTAINER_VERSION"), clear=True):
            result = check_crostini_environment()
        assert not result.passed
        assert not result.fatal  # operator warning only

    def test_absent_message_mentions_crostini(self):
        with patch.dict("os.environ", _env_without("CROS_CONTAINER_VERSION"), clear=True):
            result = check_crostini_environment()
        assert "Crostini" in result.message


# ---------------------------------------------------------------------------
# check_recommended_tools
# ---------------------------------------------------------------------------

class TestCheckRecommendedTools:
    def test_xorriso_present(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value="/usr/bin/xorriso"):
            results = check_recommended_tools()
        xorriso = next(r for r in results if r.name == "tool_xorriso")
        assert xorriso.passed

    def test_xorriso_missing_is_not_fatal(self):
        with patch("usb_writer_core.preflight.shutil.which", return_value=None):
            results = check_recommended_tools()
        xorriso = next(r for r in results if r.name == "tool_xorriso")
        assert not xorriso.passed
        assert not xorriso.fatal
        assert "ISO" in xorriso.message


# ---------------------------------------------------------------------------
# run_preflight (integration — no real subprocess)
# ---------------------------------------------------------------------------

class TestRunPreflight:
    def test_returns_list_of_check_results(self):
        with (
            patch("usb_writer_core.preflight.shutil.which", return_value="/bin/x"),
            patch("usb_writer_core.preflight.os.geteuid", return_value=1000),
        ):
            results = run_preflight()
        assert isinstance(results, list)
        assert all(isinstance(r, CheckResult) for r in results)

    def test_contains_python_version_result(self):
        with (
            patch("usb_writer_core.preflight.shutil.which", return_value="/bin/x"),
            patch("usb_writer_core.preflight.os.geteuid", return_value=1000),
        ):
            results = run_preflight()
        names = [r.name for r in results]
        assert "python_version" in names

    def test_contains_not_root_result(self):
        with (
            patch("usb_writer_core.preflight.shutil.which", return_value="/bin/x"),
            patch("usb_writer_core.preflight.os.geteuid", return_value=1000),
        ):
            results = run_preflight()
        names = [r.name for r in results]
        assert "not_root" in names


# ---------------------------------------------------------------------------
# enforce_preflight
# ---------------------------------------------------------------------------

class TestEnforcePreflight:
    def test_all_passing_does_not_exit(self):
        passing = [CheckResult("ok_check", True, "all good", fatal=True)]
        enforce_preflight(passing)  # must not raise

    def test_fatal_failure_exits_with_code_1(self):
        failing = [CheckResult("tool_lsblk", False, "lsblk not found", fatal=True)]
        with pytest.raises(SystemExit) as exc_info:
            enforce_preflight(failing)
        assert exc_info.value.code == 1

    def test_non_fatal_failure_does_not_exit(self):
        warning = [CheckResult("crostini_container", False, "not in crostini", fatal=False)]
        enforce_preflight(warning)  # must not raise

    def test_mixed_results_exits_when_any_fatal_fails(self):
        checks = [
            CheckResult("warn", False, "just a warning", fatal=False),
            CheckResult("fatal", False, "hard failure", fatal=True),
            CheckResult("ok", True, "fine", fatal=True),
        ]
        with pytest.raises(SystemExit):
            enforce_preflight(checks)

    def test_warning_printed_to_stderr(self, capsys):
        warning = [CheckResult("crostini_container", False, "not in crostini", fatal=False)]
        enforce_preflight(warning)
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "crostini_container" in captured.err

    def test_fatal_message_printed_to_stderr(self, capsys):
        failing = [CheckResult("tool_lsblk", False, "lsblk not found", fatal=True)]
        with pytest.raises(SystemExit):
            enforce_preflight(failing)
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "tool_lsblk" in captured.err

    def test_abort_hint_included_in_stderr(self, capsys):
        failing = [CheckResult("tool_dd", False, "dd not found", fatal=True)]
        with pytest.raises(SystemExit):
            enforce_preflight(failing)
        captured = capsys.readouterr()
        assert "cannot start" in captured.err

    def test_no_output_when_all_pass(self, capsys):
        passing = [
            CheckResult("python_version", True, "ok", fatal=True),
            CheckResult("tool_lsblk", True, "ok", fatal=True),
        ]
        enforce_preflight(passing)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_no_results_runs_live_suite(self):
        """Calling with no argument runs the real checks; just verify it returns."""
        # We can't assert pass/fail since the CI environment varies, but it
        # must not raise an unexpected exception type.
        with (
            patch("usb_writer_core.preflight.shutil.which", return_value="/bin/x"),
            patch("usb_writer_core.preflight.os.geteuid", return_value=1000),
        ):
            try:
                enforce_preflight()
            except SystemExit:
                pass  # acceptable if Crostini check is fatal (it is not; this branch is never taken)
