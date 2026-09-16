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
* the writer must be provisioned on every install, because the migration creates
  the views with ``DEFINER = <writer>`` whether or not the gateway serves traffic,
  and a missing definer fails the migration and with it the release;
* everything the gateway alone needs -- the read-only user, its profile, its grants
  on the views, and the credentials rest receives -- must stay behind
  ``sqlGateway.enabled``, because that account is what customer SQL runs as.

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


def test_verification_is_gated_on_the_flag():
    assert "{{- if" in _template_text(_VERIFY).splitlines()[0]
    assert ".Values.sqlGateway.enabled" in _template_text(_VERIFY)


def test_provisioning_is_not_gated_but_its_readonly_half_is():
    """The writer is unconditional; the account customer SQL runs as is not.

    A top-level gate here is the regression this guards: it would leave every
    gateway-off install without the definer the ClickHouse migration requires.
    """
    text = _template_text(_PROVISION)
    assert "{{- if" not in text.splitlines()[0], (
        "gating the whole template leaves migration 012 without its definer"
    )
    assert ".Values.sqlGateway.enabled" in text, "the read-only half must still be gated"


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


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize(
    "value,expect",
    [
        ("-1", "must not be negative"),
        ("1.5", "whole number"),
        ("2147483648", "must fit in int32"),
        ("notanumber", "whole number of seconds or null"),
    ],
)
def test_retention_rejects_values_kubernetes_cannot_store(value, expect):
    """ttlSecondsAfterFinished is int32. Rendered rather than asserted on the helper's
    text: an out-of-range value otherwise renders fine and is refused by the API server
    in the middle of a pre-upgrade hook, where the error names none of this."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("migrations:\n  retainFinishedSeconds: %s\n" % value)
        path = f.name
    try:
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com", "-f", path],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(path)
    assert out.returncode != 0, "%s rendered instead of being refused" % value
    assert expect in out.stderr, "%s: %s" % (value, out.stderr[-400:])


def test_retention_default_is_positive_and_tunable():
    assert _values()["migrations"]["retainFinishedSeconds"] > 0


def _caps_probe(text: str) -> str:
    """The `if RAISED=...` block that proves a per-query override is refused."""
    start = text.find("if RAISED=$(")
    if start < 0:
        return ""
    end = text.find("\n              fi\n", start)
    return text[start : end + len("\n              fi\n")] if end > 0 else ""


def test_the_readonly_client_never_sets_a_per_query_setting():
    """A readonly = 1 user cannot set them; the server rejects the query outright.

    Reading them back with getSetting is how the caps are verified, so the ban is on
    *setting* one: a `--max_memory_usage 100` on the invocation line, or a SETTINGS
    clause on a probe that is expected to succeed. The one SETTINGS clause in the hook
    is the override that must be refused, asserted separately below.
    """
    text = _template_text(_VERIFY)
    for setting in ("max_execution_time", "max_result_rows", "max_result_bytes", "max_memory_usage"):
        assert "--%s" % setting not in text, "%s must not be passed on the client command line" % setting
    probe = _caps_probe(text)
    assert probe, "the caps probe is missing; without it a detached profile goes unnoticed"
    assert text.count("SETTINGS ") == text.replace(probe, "").count("SETTINGS ") + 1, (
        "the only SETTINGS clause must be the override the probe requires to be refused"
    )


def test_the_caps_probe_treats_a_successful_override_as_a_failure():
    """The probe is inverted: if the override is ACCEPTED the caps are gone.

    Written the wrong way round it would report OK exactly when the profile had
    been detached, which is the one case it exists to catch.
    """
    probe = _caps_probe(_template_text(_VERIFY))
    accepted = probe[: probe.find("elif")]
    assert "FAILED=1" in accepted, "an accepted override must fail the release"
    assert "Code: 164" in probe or "READONLY" in probe, (
        "the refusal must be matched on the server's code, not on any failure"
    )
    assert probe.count("FAILED=1") >= 2, (
        "a refusal for an unexpected reason leaves the cap unproven and must also fail"
    )


def test_every_configured_cap_is_verified_not_just_readonly():
    """Every other check passes with the profile detached, and readonly alone passes
    with a cap widened: readonly = 1 refuses any override whatever the values are."""
    text = _template_text(_VERIFY)
    assert "CAPS_EXPECTED" in text and '[ "$CAPS" != "$CAPS_EXPECTED" ]' in text
    for cap in ("readonly", "max_execution_time", "max_result_rows", "max_result_bytes", "max_memory_usage"):
        assert "getSetting('%s')" % cap in text, "%s is never read back" % cap
    for limit in ("maxExecutionTime", "maxResultRows", "maxResultBytes", "maxMemoryUsage"):
        assert '"name" "%s"' % limit in text, "%s is not compared against the server" % limit
    # Through the helper, which renders an integer. A large float64 renders in exponent
    # form and would never match what the server reports, so the check would fail on a
    # correctly configured cluster.
    assert text.count('include "traceroot.sqlGateway.limit"') == 4


def test_passwords_are_not_embedded_as_sql_literals():
    """A quote or backslash in a secret would otherwise alter the DDL."""
    text = _template_text(_PROVISION)
    assert "sha256_hash" in text
    assert "sha256_password" not in text
    for var in ("SQL_GATEWAY_WRITER_PASSWORD", "SQL_GATEWAY_RO_PASSWORD"):
        assert "BY '${%s}'" % var not in text
        assert "BY '${%s:-}'" % var not in text
    assert "${WRITER_HASH}" in text and "${RO_HASH}" in text


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
    # No access-management override here: the bundled image's admin already holds
    # CREATE USER and SET DEFINER, verified directly against
    # bitnamilegacy/clickhouse:25.2.1-debian-12-r0. Supplying one in the fixture would
    # imply the gateway needs it.
    # A fully enabled gateway. `verify` is opted into explicitly because the chart
    # defaults it off: the hook runs at pre-upgrade, so leaving it on by default would
    # let gateway drift fail a release that changed nothing about the gateway.
    ENABLED = (
        "--set",
        "sqlGateway.enabled=true",
        "--set",
        "sqlGateway.verify=true",
    )
    ENABLED_NO_VERIFY = ("--set", "sqlGateway.enabled=true")

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

    def test_only_the_writer_is_provisioned_when_disabled(self):
        """The migration creates definer-owned views on every install, so the definer exists on every install."""
        names = self._names(self._render())
        assert [n for n in names if _PROVISION in n], (
            "without this Job, migration 012 has no definer and the release fails"
        )
        assert not [n for n in names if _VERIFY in n]

        script = self._provision_script()
        assert "CREATE USER IF NOT EXISTS sql_gateway_writer" in script
        assert "GRANT SELECT ON default.spans  TO sql_gateway_writer" in script
        for absent in (
            "sql_gateway_ro",
            "sql_readonly_profile",
            "spans_public_v1",
            "traces_public_v1",
            "SQL_GATEWAY_RO_PASSWORD",
        ):
            assert absent not in script, (
                "%s belongs to the gateway and must not be provisioned when it is off" % absent
            )

    def test_no_writer_password_is_ever_read_or_stored(self):
        """Nothing authenticates as the writer, so a stored password buys nothing.

        The curated views are SQL SECURITY DEFINER: the body runs under the writer's
        grants and its password is never presented. Keeping one in a Secret would be a
        standing credential able to read every project's raw rows, including the
        columns the curated views deliberately omit. It is generated and discarded.
        """
        text = _template_text(_PROVISION)
        assert "SQL_GATEWAY_WRITER_PASSWORD" not in text, (
            "the writer password must not be read from the environment"
        )
        assert "writerPassword" not in text, "no Secret key should be referenced for the writer"
        assert "/dev/urandom" in text, "the writer password must be generated"

        for enabled in ([], ["--set", "sqlGateway.enabled=true"]):
            env = {
                e["name"]
                for d in self._render(*enabled)
                if _PROVISION in d.get("metadata", {}).get("name", "")
                for e in d["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            assert "SQL_GATEWAY_WRITER_PASSWORD" not in env, (
                "enabled=%s still mounts a writer password" % bool(enabled)
            )

    def test_the_writer_is_left_alone_when_it_already_exists(self):
        """So an operator who provisions it themselves needs no privilege to create users.

        Without this, the hook fails the release on any server whose admin cannot
        create users, even when the account it wanted is already there.
        """
        script = self._provision_script()
        assert "SELECT count() FROM system.users WHERE name = 'sql_gateway_writer'" in script
        assert 'if [ -n "$NEED_CREATE" ]; then' in script
        assert "already exists; leaving its password untouched" in script

    def test_the_privilege_precheck_is_advisory_not_fatal(self):
        """An admin holding these through a granted role prints only the role.

        A literal scan cannot see that, and failing on it aborts the release for a
        server that provisions perfectly well. The verdict belongs to the statement
        that actually runs.
        """
        script = self._provision_script()
        start = script.index('for NEEDED in "CREATE USER" "SET DEFINER"')
        block = script[start : script.index("done", start)]
        assert "exit 1" not in block, "the precheck must not fail the release"
        assert "FAIL" not in block, "the precheck must not report a failure it cannot know"
        assert "note:" in block
        # and the statement that can know carries the guidance the precheck used to
        assert "could not create sql_gateway_writer" in script
        assert "usersExtraOverrides" in script

    def test_a_missing_database_fails_before_the_grants_land_on_nothing(self):
        """GRANT on a database that does not exist succeeds and is recorded.

        The migration then fails with UNKNOWN_DATABASE, several steps away from the
        mistyped value that caused it.
        """
        script = self._provision_script()
        assert "FROM system.databases WHERE name = 'default'" in script
        assert "clickhouse.database is set to 'default', which does not exist" in script

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

    def test_view_probes_pass_the_time_range(self):
        """Every call the verify hook makes to a public view must pass start_time and end_time.

        The views declare a half-open time range alongside the project. A declared
        parameter the caller omits fails with Code 456 rather than reading unbounded, so a
        probe that passes only project_id breaks verification the moment the views gain
        the range. Matched on the call shape, not the view name, because the hook builds
        the name from a shell variable.
        """
        script = self._verify_script()
        # Balanced scan rather than a regex: the bounds themselves contain parentheses,
        # so `[^)]*` would stop inside toDateTime64(...) and never see end_time.
        calls = []
        for m in re.finditer(r"\(project_id\s*=", script):
            depth, i = 0, m.start()
            while i < len(script):
                depth += {"(": 1, ")": -1}.get(script[i], 0)
                if depth == 0:
                    break
                i += 1
            calls.append(script[m.start() : i + 1])
        assert calls, "expected the verify hook to call at least one public view"
        for call in calls:
            assert "start_time" in call and "end_time" in call, (
                f"view call omits the time range and would fail with Code 456: {call}"
            )

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
        # Every goose call must rely on the exported config. A leftover positional
        # driver/DSN pair reads an unset variable and connects to nothing, and the
        # status call is deliberately allowed to fail, so it would break in silence.
        for call in re.findall(r"goose -dir /migrations[^\n|]*", out.stdout):
            assert "clickhouse" not in call, f"stale positional driver/DSN in: {call.strip()}"
            assert "$DSN" not in call, f"stale DSN reference in: {call.strip()}"

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

    def _provision_script(self, *extra: str) -> str:
        for d in self._render(*extra):
            if _PROVISION in d.get("metadata", {}).get("name", ""):
                return d["spec"]["template"]["spec"]["containers"][0]["args"][0]
        raise AssertionError("provisioning job did not render")

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

    def test_a_large_retention_still_renders_an_integer_for_every_job(self):
        """A values FILE delivers the number as float64, and Go renders a large
        float64 in exponent form. `2.592e+06` is not an integer and `1e+06` is not
        even a number to a YAML parser, so the API server rejects the Job in the
        middle of a pre-upgrade hook. The default (604800) happens to render
        cleanly, which is why only a larger value exercises this.

        `--set` cannot reproduce it: it delivers int64.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("migrations:\n  retainFinishedSeconds: 2592000\n")
            path = f.name
        try:
            found = {
                d["metadata"]["name"]: d["spec"].get("ttlSecondsAfterFinished")
                for d in self._render(*self.ENABLED, "-f", path)
                if d.get("kind") == "Job"
            }
        finally:
            os.unlink(path)
        assert len(found) == 4, "expected four hook Jobs, got %s" % sorted(found)
        for name, ttl in found.items():
            assert isinstance(ttl, int), (
                "%s rendered %r, which Kubernetes cannot store in an int32 field" % (name, ttl)
            )
            assert ttl == 2592000, "%s lost the configured value: %r" % (name, ttl)

    def test_provisioning_grants_exactly_the_four_expected_selects(self):
        """The suite's headline claim: the read-only user holds no grant on the raw tables.

        Every check elsewhere runs against a stubbed client and proves the hook's
        branching, never what provisioning actually grants. Adding
        `GRANT SELECT ON <db>.spans TO <ro>` to the template passed the whole suite.
        """
        script = self._provision_script(*self.ENABLED)
        granted = set(re.findall(r"GRANT SELECT ON (\S+)\s+TO (\S+?);", script))
        assert granted == {
            ("default.spans", "sql_gateway_writer"),
            ("default.traces", "sql_gateway_writer"),
            ("default.spans_public_v1", "sql_gateway_ro"),
            ("default.traces_public_v1", "sql_gateway_ro"),
        }, "provisioning grants changed: %s" % sorted(granted)

    def test_the_readonly_account_is_given_the_capped_profile(self):
        """Dropping CONST, or the SETTINGS PROFILE clause, left the suite green.

        The profile is what bounds one query's rows, time and memory, so an account
        created without it is uncapped while looking identical everywhere else.
        """
        script = self._provision_script(*self.ENABLED)
        for cap, value in (
            ("max_execution_time", 30),
            ("max_result_rows", 100000),
            ("max_result_bytes", 536870912),
            ("max_memory_usage", 4294967296),
        ):
            # Once in CREATE and once in ALTER, each carrying CONST and the configured
            # value. A bare count of CONST tokens passes even when one cap has none.
            assert script.count("%s = %d CONST" % (cap, value)) == 2, (
                "%s is not capped at %d with CONST in both statements" % (cap, value)
            )
            assert "e+" not in script[script.index(cap) : script.index(cap) + 60], (
                "%s rendered in exponent form" % cap
            )
        assert "readonly = 1" in script
        assert script.count("SETTINGS PROFILE 'sql_readonly_profile'") == 2, (
            "the read-only user must carry the profile on both CREATE and ALTER"
        )
        assert "ALTER SETTINGS PROFILE sql_readonly_profile" in script, (
            "without the ALTER an existing profile keeps its old, wider caps"
        )

    def test_the_probes_name_the_right_tables_and_run_as_the_right_user(self):
        """Inverting the denial loop to the views, or swapping CH_RO for CH_ADMIN, was green.

        The execution tests stub the client, so nothing read the query text; both
        mutations turn the hook into one that asserts the opposite of the design.
        """
        script = self._verify_script()
        assert "for TABLE in spans traces; do" in script, (
            "the denial loop must name the physical tables, not the curated views"
        )
        denial = script[script.index("for TABLE in spans traces"):]
        denial = denial[: denial.index("done")]
        assert '"${CH_RO[@]}"' in denial and "CH_ADMIN" not in denial, (
            "denial must be probed as the read-only user"
        )
        read = script[script.index("for VIEW in spans_public_v1 traces_public_v1; do", script.index("curated views must stay readable")):]
        read = read[: read.index("done")]
        assert '"${CH_RO[@]}"' in read and "CH_ADMIN" not in read, (
            "the read probe must run as the read-only user or it proves nothing"
        )
        assert '--user "sql_gateway_ro"' in script, "CH_RO must authenticate as the read-only account"

    @pytest.mark.parametrize(
        "override,expect",
        [
            ("sqlGateway.readonlyUser=sql_gateway_writer", "must be different accounts"),
            ("sqlGateway.writerUser=default", "must not be the ClickHouse admin"),
            ("sqlGateway.readonlyUser=default", "must not be the ClickHouse admin"),
        ],
    )
    def test_collapsing_two_identities_into_one_is_refused(self, override, expect):
        """One account holding the writer's SELECT on the physical tables and the
        password handed to customer SQL is the isolation removed. The hook would
        carry it out and report success, so it is refused at render time."""
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com",
             "--set", "sqlGateway.enabled=true", "--set", override],
            capture_output=True, text=True,
        )
        assert out.returncode != 0, "%s rendered instead of being refused" % override
        assert expect in out.stderr, out.stderr[-300:]

    def test_an_admin_username_that_breaks_out_of_the_script_is_refused(self):
        """It is spliced into a shell command line and into SQL string literals."""
        for bad in ('ad"min', "ad'min", "ad min", "ad;min"):
            out = subprocess.run(
                ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com",
                 "--set", "clickhouse.auth.username=%s" % bad],
                capture_output=True, text=True,
            )
            assert out.returncode != 0, "%r rendered" % bad
            assert "clickhouse.auth.username must match" in out.stderr

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_cap_of_zero_is_refused(self, value):
        """ClickHouse reads 0 as no limit, so a zero cap removes the bound rather than
        tightening it, and the hook would provision it and report success."""
        out = subprocess.run(
            ["helm", "template", "traceroot", _CHART, "--set", "ingress.host=example.com",
             "--set", "sqlGateway.enabled=true", "--set", "sqlGateway.limits.maxResultRows=%s" % value],
            capture_output=True, text=True,
        )
        assert out.returncode != 0, "a cap of %s rendered" % value
        assert "must be greater than zero" in out.stderr

    @pytest.mark.parametrize("var", ["${WRITER_HASH}", "${RO_HASH}"])
    def test_no_password_hash_reaches_the_command_line(self, var):
        """/proc/<pid>/cmdline is readable by anything that can see the process.

        The hashes are not the passwords, but they need not be there at all:
        clickhouse-client reads statements from stdin just as well. Asserted by
        finding, for each statement carrying a hash, whether the nearest preceding
        invocation is a pipe or a --query argument.
        """
        script = self._provision_script(*self.ENABLED)
        idx = script.index(var)
        piped = script.rfind("printf", 0, idx)
        argv = script.rfind("--query", 0, idx)
        assert piped > argv, "%s reaches the client through --query, so it lands in argv" % var

    def test_a_numeric_secret_key_stays_a_string(self):
        """secretKeyRef.key is a string field; an unquoted numeric value renders as a
        number and the API server refuses the Job in the middle of the upgrade.

        Scoped to the hook Jobs this stack owns. The Deployments have the same shape
        throughout the chart and are left for a change that can cover them all.
        """
        for d in self._render("--set", "clickhouse.auth.existingSecretKey=12345",
                              "--set", "sqlGateway.enabled=true"):
            if d.get("kind") != "Job":
                continue
            for e in d["spec"]["template"]["spec"]["containers"][0].get("env", []):
                key = e.get("valueFrom", {}).get("secretKeyRef", {}).get("key")
                if key is not None:
                    assert isinstance(key, str), "%s rendered %r" % (e["name"], key)

    def test_orphan_scan_covers_the_physical_tables_not_just_the_views(self):
        """A stale writer holds SELECT on the raw tables, which is the worse leftover
        and the one a view-only scan would never surface."""
        script = self._verify_script()
        block = script[script.index("system.grants"):]
        block = block[: block.index('"')]
        assert "spans_public_v1" in block and "traces_public_v1" in block
        assert "'spans', 'traces'" in block, "physical tables are not scanned"

    @pytest.mark.parametrize(
        "name,stub,expect",
        [
            ("scan failed", 'echo "Code: 516. Authentication failed" >&2; return 4;', "WARNING: could not read system.grants"),
            ("no orphans", "return 0;", "OK: only the configured gateway accounts"),
            ("orphan found", 'echo "old_ro\tspans_public_v1"; return 0;', "WARNING: accounts other than"),
        ],
    )
    def test_orphan_scan_never_reports_ok_when_it_did_not_run(self, name, stub, expect):
        """The scan is informational, so a failure must not fail the Job, but a query
        that never ran must not print the all-clear either."""
        script = self._verify_script()
        start = script.index('echo "--- accounts with SELECT')
        lines = script[start:].splitlines()
        end = next(n for n, l in enumerate(lines) if l.strip() == "fi")
        out = _run_block(script, "\n".join(lines[: end + 1]), stub, "CH_ADMIN")
        assert expect in out, "%s: %s" % (name, out)
        assert "FAILED=0" in out, "%s: the informational scan failed the Job: %s" % (name, out)
        if name == "scan failed":
            assert "OK:" not in out, "a scan that never ran reported the all-clear: %s" % out

    # A stub on PATH rather than a shell function: CH_RO is built as
    # `env VAR=... clickhouse-client`, and env execs the real binary, so a function
    # would only ever stub the admin half of the script.
    _STUB = """#!/usr/bin/env bash
q=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--query" ]; then q="$2"; shift; fi
  shift
done
case "$q" in
  *"SELECT version()"*) echo "25.2.1" ;;
  *"SHOW CREATE VIEW"*) echo "CREATE VIEW v DEFINER = ${DEFINER:-sql_gateway_writer} SQL SECURITY DEFINER AS SELECT 1" ;;
  *"_public_v1(project_id"*) echo "0" ;;
  *"LIMIT 0"*) echo "Code: 497. DB::Exception: ACCESS_DENIED" >&2; exit 1 ;;
  *"SHOW GRANTS FOR"*)
    printf 'GRANT SELECT ON default.spans_public_v1 TO sql_gateway_ro\n'
    printf 'GRANT SELECT ON default.traces_public_v1 TO sql_gateway_ro\n'
    ${EXTRA_GRANT:+printf '%s\n' "$EXTRA_GRANT"} ;;
  *"getSetting('readonly')"*) printf '%s\\n' "${CAPS:-1\t30\t100000\t536870912\t4294967296}" ;;
  *"SETTINGS max_result_rows"*) echo "Code: 164. DB::Exception: READONLY" >&2; exit 1 ;;
  *) : ;;
esac
exit 0
"""

    def _run_whole_verify(self, **env) -> int:
        """Run the entire rendered hook with a stubbed client, and return its exit code."""
        d = tempfile.mkdtemp()
        try:
            stub = os.path.join(d, "clickhouse-client")
            with open(stub, "w") as f:
                f.write(self._STUB)
            os.chmod(stub, 0o755)
            script = os.path.join(d, "verify.sh")
            with open(script, "w") as f:
                f.write(self._verify_script())
            e = dict(os.environ)
            e.update({
                "PATH": d + os.pathsep + e["PATH"],
                "CLICKHOUSE_ADMIN_PASSWORD": "admin",
                "SQL_GATEWAY_RO_PASSWORD": "ro",
            })
            e.update({k: v for k, v in env.items()})
            return subprocess.run(["bash", script], env=e, capture_output=True, text=True).returncode
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.parametrize(
        "name,env,expect",
        [
            ("everything healthy", {}, 0),
            ("view owned by someone else", {"DEFINER": "default"}, 1),
            ("settings profile detached", {"CAPS": "0\t0\t0\t0\t0"}, 1),
            ("one cap quietly widened", {"CAPS": "1\t30\t100000\t536870912\t42949672960"}, 1),
            ("read-only account widened", {"EXTRA_GRANT": "GRANT SELECT ON default.spans TO sql_gateway_ro"}, 1),
        ],
    )
    def test_the_hook_exit_code_actually_reflects_the_checks(self, name, env, expect):
        """Helm reads the exit status, and nothing else asserted it.

        Deleting `exit 1` from the summary, or appending `exit 0` after it, left the
        suite green while turning the hook into one that can never fail a release.
        The FAILED variable is an implementation detail; this is the contract.
        """
        assert self._run_whole_verify(**env) == expect, name

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
