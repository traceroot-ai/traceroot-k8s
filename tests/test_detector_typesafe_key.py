"""Chart guard: the detector worker gets TraceRoot's TypeSafe key, optionally.

Signal grouping runs Jev on TYPESAFE_API_KEY when it is set and falls back to
OpenAI otherwise, so the key is optional end to end: a secret without the
``typesafe-api-key`` field must still start the worker. That makes ``optional:
true`` on the secretKeyRef load-bearing; without it the pod would fail to start
on every environment that has not added the field yet. Only the detector worker
reads the key, so the other deployments do not get it.
"""

import shutil
import subprocess

import pytest
import yaml

from test_ingress_internal_block import _CHART, _values


def test_key_name_matches_the_secret_field():
    assert _values()["llmKeys"]["keys"]["typesafeApiKey"] == "typesafe-api-key"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestRendered:
    @staticmethod
    def _env_by_deployment(*extra: str) -> dict[str, dict]:
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

    def _with_llm_keys(self) -> dict[str, dict]:
        return self._env_by_deployment("--set", "llmKeys.existingSecret=traceroot-llm-keys")

    def test_detector_reads_the_key_from_the_llm_keys_secret_optionally(self):
        detector = next(env for name, env in self._with_llm_keys().items() if name.endswith("detector"))
        ref = detector["TYPESAFE_API_KEY"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "traceroot-llm-keys", "key": "typesafe-api-key", "optional": True}

    def test_only_the_detector_gets_the_key(self):
        holders = [name for name, env in self._with_llm_keys().items() if "TYPESAFE_API_KEY" in env]
        assert len(holders) == 1 and holders[0].endswith("detector")

    def test_no_llm_keys_secret_means_no_key(self):
        assert all("TYPESAFE_API_KEY" not in env for env in self._env_by_deployment().values())
