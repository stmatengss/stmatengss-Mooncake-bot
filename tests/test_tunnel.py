import hashlib
import hmac

import httpx
import pytest

from mooncake_bot.config import Config
from mooncake_bot.tunnel import probe, register


def test_probe_is_signed_and_does_not_enqueue_analysis():
    def handle(request):
        assert request.url.path == "/webhooks/github"
        assert request.headers["x-github-event"] == "ping"
        assert request.headers["x-hub-signature-256"] == (
            "sha256=" + hmac.new(b"secret", request.content, hashlib.sha256).hexdigest()
        )
        return httpx.Response(200, json={"accepted": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        probe(client, "https://example-tunnel.trycloudflare.com", b"secret")


def test_registration_preserves_existing_config_and_changes_only_webhook_url(tmp_path):
    config = Config(root=tmp_path, backend="kimi", publish=True)
    config.save(tmp_path / "config.toml")
    calls = []

    class GitHub:
        def assertion(self):
            return "private-token"

        def request(self, method, path, token, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return {"url": "https://old.example/webhooks/github", "insecure_ssl": "0"}
            return kwargs["json"] | {"insecure_ssl": "0"}

    url = register(tmp_path / "config.toml", "https://new-tunnel.trycloudflare.com", GitHub())
    assert calls[-1] == ("PATCH", "/app/hook/config", {"json": {"url": url}})
    saved = Config.load(tmp_path / "config.toml")
    assert saved.webhook_url == url and saved.backend == "kimi" and saved.publish
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_registration_rejects_arbitrary_origins(tmp_path):
    with pytest.raises(ValueError, match="origin"):
        register(tmp_path / "missing.toml", "https://attacker.example", None)


def test_successful_http_status_without_accepted_ping_is_not_healthy():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"accepted": False}))
    ) as client:
        with pytest.raises(ValueError, match="not accepted"):
            probe(client, "https://example-tunnel.trycloudflare.com", b"secret")
