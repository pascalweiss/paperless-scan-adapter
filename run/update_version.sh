#!/usr/bin/env bash

set -euo pipefail

show_help() {
  cat <<'USAGE'
Usage: update_version.sh [OPTIONS] VERSION

Update version numbers across the project to trigger a new CI build.

OPTIONS:
  -h, --help    Show this help message and exit

VERSION:
  Must be in the format x.y.z (for example 0.2.1)

EXAMPLES:
  update_version.sh 0.2.0    # Set version to 0.2.0
  update_version.sh --help   # Display this help message
USAGE
}

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT="$SCRIPT_DIR/.."
cd "$REPO_ROOT"

validate_version() {
  local version=$1
  if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Version must be in format x.y.z (e.g. 0.1.5)" >&2
    exit 1
  fi
}

CURRENT_VERSION=$(grep -oP 'TAG:\s*"?\K[0-9]+\.[0-9]+\.[0-9]+' .gitlab-ci.yml || true)
if [[ -z "$CURRENT_VERSION" ]]; then
  echo "Error: Could not determine current version from .gitlab-ci.yml" >&2
  exit 1
fi

echo "Current version: $CURRENT_VERSION"

if [[ $# -eq 0 ]]; then
  echo "Paperless Scan Adapter Version Manager"
  echo "Current version: $CURRENT_VERSION"
  echo
  echo "Provide a new version number to update the project (e.g. $0 0.1.7)."
  echo "For more details run: $0 --help"
  exit 0
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  show_help
  exit 0
fi

NEW_VERSION=$1
validate_version "$NEW_VERSION"

echo "New version will be: $NEW_VERSION"

echo "Updating .gitlab-ci.yml..."
python3 - "$NEW_VERSION" <<'PY'
import re, sys, pathlib
new_version = sys.argv[1]
path = pathlib.Path('.gitlab-ci.yml')
content = path.read_text()
pattern = re.compile(r'(TAG:\s*)"?([0-9]+\.[0-9]+\.[0-9]+)"?')
updated, count = pattern.subn(lambda m: f"{m.group(1)}\"{new_version}\"", content, count=1)
if count == 0:
    raise SystemExit('Failed to update TAG in .gitlab-ci.yml')
path.write_text(updated)
PY

echo "Updating helm/Chart.yaml (version & appVersion)..."
python3 - "$NEW_VERSION" <<'PY'
import re, sys, pathlib
new_version = sys.argv[1]
path = pathlib.Path('helm/Chart.yaml')
content = path.read_text()
def repl_version(match):
    return f"{match.group(1)}{new_version}"
content, count = re.subn(r'(^version:\s*)(\d+\.\d+\.\d+)', repl_version, content, count=1, flags=re.MULTILINE)
if count == 0:
    raise SystemExit('Failed to update chart version')
def repl_app_version(match):
    prefix, _, suffix = match.groups()
    return f"{prefix}{new_version}{suffix}"
content, count = re.subn(r'(^appVersion:\s*")([^"\n]+)(")', repl_app_version, content, count=1, flags=re.MULTILINE)
if count == 0:
    def repl_alt(match):
        return f'appVersion: "{new_version}"'
    content, count = re.subn(r'^appVersion:\s*\S+', repl_alt, content, count=1, flags=re.MULTILINE)
    if count == 0:
        raise SystemExit('Failed to update appVersion')
path.write_text(content)
PY

echo "Updating helm/values.yaml (image.tag)..."
python3 - "$NEW_VERSION" <<'PY'
import re, sys, pathlib
new_version = sys.argv[1]
path = pathlib.Path('helm/values.yaml')
content = path.read_text()
pattern = re.compile(r'(^\s*tag:\s*).*$' , re.MULTILINE)
def repl(match):
    return f"{match.group(1)}\"{new_version}\""
content, count = pattern.subn(repl, content, count=1)
if count == 0:
    raise SystemExit('Failed to update image tag')
path.write_text(content)
PY

echo "Version updated successfully to $NEW_VERSION"

echo "Next steps:"
echo "  git add .gitlab-ci.yml helm/Chart.yaml helm/values.yaml"
echo "  git commit -m \"Bump version to $NEW_VERSION\""
echo "  git push"
