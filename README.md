# Paperless Scan Adapter Helm Package

This repository contains the Python based Paperless Scan Adapter together with an opinionated Helm chart and GitLab CI pipeline that build and publish both the container image and the Helm package to Nexus.

## Repository Layout

- `src/` – Python sources for the adapter.
- `Dockerfile` – Builds the runtime image used by Kubernetes.
- `helm/` – Helm chart that mirrors the configuration from the original example manifests used during the initial deployment.
- `run/` – Utility scripts invoked by the CI pipeline (and available for local use).

## Configuring the Helm Chart

The chart defaults match the example deployment:

- Configuration values are managed via a ConfigMap (`config.data`) for retry behaviour, target Paperless endpoints and logging.
- `env.secretRefs` wires `PAPERLESS_ADMIN_PASSWORD` from a Kubernetes Secret (`paperless-admin`, key `password`).
- Persistence is enabled with an SMB backed `PersistentVolume`/`PersistentVolumeClaim`. Toggle creation with `persistence.enabled` or point at an existing claim via `persistence.existingClaim`.
- Additional knobs are exposed for image overrides, image pull secrets, resources, and optional namespace/service creation.

Render values with `helm show values ./helm` and override them through your GitOps repository (e.g. Flux `HelmRelease`).

## GitLab CI Pipeline

The pipeline defined in `.gitlab-ci.yml` performs three jobs:

1. `build-docker-image` – uses `run/build_and_push.sh` to build with Podman and push to `${DOCKER_REGISTRY}/${IMAGE_NAME}:${TAG}`.
2. `build-helm-chart` – packages the chart from `helm/` and pushes it to `${HELM_REPO}` via `run/helm_package.sh` and `run/helm_publish.sh`.
3. `secret-detection` – scans the repo with Gitleaks.

Provide the following CI variables (usually masked/protected) so the jobs can authenticate against Nexus:

- `DOCKER_REGISTRY`
- `NEXUS_USERNAME`
- `NEXUS_PASSWORD`
- `HELM_LOGIN_URL` (e.g. `registry.pwlab.dev`)
- `HELM_REPO` (e.g. `oci://registry.pwlab.dev/helm`)
- Optional overrides: `IMAGE_NAME`, `TAG`

For local execution export the same variables or create a `.env` file in the repo root (see `.env_template`).

## Useful Commands

```bash
# Build & push the container image
./run/build_and_push.sh

# Package the chart locally
./run/helm_package.sh

# Publish an already packaged chart
./run/helm_publish.sh

# Render the chart with your overrides
helm template test ./helm -f my-values.yaml
```

The produced Helm chart can then be consumed by your HomeLab GitOps repo once published to Nexus.
