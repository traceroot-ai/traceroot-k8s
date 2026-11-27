import os
import subprocess

import pytest
import yaml

_CHART = os.path.join(os.path.dirname(__file__), "..", "charts", "traceroot")


@pytest.fixture
def rendered_deployments():
    """Render the chart and return deployments by name."""

    def render(*extra: str) -> dict[str, dict]:
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com", *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        deployments = {}
        for doc in yaml.safe_load_all(out):
            if doc and doc.get("kind") == "Deployment":
                deployments[doc["metadata"]["name"]] = doc
        return deployments

    return render


@pytest.fixture
def env_by_deployment(rendered_deployments):
    """Render the chart and return {deployment name: {env var name: env entry}}."""

    def render(*extra: str) -> dict[str, dict]:
        deployments = rendered_deployments(*extra)
        return {
            name: {
                entry["name"]: entry
                for container in doc["spec"]["template"]["spec"]["containers"]
                for entry in container.get("env", [])
            }
            for name, doc in deployments.items()
        }

    return render
