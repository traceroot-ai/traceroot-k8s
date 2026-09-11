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
import re
import shutil
import tempfile
import subprocess

import pytest
import yaml

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CHART = os.path.join(_ROOT, "charts", "traceroot")
_VALUES = os.path.join(_CHART, "values.yaml")
_MIGRATIONS = os.path.join(_CHART, "templates", "migrations")

_PROVISION = "provision-clickhouse-users"
_MIGRATE = "migrate-clickhouse"
_MIGRATE_PG = "migrate-postgres"
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


# Every channel through which the chart could widen the ClickHouse admin. It has no
# reason to: the bundled image already grants what provisioning needs, and setting
# any of these would force a ClickHouse pod restart.
_ACCESS_MANAGEMENT_KEYS = (
    "usersExtraOverrides",
    "usersExtraOverridesConfigmap",
    "usersExtraOverridesSecret",
)


def test_admin_access_management_is_not_granted_by_default():
    """Creating users needs it; an install without the gateway should not have it."""
    ch = _values()["clickhouse"] or {}
    for key in _ACCESS_MANAGEMENT_KEYS:
        assert not ch.get(key), "%s would grant it by default" % key
    # Any other override channel could carry the grant too; assert none mentions it.
    for key in ("defaultConfigurationOverrides", "extraOverrides", "configuration"):
        assert "access_management" not in str(ch.get(key) or "")


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


@pytest.mark.parametrize("name", [_PROVISION, _MIGRATE, _MIGRATE_PG, _VERIFY])
def test_successful_runs_are_not_deleted(name):
    line = next(l for l in _template_text(name).splitlines() if "hook-delete-policy" in l)
    assert "hook-succeeded" not in line
    assert "before-hook-creation" in line, "needed to recreate an immutable Job"


@pytest.mark.parametrize("name", [_PROVISION, _MIGRATE, _MIGRATE_PG, _VERIFY])
def test_retention_comes_from_the_shared_value(name):
    """Whether inline or via the shared helper, retention must not be hardcoded."""
    text = _template_text(name)
    assert "ttlSecondsAfterFinished" in text or "traceroot.migrations.ttl" in text
    assert ".Values.migrations.retainFinishedSeconds" in text or "traceroot.migrations.ttl" in text


def test_retention_rejects_values_kubernetes_cannot_store():
    """ttlSecondsAfterFinished is int32; a larger value renders fine and is rejected
    by the API server mid-upgrade instead."""
    helper = open(os.path.join(_CHART, "templates", "_helpers.tpl")).read()
    block = helper[helper.index("traceroot.migrations.ttl"):]
    assert "2147483647" in block, "no int32 upper bound on the retention value"


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
    assert '[@]}" --query' in text, "client must be invoked as an array"
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert "--password" not in code, "password must not reach argv"


def _run_block(script: str, block: str, stub: str, cmd_var: str) -> str:
    """Execute one extracted block of the rendered job with a stubbed client."""
    harness = "\n".join([
        "set -euo pipefail",
        "FAILED=0",
        'CLICKHOUSE_PASSWORD=x; SQL_GATEWAY_RO_PASSWORD=x; HOST=h',
        "stub() { %s }" % stub,
        "%s=(stub)" % cmd_var,
        block,
        'echo "FAILED=$FAILED"',
    ])
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    return out.stdout + out.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestRendered:
    # Enabling the gateway now requires the admin access-management override, so
    # every "enabled" render supplies it. Its absence is asserted separately.
    # A fully enabled gateway. `verify` is opted into explicitly because the chart
    # defaults it off: the hook runs at pre-upgrade, so leaving it on by default would
    # let gateway drift fail a release that changed nothing about the gateway.
    ENABLED = (
        "--set",
        "sqlGateway.enabled=true",
        "--set",
        "sqlGateway.verify=true",
        "--set",
        "clickhouse.usersExtraOverrides=x",
    )
    ENABLED_NO_VERIFY = (
        "--set",
        "sqlGateway.enabled=true",
        "--set",
        "clickhouse.usersExtraOverrides=x",
    )

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
        names = self._names(self._render(*self.ENABLED))
        assert [n for n in names if _PROVISION in n]
        assert [n for n in names if _VERIFY in n]

    def test_one_database_reaches_every_consumer(self):
        """Grants, views and the app must all name the same database.

        The migrate Job creates the definer-owned views, the provisioning hook grants on
        them, the verify hook probes them, and the Deployments read them. A consumer left
        on a hardcoded `default` does not fail: the grants simply land on a different
        database from the views, and the gateway returns nothing with no error anywhere.
        """
        out = subprocess.run(
            [
                "helm", "template", "traceroot", _CHART,
                "--set", "ingress.host=example.com",
                "--set", "sqlGateway.enabled=true",
                "--set", "sqlGateway.verify=true",
                "--set", "clickhouse.usersExtraOverrides=x",
                "--set", "clickhouse.database=analytics",
            ],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        rendered = out.stdout
        assert "GRANT SELECT ON analytics." in rendered, "provisioning grants on the wrong database"
        assert "SHOW CREATE VIEW analytics." in rendered, "verify probes the wrong database"
        assert "database = 'analytics'" in rendered, "the orphan-grant scan is still on default"
        assert rendered.count('value: "analytics"') >= 4, "a Deployment or the migrate Job is hardcoded"
        assert "9000/default" not in rendered, "the migration DSN is hardcoded to default"

    def test_migration_dsn_never_reaches_argv(self):
        """`/proc/<pid>/cmdline` is world-readable; `/proc/<pid>/environ` is not.

        The migration runs as the one account holding access management, so its password
        landing in a process argument is an escalation path for anyone who can read the
        host's process list while the Job runs.
        """
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com"],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        assert not re.search(r"goose[^\n]*clickhouse://", out.stdout), (
            "the DSN is passed to goose as an argument"
        )
        assert "GOOSE_DBSTRING=" in out.stdout, "the DSN should travel in the environment"

    def test_verify_job_is_off_unless_asked_for(self):
        """`verify` defaults to false, and enabling the gateway must not turn it on.

        The hook is annotated pre-upgrade, so a gateway drift would otherwise fail the
        whole release and leave Helm in pending-upgrade, including on an upgrade that
        touched nothing in the gateway. Provisioning must still render.
        """
        names = self._names(self._render(*self.ENABLED_NO_VERIFY))
        assert [n for n in names if _PROVISION in n], "provisioning must still render"
        assert not [n for n in names if _VERIFY in n], (
            "verify must stay off until explicitly enabled"
        )

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
        assert ro_env(self._render(*self.ENABLED)) == {"traceroot-rest"}

    def _verify_script(self) -> str:
        for d in self._render(*self.ENABLED):
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
        checked = 0
        for d in self._render(*self.ENABLED):
            if d.get("kind") != "Deployment":
                continue
            for c in d["spec"]["template"]["spec"]["containers"]:
                env = {e["name"]: e for e in c.get("env", [])}
                if "CLICKHOUSE_RO_PASSWORD" not in env:
                    continue
                ro_key = env["CLICKHOUSE_RO_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
                admin_key = env["CLICKHOUSE_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
                assert ro_key != admin_key
                checked += 1
        assert checked, "no container carried the read-only password; nothing was asserted"


    def test_every_retained_job_renders_a_ttl(self):
        """Asserted on the rendered output, since the value reaches the Jobs by
        two different routes and only the result is what Kubernetes sees."""
        found = {}
        for d in self._render(*self.ENABLED):
            if d.get("kind") != "Job":
                continue
            found[d["metadata"]["name"]] = d["spec"].get("ttlSecondsAfterFinished")
        assert len(found) == 4, "expected four hook Jobs, got %s" % sorted(found)
        for name, ttl in found.items():
            assert isinstance(ttl, int) and ttl > 0, "%s has no bounded TTL: %r" % (name, ttl)

    def test_orphan_scan_covers_the_physical_tables_not_just_the_views(self):
        """A stale writer holds SELECT on the raw tables, which is the worse leftover
        and the one a view-only scan would never surface."""
        script = self._verify_script()
        block = script[script.index("system.grants"):]
        block = block[: block.index('"')]
        assert "spans_public_v1" in block and "traces_public_v1" in block
        assert "'spans', 'traces'" in block, "physical tables are not scanned"

    def test_verify_toggle_actually_gates_the_verification_job(self):
        names = self._names(self._render(*self.ENABLED, "--set", "sqlGateway.verify=false"))
        assert not [n for n in names if _VERIFY in n], "verify=false still rendered the Job"
        assert [n for n in names if _PROVISION in n], "provisioning should be unaffected"

    def test_rendered_job_scripts_are_valid_shell(self):
        """A templating change producing unbalanced quotes would otherwise only show in-cluster."""
        checked = 0
        for d in self._render(*self.ENABLED):
            name = d.get("metadata", {}).get("name", "")
            if not (name.endswith(_PROVISION) or name.endswith(_VERIFY)):
                continue
            script = d["spec"]["template"]["spec"]["containers"][0]["args"][0]
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                fh.write(script)
            try:
                out = subprocess.run(["bash", "-n", fh.name], capture_output=True, text=True)
                assert out.returncode == 0, "%s: %s" % (name, out.stderr)
            finally:
                os.unlink(fh.name)
            checked += 1
        assert checked == 2, "expected both gateway job scripts, checked %d" % checked

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize(
    "override,expect",
    [
        ({"sqlGateway.writerUser": "sql-gateway-writer"}, "writerUser must match"),
        ({"sqlGateway.readonlyUser": 'ro" --user default --password oops "x'}, "readonlyUser must match"),
        ({"sqlGateway.settingsProfile": "1profile"}, "settingsProfile must match"),
    ],
)
def test_identifiers_that_would_break_sql_or_the_shell_are_refused(override, expect):
    """These values are spliced into DDL and into a client command line."""
    args = ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com",
            "--set", "sqlGateway.enabled=true", "--set", "clickhouse.usersExtraOverrides=x"]
    for k, v in override.items():
        args += ["--set", "%s=%s" % (k, v)]
    out = subprocess.run(args, capture_output=True, text=True)
    assert out.returncode != 0 and expect in out.stderr, out.stderr
