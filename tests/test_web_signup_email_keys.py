"""Chart guard: web gets the sign-up email keys from the smtp and slack secrets, optionally.

The welcome email and the sign-up Slack post run only when TRACEROOT_CLOUD=true, so
every environment without these secret fields must still start web. That makes
``optional: true`` load-bearing, as for the detector's TypeSafe key. Only web
reads them, so the other deployments do not get them.
"""

import shutil

import pytest

from test_detector_typesafe_key import TestRendered as _Rendered
from test_ingress_internal_block import _values

KEYS = {
    "RESEND_API_KEY": ("smtp", "resendApiKey", "resend-api-key"),
    "TRACEROOT_EMAIL_TOKEN_SECRET": ("smtp", "emailTokenSecret", "email-token-secret"),
    "TRACEROOT_SIGNUP_SLACK_WEBHOOK_URL": ("slack", "signupWebhookUrl", "signup-webhook-url"),
}


def test_key_names_match_the_secret_fields():
    for section, key, field in KEYS.values():
        assert _values()[section]["keys"][key] == field


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestRendered:
    _render = staticmethod(_Rendered._env_by_deployment)

    def _with_secrets(self):
        return self._render(
            "--set", "smtp.existingSecret=traceroot-smtp", "--set", "slack.existingSecret=traceroot-slack"
        )

    def test_web_reads_each_key_optionally(self):
        web = next(env for name, env in self._with_secrets().items() if name.endswith("-web"))
        for var, (section, _, field) in KEYS.items():
            ref = web[var]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": f"traceroot-{section}", "key": field, "optional": True}

    def test_only_web_gets_the_keys(self):
        for var in KEYS:
            holders = [name for name, env in self._with_secrets().items() if var in env]
            assert len(holders) == 1 and holders[0].endswith("-web")

    def test_no_secrets_means_no_keys(self):
        assert all(var not in env for env in self._render().values() for var in KEYS)
