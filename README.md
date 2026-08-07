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

## Health Endpoint

The adapter serves a small HTTP endpoint (default port `8080`, `HEALTH_PORT`) from a
daemon thread, so it keeps answering even while the worker is busy or blocked.

| Path | Meaning | 200 when | 503 when |
|------|---------|----------|----------|
| `/healthz` | liveness | the worker is not wedged | the loop heartbeat is older than `HEALTH_STALE_AFTER_SECONDS` |
| `/readyz` | readiness | startup finished and the heartbeat is fresh | still waiting for a dependency, or the heartbeat is stale |
| `/metrics` | Prometheus scrape | always | — |

`/metrics` exposes the same state in the Prometheus text format, prefixed
`paperless_scan_adapter_`: `alive`, `ready`, `seconds_since_last_beat`,
`uptime_seconds`, `uploads_total` and `upload_failures_total`. The failure counter is
the one that cannot be seen any other way: when an upload fails for good the adapter
moves the file to the archive folder and carries on, so the document silently never
reaches Paperless.

A `ClusterIP` Service is enabled by default to give Prometheus a stable target. Nothing
else talks to this workload.

Both return the full state as JSON, which makes them useful to `curl` by hand:

```bash
kubectl -n paperless-ngx port-forward deploy/paperless-scan-adapter 8080:8080
curl -s localhost:8080/healthz | jq
```

**Why liveness is heartbeat based.** The worker polls a network share. If that mount goes
stale, a read can block indefinitely instead of raising, and the process stays `Running`
while no document is ever processed again. The worker therefore records a heartbeat
whenever it reaches a line of code, and every deliberate wait beats in slices
(`HEARTBEAT_SLICE_SECONDS`), so slow work is never mistaken for a wedge. Only a real
block stops the beat. `HEALTH_STALE_AFTER_SECONDS` defaults to 120s, comfortably above
the longest single blocking call (the 60s upload timeout).

**Startup waits instead of exiting.** A missing scan folder or an unreachable Paperless
is not a failure, it is a dependency that has not arrived yet. The adapter retries with
capped exponential backoff and reports `alive` but not `ready` meanwhile. Exiting would
only hand the same wait back to Kubernetes, inflate the restart counter and delay the
start by the CrashLoopBackOff. A missing `PAPERLESS_ADMIN_PASSWORD` still exits
immediately, because no amount of retrying fixes a configuration error.

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
