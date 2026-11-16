"""Chart guard: the detector worker gets TraceRoot's TypeSafe key, optionally.

Signal grouping runs Jev on TYPESAFE_API_KEY when it is set and falls back to
OpenAI otherwise, so the key is optional end to end: a secret without the
``typesafe-api-key`` field must still start the worker. That makes ``optional:
true`` on the secretKeyRef load-bearing; without it the pod would fail to start
on every environment that has not added the field yet. Only the detector worker
reads the key, so the other deployments do not get it.
"""

import shutil

import pytest

from test_ingress_internal_block import _values


def test_key_name_matches_the_secret_field():
    assert _values()["llmKeys"]["keys"]["typesafeApiKey"] == "typesafe-api-key"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestRendered:
    @pytest.fixture
    def with_llm_keys(self, env_by_deployment):
        return env_by_deployment("--set", "llmKeys.existingSecret=traceroot-llm-keys")

    def test_detector_reads_the_key_from_the_llm_keys_secret_optionally(self, with_llm_keys):
        detector = next(env for name, env in with_llm_keys.items() if name.endswith("detector"))
        ref = detector["TYPESAFE_API_KEY"]["valueFrom"]["secretKeyRef"]
        assert ref == {"name": "traceroot-llm-keys", "key": "typesafe-api-key", "optional": True}

    def test_only_the_detector_gets_the_key(self, with_llm_keys):
        holders = [name for name, env in with_llm_keys.items() if "TYPESAFE_API_KEY" in env]
        assert len(holders) == 1 and holders[0].endswith("detector")

    def test_no_llm_keys_secret_means_no_key(self, env_by_deployment):
        assert all("TYPESAFE_API_KEY" not in env for env in env_by_deployment().values())
