"""Chart guard: the web pod reads AgentID OAuth credentials from the configured secret."""

import shutil

import pytest

_ENV_KEYS = ("AUTH_AGENTID_CLIENT_ID", "AUTH_AGENTID_CLIENT_SECRET")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_web_reads_agentid_credentials_only_when_a_secret_is_set(env_by_deployment):
    # A non-default name, so a template that hard-codes the secret name fails.
    envs = env_by_deployment("--set", "agentidOAuth.existingSecret=custom-agentid-secret")
    web = next(env for name, env in envs.items() if name.endswith("web"))
    assert web["AUTH_AGENTID_CLIENT_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "custom-agentid-secret",
        "key": "agentid-client-id",
    }
    assert web["AUTH_AGENTID_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "custom-agentid-secret",
        "key": "agentid-client-secret",
    }
    assert all(k not in env for env in env_by_deployment().values() for k in _ENV_KEYS)
