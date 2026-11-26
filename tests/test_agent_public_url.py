"""Chart guard: links the agent shows a person use the public app URL.

publicUiUrl() reads TRACEROOT_PUBLIC_UI_URL, then NEXT_PUBLIC_APP_URL, then
http://localhost:3000. Rest already sets the first from betterAuth.url. An
empty betterAuth.url is left unset so that localhost fallback still applies.
"""

import shutil

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def test_agent_gets_the_public_url_rest_already_sets(env_by_deployment):
    envs = env_by_deployment("--set", "betterAuth.url=https://app.example.com")
    agent = envs["traceroot-agent"]
    assert agent["TRACEROOT_UI_URL"]["value"] == "http://traceroot-web:3000"
    assert agent["TRACEROOT_PUBLIC_UI_URL"]["value"] == "https://app.example.com"
    assert agent["TRACEROOT_PUBLIC_UI_URL"] == envs["traceroot-rest"]["TRACEROOT_PUBLIC_UI_URL"]


def test_agent_public_url_stays_unset_when_better_auth_url_is_empty(env_by_deployment):
    agent = env_by_deployment()["traceroot-agent"]
    assert "TRACEROOT_PUBLIC_UI_URL" not in agent
    assert "NEXT_PUBLIC_APP_URL" not in agent
