#!/usr/bin/env bash
set -euo pipefail

# Build the container image with Podman and push it to the configured registry.

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ -f ".env" ]; then
    # shellcheck disable=SC1091
    source .env
fi

: "${IMAGE_NAME:?IMAGE_NAME variable must be provided}"
: "${TAG:?TAG variable must be provided}"
: "${DOCKER_REGISTRY:?DOCKER_REGISTRY variable must be provided}"
: "${NEXUS_USERNAME:?NEXUS_USERNAME variable must be provided}"
: "${NEXUS_PASSWORD:?NEXUS_PASSWORD variable must be provided}"

LOCAL_IMAGE_NAME="localhost/${IMAGE_NAME}:${TAG}"
REMOTE_IMAGE_NAME="${DOCKER_REGISTRY}/${IMAGE_NAME}:${TAG}"

echo "Building ${LOCAL_IMAGE_NAME} with Podman..."
podman build --format docker -t "${LOCAL_IMAGE_NAME}" -f Dockerfile .

echo "Tagging image as ${REMOTE_IMAGE_NAME}..."
podman tag "${LOCAL_IMAGE_NAME}" "${REMOTE_IMAGE_NAME}"

echo "Logging into registry ${DOCKER_REGISTRY}..."
echo "${NEXUS_PASSWORD}" | podman login -u "${NEXUS_USERNAME}" --password-stdin "${DOCKER_REGISTRY}"

echo "Pushing image to ${REMOTE_IMAGE_NAME}..."
podman push "${REMOTE_IMAGE_NAME}"

echo "Image successfully pushed to ${REMOTE_IMAGE_NAME}"
