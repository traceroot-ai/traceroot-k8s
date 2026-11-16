import os
import subprocess

import pytest
import yaml

_CHART = os.path.join(os.path.dirname(__file__), "..", "charts", "traceroot")


@pytest.fixture
def env_by_deployment():
    """Render the chart and return {deployment name: {env var name: env entry}}."""

    def render(*extra: str) -> dict[str, dict]:
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com", *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        envs = {}
        for doc in yaml.safe_load_all(out):
            if doc and doc.get("kind") == "Deployment":
                containers = doc["spec"]["template"]["spec"]["containers"]
                envs[doc["metadata"]["name"]] = {
                    e["name"]: e for c in containers for e in c.get("env", [])
                }
        return envs

    return render
