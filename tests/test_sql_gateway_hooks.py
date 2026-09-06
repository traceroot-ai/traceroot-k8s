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


@pytest.mark.parametrize("name", [_PROVISION, _MIGRATE, _VERIFY])
def test_retention_is_bounded(name):
    """Retained Jobs need a TTL, or an uninstalled release keeps them forever."""
    text = _template_text(name)
    assert "ttlSecondsAfterFinished" in text
    assert ".Values.migrations.retainFinishedSeconds" in text, "should use the shared value"


def test_retention_default_is_positive_and_tunable():
    assert _values()["migrations"]["retainFinishedSeconds"] > 0


def test_readonly_client_sends_no_per_query_settings():
    """A readonly = 1 user cannot set them; the server rejects the query outright."""
    text = _template_text(_VERIFY)
    ro = text[text.index("CH_RO=("):]
    ro = ro[: ro.index(")")]
    for setting in ("max_execution_time", "max_result_rows", "max_result_bytes", "max_memory_usage"):
        assert setting not in ro, "%s cannot be set by the read-only user" % setting




def test_passwords_are_not_embedded_as_sql_literals():
    """A quote or backslash in a secret would otherwise alter the DDL."""
    text = _template_text(_PROVISION)
    assert "sha256_hash" in text
    assert "sha256_password" not in text
    for var in ("SQL_GATEWAY_WRITER_PASSWORD", "SQL_GATEWAY_RO_PASSWORD"):
        assert "BY '${%s}'" % var not in text


@pytest.mark.parametrize("name", [_PROVISION, _VERIFY])
def test_client_commands_are_arrays(name):
    """A string command re-splits a password containing whitespace or a glob."""
    text = _template_text(name)
    assert "[@]}\" --query" in text or '[@]}" --query' in text
    assert '--password ${' not in text, "unquoted password interpolation"


def _run_block(script: str, block: str, stub: str, cmd_var: str) -> str:
    """Execute one extracted block of the rendered job with a stubbed client."""
    harness = "\n".join([
        "set -euo pipefail",
        "FAILED=0",
        "stub() { %s }" % stub,
        "%s=(stub)" % cmd_var,
        block,
        'echo "FAILED=$FAILED"',
    ])
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    return out.stdout + out.stderr


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

    def _verify_script(self) -> str:
        for d in self._render("--set", "sqlGateway.enabled=true"):
            if d.get("metadata", {}).get("name", "").endswith("verify-clickhouse-sql-gateway"):
                return d["spec"]["template"]["spec"]["containers"][0]["args"][0]
        raise AssertionError("verification job did not render")

    @staticmethod
    def _block(script: str, start: str) -> str:
        """The `for ... done` loop beginning at the line containing `start`."""
        lines = script.splitlines()
        i = next(n for n, l in enumerate(lines) if start in l)
        i = next(n for n in range(i, len(lines)) if lines[n].strip().startswith("for "))
        j = next(n for n in range(i, len(lines)) if lines[n].strip() == "done")
        return "\n".join(lines[i:j + 1])

    # The property the whole Job exists to prove: a probe that merely failed is not
    # evidence of denial. Executed, not pattern-matched -- a source-text assertion
    # here passes against a probe that reports every failure as success.
    @pytest.mark.parametrize(
        "name,stub,expect_failed",
        [
            ("real denial", 'echo "Code: 497. DB::Exception: ACCESS_DENIED" >&2; return 241;', 0),
            ("timeout", 'echo "Timeout exceeded while reading from socket" >&2; return 159;', 1),
            ("bad password", 'echo "Authentication failed" >&2; return 4;', 1),
            ("table readable", 'echo 0; return 0;', 1),
        ],
    )
    def test_only_access_denied_counts_as_denial(self, name, stub, expect_failed):
        block = self._block(self._verify_script(), "must never reach the physical tables")
        out = _run_block(self._verify_script(), block, stub, "CH_RO")
        assert "FAILED=%d" % expect_failed in out, "%s: %s" % (name, out)

    @pytest.mark.parametrize(
        "definer,expect_failed",
        [
            ("sql_gateway_writer", 0),
            # A longer account containing the expected name must not pass.
            ("sql_gateway_writer_admin", 1),
            ("default", 1),
        ],
    )
    def test_definer_check_is_not_a_prefix_match(self, definer, expect_failed):
        block = self._block(self._verify_script(), "isolation would rest on nothing")
        stub = (
            'echo "CREATE VIEW default.v DEFINER = %s SQL SECURITY DEFINER"; echo "AS SELECT 1"; return 0;'
            % definer
        )
        out = _run_block(self._verify_script(), block, stub, "CH_ADMIN")
        assert "FAILED=%d" % expect_failed in out, "%s: %s" % (definer, out)

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
