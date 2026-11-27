"""Chart guard: the billing pod gets the billing flag, SMTP settings and public app URL it reads."""

import shutil

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

_BILLING = "traceroot-billing"


@pytest.mark.parametrize("value", ["false", "true"])
def test_billing_pod_gets_enable_billing_from_values(env_by_deployment, value):
    """ENABLE_BILLING must follow enableBilling, or self-hosted billing jobs still cap Free workspaces."""
    envs = env_by_deployment("--set", f"enableBilling={value}")
    assert envs[_BILLING]["ENABLE_BILLING"]["value"] == value
    assert envs[_BILLING]["ENABLE_BILLING"] == envs["traceroot-rest"]["ENABLE_BILLING"]


def test_billing_pod_gets_smtp_settings_from_the_secret(env_by_deployment):
    """Free-plan usage emails need the same SMTP transport the detector uses."""
    envs = env_by_deployment(
        "--set",
        "smtp.existingSecret=mail-secret",
        "--set",
        "smtp.keys.url=smtp-url-key",
        "--set",
        "smtp.keys.mailFrom=from-key",
    )
    billing = envs[_BILLING]
    detector = envs["traceroot-detector"]
    assert billing["TRACEROOT_SMTP_URL"]["valueFrom"] == {
        "secretKeyRef": {"name": "mail-secret", "key": "smtp-url-key"}
    }
    assert billing["TRACEROOT_SMTP_URL"] == detector["TRACEROOT_SMTP_URL"]
    assert billing["TRACEROOT_SMTP_MAIL_FROM"] == detector["TRACEROOT_SMTP_MAIL_FROM"]


def test_billing_pod_has_no_smtp_settings_without_a_secret(env_by_deployment):
    """With no SMTP secret configured, the billing pod must not reference SMTP variables."""
    billing = env_by_deployment()[_BILLING]
    assert "TRACEROOT_SMTP_URL" not in billing
    assert "TRACEROOT_SMTP_MAIL_FROM" not in billing


def test_billing_pod_gets_public_app_url_for_email_links(env_by_deployment):
    """Usage-email billing links must point at betterAuth.url, not the localhost fallback."""
    envs = env_by_deployment("--set", "betterAuth.url=https://app.example.com")
    assert envs[_BILLING]["NEXT_PUBLIC_APP_URL"]["value"] == "https://app.example.com"
    assert envs[_BILLING]["NEXT_PUBLIC_APP_URL"] == envs["traceroot-web"]["NEXT_PUBLIC_APP_URL"]
