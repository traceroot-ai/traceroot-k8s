# traceroot-k8s

Kubernetes configuration and Helm chart for [TraceRoot](https://github.com/traceroot-ai/traceroot).

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
| `postgresql.*`, `redis.*`, `clickhouse.*`, `s3.*` | external service connection details |
| `*.existingSecret` / `secretKeys` | names of the Kubernetes Secrets holding credentials |

Secrets are referenced by name, never set as values in this chart.

## Contributing

Chart changes are released automatically on merge to `main` — bump `version` in `Chart.yaml`
in the same PR.
