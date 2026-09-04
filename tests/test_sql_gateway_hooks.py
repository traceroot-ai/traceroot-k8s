"""Chart guard: the SQL-gateway hooks stay ordered, gated, and readable.

The gateway's isolation rests on the curated views being owned by a dedicated
writer, and on the read-only user holding no grant on the physical tables. Three
things about the chart keep that true, and all three break quietly:

* the provisioning hook must be weighted below the ClickHouse migration. The
  migration creates the views with ``DEFINER = <writer>``, which fails outright
  if the writer does not exist yet — so a reweight turns every upgrade red;
* the hooks must not carry ``hook-succeeded``. Helm deletes the Job and its pod
  the moment the run succeeds, and engineers holding read-only cluster access
  cannot query ClickHouse to find out what happened instead;
* none of it may render unless ``sqlGateway.enabled``. Enabling creates database
  users and needs the ClickHouse admin to hold access management, which an
  installation that does not serve the gateway should not be given.

The structural checks run unconditionally; the render checks run when Helm is
installed.
"""

import os
import shutil
import subprocess

import pytest
import yaml

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CHART = os.path.join(_ROOT, "charts", "traceroot")
_VALUES = os.path.join(_CHART, "values.yaml")
_MIGRATIONS = os.path.join(_CHART, "templates", "migrations")

_PROVISION = "provision-clickhouse-users"
_MIGRATE = "migrate-clickhouse"
_VERIFY = "verify-clickhouse-sql-gateway"


def _values() -> dict:
    with open(_VALUES) as f:
        return yaml.safe_load(f)


def _template_text(name: str) -> str:
    with open(os.path.join(_MIGRATIONS, f"{name}.yaml")) as f:
        return f.read()


def test_gateway_is_off_by_default():
    assert _values()["sqlGateway"]["enabled"] is False


def test_gateway_templates_are_gated_on_the_flag():
    for name in (_PROVISION, _VERIFY):
        assert "{{- if" in _template_text(name).splitlines()[0]
        assert ".Values.sqlGateway.enabled" in _template_text(name)


def test_admin_access_management_is_not_granted_by_default():
    """Creating users needs it; an install without the gateway should not have it."""
    assert "usersExtraOverrides" not in (_values()["clickhouse"] or {})


def test_provisioning_runs_before_the_migration_and_verification_after():
    def weight(name: str) -> int:
        text = _template_text(name)
        line = next(line for line in text.splitlines() if "hook-weight" in line)
        return int(line.split(":")[-1].strip().strip('"'))

    assert weight(_PROVISION) < weight(_MIGRATE) < weight(_VERIFY)


def test_hooks_share_a_phase_with_the_migration():
    """Weights only order hooks within the same phase."""
    def phase(name: str) -> str:
        line = next(l for l in _template_text(name).splitlines() if '"helm.sh/hook":' in l)
        return line.split(":", 1)[1].strip()

    assert phase(_PROVISION) == phase(_MIGRATE) == phase(_VERIFY)


@pytest.mark.parametrize("name", [_PROVISION, _MIGRATE, _VERIFY])
def test_successful_runs_are_not_deleted(name):
    line = next(l for l in _template_text(name).splitlines() if "hook-delete-policy" in l)
    assert "hook-succeeded" not in line
    assert "before-hook-creation" in line, "needed to recreate an immutable Job"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestRendered:
    @staticmethod
    def _render(*extra: str) -> list:
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com", *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [d for d in yaml.safe_load_all(out) if d]

    @staticmethod
    def _names(docs) -> set:
        return {d.get("metadata", {}).get("name", "") for d in docs}

    def test_nothing_gateway_renders_when_disabled(self):
        names = self._names(self._render())
        assert not [n for n in names if _PROVISION in n or _VERIFY in n]

    def test_both_jobs_render_when_enabled(self):
        names = self._names(self._render("--set", "sqlGateway.enabled=true"))
        assert [n for n in names if _PROVISION in n]
        assert [n for n in names if _VERIFY in n]

    def test_readonly_credentials_reach_rest_only_when_enabled(self):
        def ro_env(docs):
            found = set()
            for d in docs:
                if d.get("kind") != "Deployment":
                    continue
                for c in d["spec"]["template"]["spec"]["containers"]:
                    for e in c.get("env", []):
                        if e["name"].startswith("CLICKHOUSE_RO_"):
                            found.add(d["metadata"]["name"])
            return found

        assert ro_env(self._render()) == set()
        assert ro_env(self._render("--set", "sqlGateway.enabled=true")) == {"traceroot-rest"}

    def test_readonly_user_never_gets_the_admin_password(self):
        """The gateway user's whole purpose is not being the admin."""
        for d in self._render("--set", "sqlGateway.enabled=true"):
            if d.get("kind") != "Deployment":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                env = {e["name"]: e for e in c.get("env", [])}
                if "CLICKHOUSE_RO_PASSWORD" not in env:
                    continue
                ro_key = env["CLICKHOUSE_RO_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
                admin_key = env["CLICKHOUSE_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
                assert ro_key != admin_key
