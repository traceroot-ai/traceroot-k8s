"""Chart guard: pods that store traces get the region the app actually reads.

S3Settings uses the prefix S3_, so the region field is S3_REGION and defaults
to us-east-1. An empty S3_REGION is the empty string, which replaces that
default, so the chart sets the variable only when s3.region is set.
"""

import shutil

import pytest

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

_WITH_BUCKET = ("traceroot-rest", "traceroot-worker")


def test_a_set_region_is_s3_region(env_by_deployment):
    envs = env_by_deployment("--set", "s3.region=eu-west-1", "--set", "s3.bucket=traces")
    for name in _WITH_BUCKET:
        env = envs[name]
        assert env["S3_BUCKET_NAME"]["value"] == "traces"
        assert env["S3_REGION"]["value"] == "eu-west-1"
        assert "AWS_DEFAULT_REGION" not in env


def test_an_empty_region_is_left_unset(env_by_deployment):
    envs = env_by_deployment()
    for name in _WITH_BUCKET:
        env = envs[name]
        assert "S3_REGION" not in env
        assert "AWS_DEFAULT_REGION" not in env


def test_every_pod_with_a_bucket_gets_the_same_region(env_by_deployment):
    envs = env_by_deployment("--set", "s3.region=ap-southeast-2")
    holders = [name for name, env in envs.items() if "S3_BUCKET_NAME" in env]
    assert sorted(holders) == sorted(_WITH_BUCKET)
    assert {envs[name]["S3_REGION"]["value"] for name in holders} == {"ap-southeast-2"}
