def test_worker_can_call_the_web_service(env_by_deployment):
    env = env_by_deployment(
        "--set", "web.port=3100",
        "--set", "secrets.existingSecret=custom-app-secret",
        "--set", "secrets.keys.internalApiSecret=custom-internal-key",
    )
    worker = env["traceroot-worker"]
    assert worker["TRACEROOT_UI_URL"]["value"] == "http://traceroot-web:3100"
    secret_ref = {"secretKeyRef": {"name": "custom-app-secret", "key": "custom-internal-key"}}
    assert worker["INTERNAL_API_SECRET"]["valueFrom"] == secret_ref
    assert worker["INTERNAL_API_SECRET"] == env["traceroot-web"]["INTERNAL_API_SECRET"]
