#!/usr/bin/env bash
set -euo pipefail

# Push the packaged Helm chart to the configured OCI registry.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
CHART_DIR="${ROOT_DIR}/helm"
PACKAGE_DIR="${ROOT_DIR}/package"

if [ -f "${ROOT_DIR}/.env" ]; then
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.env"
fi

: "${NEXUS_USERNAME:?NEXUS_USERNAME variable must be provided}"
: "${NEXUS_PASSWORD:?NEXUS_PASSWORD variable must be provided}"
: "${HELM_LOGIN_URL:?HELM_LOGIN_URL variable must be provided}"
: "${HELM_REPO:?HELM_REPO variable must be provided}"

CHART_NAME=$(yq -r '.name' "${CHART_DIR}/Chart.yaml")
CHART_VERSION=$(yq -r '.version' "${CHART_DIR}/Chart.yaml")
CHART_PACKAGE="${PACKAGE_DIR}/${CHART_NAME}-${CHART_VERSION}.tgz"

if [ ! -f "${CHART_PACKAGE}" ]; then
    echo "Package ${CHART_PACKAGE} not found. Run helm_package.sh first." >&2
    exit 1
fi

echo "Logging into helm registry ${HELM_LOGIN_URL}..."
echo "${NEXUS_PASSWORD}" | helm registry login "${HELM_LOGIN_URL}" --username "${NEXUS_USERNAME}" --password-stdin

echo "Publishing chart ${CHART_PACKAGE} to ${HELM_REPO}..."
helm push "${CHART_PACKAGE}" "${HELM_REPO}"

echo "Chart successfully published."
