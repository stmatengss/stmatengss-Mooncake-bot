import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from mooncake_bot.agent import added_lines, check_result
from mooncake_bot.config import Config
from mooncake_bot.github import GitHub, GitHubError
from mooncake_bot.service import create_app, execute, parse_event
from mooncake_bot.setup import create_setup, manifest
from mooncake_bot.store import Store


@pytest.fixture
def config(tmp_path):
    result = Config(root=tmp_path, installation_ids=[123])
    result.path("webhook_secret_file").parent.mkdir()
    result.path("webhook_secret_file").write_text("a" * 64)
    return result


@pytest.fixture
def payload():
    user = {"id": 11641725, "login": "stmatengss", "type": "User"}
    return {
        "action": "created",
        "repository": {"id": 819733173, "full_name": "kvcache-ai/Mooncake"},
        "installation": {"id": 123},
        "sender": user,
        "comment": {"id": 99, "user": user, "body": "@mooncake-bot review"},
        "issue": {"number": 1, "pull_request": {}},
    }


def post(client, payload, secret=b"a" * 64, event="issue_comment"):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature},
    )


def test_signature_and_semantic_dedup(config, payload):
    app = create_app(config, workers=False)
    with TestClient(app) as client:
        assert post(client, payload, b"wrong").status_code == 401
        first, second = post(client, payload), post(client, payload)
        assert first.status_code == 200
        assert first.json()["task_id"] == second.json()["task_id"]
    assert len(app.state.store.list()) == 1


@pytest.mark.parametrize(
    "change", ["actor", "repository", "installation", "edited", "quoted", "bot"]
)
def test_reject_untrusted_triggers(config, payload, change):
    if change == "actor":
        payload["sender"] = {"id": 999, "login": "outsider", "type": "User"}
    elif change == "repository":
        payload["repository"]["id"] = 12
    elif change == "installation":
        payload["installation"]["id"] = 456
    elif change == "edited":
        payload["action"] = "edited"
    elif change == "quoted":
        payload["comment"]["body"] = "> @mooncake-bot review"
    elif change == "bot":
        payload["sender"]["type"] = "Bot"
    app = create_app(config, workers=False)
    with TestClient(app) as client:
        response = post(client, payload)
        assert response.status_code == 403 or response.json()["accepted"] is False
    assert not app.state.store.list()


def test_malformed_signed_input(config):
    with TestClient(create_app(config, workers=False)) as client:
        assert post(client, []).status_code == 403
        assert post(client, {}).status_code == 200
        assert post(client, {}, event="ping").json() == {"accepted": True}


def test_size_limit(config):
    with TestClient(create_app(config, workers=False)) as client:
        assert (
            client.post("/webhooks/github", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
        )


def test_same_pr_serial_and_global_limit(config, payload):
    store = Store(config.path("data_dir"))
    task = parse_event(config, "issue_comment", payload)
    first = store.enqueue(task)
    store.enqueue(task | {"source": "issue_comment:100"})
    other = store.enqueue(task | {"source": "issue_comment:101", "number": 2})
    assert store.claim(2)["id"] == first["id"]
    assert store.claim(2)["id"] == other["id"]
    assert store.claim(2) is None
    store.update(first["id"], "succeeded")
    assert store.claim(2)["number"] == 1


def test_restart_does_not_replay_ambiguous_writes(config, payload):
    store = Store(config.path("data_dir"))
    row = store.enqueue(parse_event(config, "issue_comment", payload))
    store.update(row["id"], "publishing")
    store.recover()
    assert store.list()[0]["state"] == "interrupted"
    assert store.claim(2) is None


def test_added_lines_and_result_validation():
    patch = "@@ -2,2 +2,3 @@\n keep\n-old\n+new\n+extra"
    assert added_lines(patch) == {3, 4}
    context = {"files": [{"filename": "a.py", "patch": patch}]}
    result = {
        "summary": "A defect",
        "findings": [{"path": "a.py", "line": 3, "priority": 1, "body": "Details"}],
        "limitations": [],
    }
    assert check_result(result, context) == result
    result["findings"][0]["line"] = 2
    with pytest.raises(ValueError, match="added line"):
        check_result(result, context)


def test_stale_head_rejected_before_write(config, payload):
    github = GitHub(config)
    github.authorize = lambda task: None
    github.get = lambda *args, **kwargs: {
        "state": "open",
        "head": {"sha": "new"},
        "base": {"sha": "base"},
    }
    task = parse_event(config, "issue_comment", payload)
    with pytest.raises(GitHubError, match="stale"):
        github.publish(task, {"head": "old", "base": "base"}, "do not post")


def test_preview_runs_without_github_write(config, payload):
    context = {"head": "a" * 40, "base": "b" * 40, "files": []}

    class FakeGitHub:
        def context(self, task):
            return context

        def publish(self, *args):
            raise AssertionError("Preview must never write to GitHub")

    async def fake_agent(config, task, context, directory):
        directory.mkdir(parents=True)
        return {
            "summary": "No findings established",
            "findings": [],
            "limitations": ["Tests not run"],
        }

    store = Store(config.path("data_dir"))
    row = store.enqueue(parse_event(config, "issue_comment", payload))
    asyncio.run(execute(config, store, FakeGitHub(), row, fake_agent))
    assert store.list()[0]["state"] == "preview"
    comment = config.path("data_dir") / "tasks" / row["id"] / "comment.md"
    assert "AI-assisted" in comment.read_text()


def test_config_roundtrip_keeps_secrets_out(config):
    path = config.root / "config.toml"
    config.save(path)
    loaded = Config.load(path)
    assert loaded.installation_ids == [123]
    assert "a" * 64 not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_setup_csrf_and_permission_scope(config):
    path = config.root / "config.toml"
    config.save(path)
    registration = manifest(config, "http://127.0.0.1:8822")
    assert registration["default_permissions"]["contents"] == "read"
    assert registration["default_permissions"]["actions"] == "read"
    assert not registration["hook_attributes"]["active"]
    with TestClient(create_setup(path), base_url="http://127.0.0.1:8822") as client:
        assert client.get("/").status_code == 200
        assert client.get("/callback?code=example-code-12345&state=wrong").status_code == 403
        assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_no_automatic_or_fix_tasks_in_first_phase(config, payload):
    assert parse_event(config, "pull_request", payload) is None
    payload["comment"]["body"] = "@mooncake-bot fix everything"
    assert parse_event(config, "issue_comment", payload) is None


def test_personal_fork_registration(config):
    config.repository = "stmatengss/Mooncake"
    config.repository_id = 898854070
    config.registration_owner_type = "User"
    path = config.root / "config.toml"
    config.save(path)
    with TestClient(create_setup(path), base_url="http://127.0.0.1:8822") as client:
        body = client.get("/").text
        assert "action='https://github.com/settings/apps/new?state=" in body
        assert "/organizations/" not in body
    assert manifest(config, "http://127.0.0.1:8822")["public"] is False
