# traceroot-k8s

Kubernetes configuration and Helm chart for [traceroot](https://github.com/traceroot-ai/traceroot).

## Install

```bash
helm repo add traceroot https://traceroot-ai.github.io/traceroot-k8s
helm repo update
helm install traceroot traceroot/traceroot -f my-values.yaml
```

## Requirements

The chart deploys the application only. It expects PostgreSQL, ClickHouse, Redis, and an
S3-compatible object store to exist already, and reads their credentials from Kubernetes
Secrets you provide.

To provision that infrastructure on AWS as well, use
[traceroot-terraform-aws](https://github.com/traceroot-ai/traceroot-terraform-aws), which
installs this chart for you.

## Configuration

See [`charts/traceroot/values.yaml`](charts/traceroot/values.yaml) for the full set of values. The ones most operators change:

| Value | Purpose |
|---|---|
| `image.tag` | application version to run |
| `ingress.host` | public hostname |
| `ingress.blockInternalRoutes` | keep `/api/v1/internal` off the load balancer (default `true`) |
| `worker.concurrency` | Celery worker processes per pod (default `2`) |
| `postgresql.*`, `redis.*`, `clickhouse.*`, `s3.*` | external service connection details |
| `*.existingSecret` / `secretKeys` | names of the Kubernetes Secrets holding credentials |

The worker's default memory request and limit are both `2Gi`, providing headroom above
the documented estimate of `concurrency * 0.53 GiB + 0.25 GiB` at the default
concurrency. Increase
`worker.resources.requests.memory` and `worker.resources.limits.memory` when raising
`worker.concurrency`; this estimate is not a workload-specific guarantee. If
`additionalEnv` includes `WORKER_CONCURRENCY`, its first matching entry takes precedence
over `worker.concurrency` on the worker and is rendered there only once. `additionalEnv`
continues to apply to all deployments as before.

Secrets are referenced by name, never set as values in this chart.

## Contributing

Chart changes are released automatically on merge to `main` — bump `version` in `Chart.yaml`
in the same PR.
