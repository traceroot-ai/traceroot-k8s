"""Chart guard: the hosted MCP server and the OAuth wiring it needs on web and rest."""

import shutil
import subprocess

import pytest
import yaml

from conftest import _CHART

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

_ON = (
    "--set", "mcp.enabled=true",
    "--set", "mcp.host=mcp.example.com",
    "--set", "betterAuth.url=https://app.example.com",
)  # fmt: skip


def _value(env, name):
    return env[name]["value"]


def test_mcp_and_its_oauth_wiring_are_off_by_default(env_by_deployment):
    envs = env_by_deployment()
    assert not any(name.endswith("-mcp") for name in envs)
    keys = ("MCP_RESOURCE_URL", "TRACEROOT_OAUTH_ISSUER", "TRACEROOT_OAUTH_AUDIENCES")
    assert all(k not in env for env in envs.values() for k in keys)


def test_every_service_derives_the_same_resource_url_and_issuer(env_by_deployment):
    envs = env_by_deployment(*_ON)
    mcp = envs["traceroot-mcp"]
    web = next(env for name, env in envs.items() if name.endswith("-web"))
    rest = next(env for name, env in envs.items() if name.endswith("-rest"))

    resource, issuer = "https://mcp.example.com/mcp", "https://app.example.com/api/auth"
    assert _value(mcp, "MCP_RESOURCE_URL") == resource
    assert _value(web, "MCP_RESOURCE_URL") == resource
    assert _value(rest, "TRACEROOT_OAUTH_AUDIENCES") == resource
    assert _value(mcp, "MCP_OAUTH_ISSUER") == issuer
    assert _value(rest, "TRACEROOT_OAUTH_ISSUER") == issuer
    assert _value(mcp, "MCP_ALLOWED_HOSTS") == "mcp.example.com"
    assert _value(mcp, "TRACEROOT_UI_URL") == "http://traceroot-web:3000"
    assert _value(mcp, "BACKEND_INTERNAL_URL") == "http://traceroot-rest:8000"


def test_a_trailing_slash_on_the_app_url_does_not_reach_the_issuer(env_by_deployment):
    envs = env_by_deployment(*_ON, "--set", "betterAuth.url=https://app.example.com/")
    assert _value(envs["traceroot-mcp"], "MCP_OAUTH_ISSUER") == "https://app.example.com/api/auth"


def test_the_mcp_hostname_routes_to_the_mcp_service_ahead_of_the_app():
    out = subprocess.run(
        ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=app.example.com", *_ON],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ingress = next(d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Ingress")
    first, second = ingress["spec"]["rules"][:2]
    assert first["host"] == "mcp.example.com"
    assert first["http"]["paths"][0]["backend"]["service"]["name"] == "traceroot-mcp"
    assert second["host"] == "app.example.com"


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--set", "mcp.enabled=true"), "needs mcp.host"),
        (("--set", "mcp.enabled=true", "--set", "mcp.host=mcp.example.com"), "needs betterAuth.url"),
        (
            ("--set", "mcp.enabled=true", "--set", "mcp.host=https://mcp.example.com",
             "--set", "betterAuth.url=https://app.example.com"),
            "bare hostname",
        ),
    ],
)  # fmt: skip
def test_an_incomplete_mcp_configuration_fails_the_render(extra, message):
    result = subprocess.run(
        ["helm", "template", "traceroot", _CHART, *extra], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_web_trusts_the_configured_proxies(env_by_deployment):
    envs = env_by_deployment("--set", "web.trustedProxyCidrs=10.0.0.0/16")
    web = next(env for name, env in envs.items() if name.endswith("-web"))
    assert _value(web, "AUTH_TRUSTED_PROXY_CIDRS") == "10.0.0.0/16"
