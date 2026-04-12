#!/usr/bin/env bash
set -euo pipefail

# Build a Debian package for SparkWriter from this repository root.
#
# What this does:
# 1) Verifies Debian packaging files exist.
# 2) Optionally installs build dependencies from debian/control.
# 3) Runs dpkg-buildpackage to produce .deb artifacts in the parent directory.
#
# Usage:
#   ./scripts/build-deb.sh
#   ./scripts/build-deb.sh --install-deps
#   ./scripts/build-deb.sh --skip-tests
#   ./scripts/build-deb.sh --signed

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYPROJECT_FILE="pyproject.toml"
CORE_INIT_FILE="src/usb_writer_core/__init__.py"
CHANGELOG_FILE="debian/changelog"

INSTALL_DEPS="false"
SIGNED="false"
SKIP_TESTS="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-deps)
            INSTALL_DEPS="true"
            shift
            ;;
        --signed)
            SIGNED="true"
            shift
            ;;
        --skip-tests)
            SKIP_TESTS="true"
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Build SparkWriter Debian package.

Options:
  --install-deps   Install build dependencies with apt-get build-dep
    --skip-tests     Skip package tests (sets DEB_BUILD_OPTIONS+=nocheck)
  --signed         Build with source/signing enabled
  -h, --help       Show this help

Artifacts are generated in the parent directory of the repo.
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

if [[ ! -d debian ]]; then
    echo "Missing debian/ directory. Run this from the SparkWriter repository." >&2
    exit 1
fi

if [[ ! -f "$PYPROJECT_FILE" ]]; then
    echo "Missing $PYPROJECT_FILE; cannot auto-bump version." >&2
    exit 1
fi

if [[ ! -f "$CORE_INIT_FILE" ]]; then
    echo "Missing $CORE_INIT_FILE; cannot auto-bump version." >&2
    exit 1
fi

if [[ ! -f "$CHANGELOG_FILE" ]]; then
    echo "Missing $CHANGELOG_FILE; cannot auto-bump Debian package version." >&2
    exit 1
fi

bump_patch_version() {
    local current_version
    current_version="$(grep -E '^version = "[0-9]+\.[0-9]+\.[0-9]+"$' "$PYPROJECT_FILE" | head -n1 | sed -E 's/^version = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/')"

    if [[ -z "$current_version" ]]; then
        echo "Could not parse project version from $PYPROJECT_FILE" >&2
        exit 1
    fi

    if [[ ! "$current_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        echo "Project version must be semantic x.y.z, got: $current_version" >&2
        exit 1
    fi

    local major minor patch new_patch new_version
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    patch="${BASH_REMATCH[3]}"
    new_patch="$((patch + 1))"
    new_version="${major}.${minor}.${new_patch}"

    local pyproject_tmp
    pyproject_tmp="$(mktemp)"
    sed -E "0,/^version = \"[0-9]+\.[0-9]+\.[0-9]+\"$/s//version = \"${new_version}\"/" "$PYPROJECT_FILE" > "$pyproject_tmp"
    mv "$pyproject_tmp" "$PYPROJECT_FILE"

    local core_tmp
    core_tmp="$(mktemp)"
    sed -E "0,/^__version__ = \"[0-9]+\.[0-9]+\.[0-9]+\"$/s//__version__ = \"${new_version}\"/" "$CORE_INIT_FILE" > "$core_tmp"
    mv "$core_tmp" "$CORE_INIT_FILE"

    local top_line
    top_line="$(head -n1 "$CHANGELOG_FILE")"
    local package_name distribution urgency deb_revision

    if [[ "$top_line" =~ ^([^[:space:]]+)[[:space:]]+\(([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)\)[[:space:]]+([^;]+)\;[[:space:]]+urgency=([^[:space:]]+)$ ]]; then
        package_name="${BASH_REMATCH[1]}"
        distribution="${BASH_REMATCH[4]}"
        urgency="${BASH_REMATCH[5]}"
        deb_revision="${BASH_REMATCH[3]}"
    else
        echo "Could not parse Debian version header in $CHANGELOG_FILE" >&2
        exit 1
    fi

    local maintainer_line
    maintainer_line="$(grep -m1 '^ -- ' "$CHANGELOG_FILE" || true)"
    local maintainer_name maintainer_email

    if [[ "$maintainer_line" =~ ^[[:space:]]--[[:space:]](.+)[[:space:]]\<([^\>]+)\>[[:space:]]{2} ]]; then
        maintainer_name="${BASH_REMATCH[1]}"
        maintainer_email="${BASH_REMATCH[2]}"
    else
        maintainer_name="${DEBFULLNAME:-SparkWriter Maintainer}"
        maintainer_email="${DEBEMAIL:-noreply@example.com}"
    fi

    local changelog_tmp
    changelog_tmp="$(mktemp)"
    {
        echo "${package_name} (${new_version}-1) ${distribution}; urgency=${urgency}"
        echo
        echo "  * Automated patch version bump via scripts/build-deb.sh."
        echo
        echo " -- ${maintainer_name} <${maintainer_email}>  $(LC_ALL=C date -R)"
        echo
        cat "$CHANGELOG_FILE"
    } > "$changelog_tmp"
    mv "$changelog_tmp" "$CHANGELOG_FILE"

    echo "Version bumped: ${current_version} -> ${new_version} (Debian revision reset to 1 from ${deb_revision})."
}

if [[ "$INSTALL_DEPS" == "true" ]]; then
    echo "[1/4] Installing Debian build dependencies..."
    sudo apt-get update
    # Pulls packages listed in Build-Depends from debian/control.
    sudo apt-get build-dep -y .
fi

echo "[2/4] Bumping patch version..."
bump_patch_version

echo "[3/4] Cleaning previous packaging outputs..."
rm -f ../spark-writer_*.deb ../spark-writer_*.changes ../spark-writer_*.buildinfo ../spark-writer_*.dsc ../spark-writer_*.tar.* || true

echo "[4/4] Building package..."
if [[ "$SKIP_TESTS" == "true" ]]; then
    # Debian-standard way to disable dh_auto_test/pybuild tests for a build.
    if [[ -n "${DEB_BUILD_OPTIONS:-}" ]]; then
        export DEB_BUILD_OPTIONS="${DEB_BUILD_OPTIONS} nocheck"
    else
        export DEB_BUILD_OPTIONS="nocheck"
    fi
    # Keep pybuild-specific knob for compatibility with environments that honor it.
    export PYBUILD_DISABLE_TEST=1
    echo "Tests disabled for this build (DEB_BUILD_OPTIONS=${DEB_BUILD_OPTIONS})."
fi

if [[ "$SIGNED" == "true" ]]; then
    # Signed/source build (requires proper maintainer signing setup).
    dpkg-buildpackage -us -uc
else
    # Fast local build: binary only, unsigned.
    dpkg-buildpackage -us -uc -b
fi

echo
echo "Build complete. Artifacts:"
ls -1 ../spark-writer_* 2>/dev/null || echo "No artifacts found in parent directory."
