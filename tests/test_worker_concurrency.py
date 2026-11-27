"""Worker concurrency and memory values are configurable without changing other pods."""

import shutil

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _worker_env(deployment):
    return deployment["spec"]["template"]["spec"]["containers"][0]["env"]


def _named_entries(env, name):
    return [entry for entry in env if entry["name"] == name]


def test_default_concurrency_and_memory_are_rendered_for_worker_only(rendered_deployments):
    deployments = rendered_deployments()
    worker = deployments["traceroot-worker"]
    worker_env = _worker_env(worker)

    assert _named_entries(worker_env, "WORKER_CONCURRENCY") == [
        {"name": "WORKER_CONCURRENCY", "value": "2"}
    ]
    assert worker["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "limits": {"cpu": "1", "memory": "2Gi"},
        "requests": {"cpu": "250m", "memory": "2Gi"},
    }
    assert all(
        not _named_entries(_worker_env(deployment), "WORKER_CONCURRENCY")
        for name, deployment in deployments.items()
        if name != "traceroot-worker"
    )


def test_worker_concurrency_can_be_overridden(rendered_deployments):
    worker = rendered_deployments("--set", "worker.concurrency=4")["traceroot-worker"]

    assert _named_entries(_worker_env(worker), "WORKER_CONCURRENCY") == [
        {"name": "WORKER_CONCURRENCY", "value": "4"}
    ]


def test_legacy_global_concurrency_wins_once_on_worker_and_remains_global(
    rendered_deployments,
):
    deployments = rendered_deployments(
        "--set", "worker.concurrency=4",
        "--set", "additionalEnv[0].name=WORKER_CONCURRENCY",
        "--set-string", "additionalEnv[0].value=3",
        "--set", "additionalEnv[1].name=WORKER_CONCURRENCY",
        "--set-string", "additionalEnv[1].value=5",
        "--set", "additionalEnv[2].name=LEGACY_GLOBAL_SETTING",
        "--set-string", "additionalEnv[2].value=present",
    )

    for name, deployment in deployments.items():
        env = _worker_env(deployment)
        assert _named_entries(env, "LEGACY_GLOBAL_SETTING") == [
            {"name": "LEGACY_GLOBAL_SETTING", "value": "present"}
        ]
        if name == "traceroot-worker":
            assert _named_entries(env, "WORKER_CONCURRENCY") == [
                {"name": "WORKER_CONCURRENCY", "value": "3"}
            ]
        else:
            assert [entry["value"] for entry in _named_entries(env, "WORKER_CONCURRENCY")] == [
                "3",
                "5",
            ]


def test_worker_resource_overrides_are_preserved(rendered_deployments):
    worker = rendered_deployments(
        "--set", "worker.resources.requests.memory=1536Mi",
        "--set", "worker.resources.limits.memory=3Gi",
    )["traceroot-worker"]
    resources = worker["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["requests"]["memory"] == "1536Mi"
    assert resources["limits"]["memory"] == "3Gi"
