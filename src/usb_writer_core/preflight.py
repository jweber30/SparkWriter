"""Pre-flight environment checks for SparkWriter.

These checks run at startup — before any GTK state is initialised — to verify
the execution environment is safe and correctly configured for USB write
operations.

Fatal failures abort the program immediately with a clear message to the
operator. Non-fatal failures are printed as warnings but do not block startup.

Trust model rationale
---------------------
Manifest-level trust checks (signing, URL allow-lists, approval gating) are
meaningless if the environment itself is broken.  These checks anchor the
security model to the physical execution context:

- Python version:     wrong interpreter can silently misparse constants.
- Required tools:     lsblk and dd are called via subprocess; their absence
                      means write operations will crash mid-session.
- Not running as root: SparkWriter must not acquire elevated privileges
                      at startup; USB writes are brokered through the
                      expected privilege boundary.
- Crostini container: warns when Crostini-specific USB workarounds may not
                      apply (non-fatal — app runs on regular Linux too).
- Recommended tools:  xorriso absence only disables ISO-mod features.
"""

import os
import shutil
import sys
from dataclasses import dataclass
from typing import List, Optional

# Matches requires-python in pyproject.toml
MINIMUM_PYTHON: tuple = (3, 10)

# (binary-name, apt-package-hint)
REQUIRED_TOOLS: List[tuple] = [
    ("lsblk", "util-linux"),
    ("dd", "coreutils"),
]

RECOMMENDED_TOOLS: List[tuple] = [
    ("xorriso", "xorriso"),
]


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    fatal: bool = True


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

def check_python_version() -> CheckResult:
    """Verify the running interpreter meets the minimum version requirement."""
    current = sys.version_info[:2]
    ok = current >= MINIMUM_PYTHON
    min_str = ".".join(str(x) for x in MINIMUM_PYTHON)
    cur_str = ".".join(str(x) for x in current)
    return CheckResult(
        name="python_version",
        passed=ok,
        message=(
            f"Python {cur_str} (requires >= {min_str})"
            if ok
            else f"Python {cur_str} is too old — requires >= {min_str}"
        ),
        fatal=True,
    )


def check_required_tools() -> List[CheckResult]:
    """Verify that every binary SparkWriter shells out to during a write is present."""
    results = []
    for tool, package in REQUIRED_TOOLS:
        found = shutil.which(tool) is not None
        results.append(CheckResult(
            name=f"tool_{tool}",
            passed=found,
            message=(
                f"'{tool}' found"
                if found
                else f"'{tool}' not found — install package '{package}'"
            ),
            fatal=True,
        ))
    return results


def check_not_running_as_root() -> CheckResult:
    """SparkWriter must not start as root; USB writes use a deliberate privilege boundary."""
    is_root = os.geteuid() == 0
    return CheckResult(
        name="not_root",
        passed=not is_root,
        message=(
            "Running as root is not supported and is a security risk"
            if is_root
            else "Not running as root"
        ),
        fatal=True,
    )


def check_crostini_environment() -> CheckResult:
    """Detect whether we are inside the Crostini Linux container on ChromeOS.

    Non-fatal: SparkWriter also works on plain Linux, but Crostini-specific USB
    workarounds (the dd-to-temp-file path) will not activate outside a Crostini
    container, so the operator deserves a visible notice.
    """
    in_crostini = "CROS_CONTAINER_VERSION" in os.environ
    return CheckResult(
        name="crostini_container",
        passed=in_crostini,
        message=(
            f"Crostini container v{os.environ['CROS_CONTAINER_VERSION']} detected"
            if in_crostini
            else "Not running in Crostini — Crostini USB workarounds inactive"
        ),
        fatal=False,
    )


def check_recommended_tools() -> List[CheckResult]:
    """Check for optional tools that unlock additional features."""
    results = []
    for tool, package in RECOMMENDED_TOOLS:
        found = shutil.which(tool) is not None
        results.append(CheckResult(
            name=f"tool_{tool}",
            passed=found,
            message=(
                f"'{tool}' found"
                if found
                else f"'{tool}' not found — ISO modification features unavailable (install '{package}')"
            ),
            fatal=False,
        ))
    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_preflight() -> List[CheckResult]:
    """Collect results from all pre-flight checks in priority order."""
    results: List[CheckResult] = []
    results.append(check_python_version())
    results.extend(check_required_tools())
    results.append(check_not_running_as_root())
    results.append(check_crostini_environment())
    results.extend(check_recommended_tools())
    return results


def enforce_preflight(results: Optional[List[CheckResult]] = None) -> None:
    """Run all pre-flight checks and abort on any fatal failure.

    Call this once before initialising GTK.  Warnings are printed to stderr
    and startup continues.  Fatal failures print a summary and call sys.exit(1).

    Args:
        results: Pre-computed results (used in tests).  When None the full
                 check suite is executed.
    """
    if results is None:
        results = run_preflight()

    warnings = [r for r in results if not r.fatal and not r.passed]
    failures = [r for r in results if r.fatal and not r.passed]

    for w in warnings:
        print(f"[preflight] WARNING  {w.name}: {w.message}", file=sys.stderr)

    if failures:
        for f in failures:
            print(f"[preflight] FATAL    {f.name}: {f.message}", file=sys.stderr)
        print(
            "\nSparkWriter cannot start. Resolve the issues above and try again.",
            file=sys.stderr,
        )
        sys.exit(1)
