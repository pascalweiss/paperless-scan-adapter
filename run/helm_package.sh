#!/usr/bin/env bash
set -euo pipefail

# Package the Helm chart into a .tgz file inside the package directory.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
CHART_DIR="${ROOT_DIR}/helm"
PACKAGE_DIR="${ROOT_DIR}/package"

mkdir -p "${PACKAGE_DIR}"

helm package "${CHART_DIR}" --destination "${PACKAGE_DIR}"
