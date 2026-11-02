import asyncio
import copy
import json

import httpx
import pytest

from mooncake_bot.config import Config
from mooncake_bot.github import GitHub, GitHubError
from mooncake_bot.rsi import Settings, Worker
from mooncake_bot.rsi_state import State
from mooncake_bot.upstream import UPSTREAM, UPSTREAM_ID, UpstreamSync


class API:
    prefix = "/repos/stmatengss/Mooncake"

    def __init__(self, config):
        self.head, self.ahead, self.behind = "a" * 40, 12, 3
        self.repo = {
            "id": config.repository_id,
            "full_name": config.repository,
            "fork": True,
            "default_branch": "main",
            "parent": {"id": UPSTREAM_ID, "full_name": UPSTREAM, "default_branch": "main"},
        }
        self.writes, self.permissions = [], []
        self.error = None
        self.preserved = True

    def get(self, path, _task, **kwargs):
        if path == self.prefix:
            return self.repo
        if path.endswith("/commits/main"):
            return {"sha": self.head}
        if path.endswith("...kvcache-ai:main"):
            return {"ahead_by": self.ahead, "behind_by": self.behind}
        if "/compare/" in path:
            return {
                "behind_by": 0 if self.preserved else 1,
                "status": "ahead" if self.preserved else "diverged",
            }
        raise AssertionError(path)

    def token(self, installation, **permissions):
        assert installation == 1
        self.permissions.append(permissions)
        return "opaque-sync-token"

    def request(self, method, path, _token, **kwargs):
        self.writes.append((method, path, kwargs))
        if self.error:
            raise self.error
        self.head = "b" * 40
        self.ahead = 0
        return {"merge_type": "merge", "base_branch": "kvcache-ai:main"}


@pytest.fixture
def sync(tmp_path):
    config = Config(
        root=tmp_path,
        repository="stmatengss/Mooncake",
        repository_id=123,
        backend="kimi",
        installation_ids=[1],
        publish=True,
    )
    clock = [10000.0]
    worker = Worker(
        config,
        Settings(upstream_sync_enabled=True),
        github=API(config),
        state=State(tmp_path / "state", now=lambda: clock[0]),
    )
    result = UpstreamSync(worker)
    result.clock = clock
    return result


def test_sync_preserves_fork_commits_and_never_updates_refs_directly(sync):
    sync.run_if_due()
    github = sync.worker.github
    assert github.writes == [
        ("POST", github.prefix + "/merge-upstream", {"json": {"branch": "main"}})
    ]
    assert github.permissions == [{"contents_write": True, "workflows_write": True}]
    record = sync.status()["last"]
    assert record["state"] == "synced"
    assert record["result"]["previous_head_preserved"] is True
    assert record["result"]["fork_commits_ahead"] == 3
    assert sync.state.used() == 0


def test_schedule_is_durable_and_current_upstream_needs_no_write(sync):
    sync.run_if_due()
    sync.run_if_due()
    assert len(sync.worker.github.writes) == 1
    sync.clock[0] += sync.settings.upstream_sync_interval_seconds
    restarted = UpstreamSync(
        Worker(
            sync.config,
            sync.settings,
            github=sync.worker.github,
            state=State(sync.state.directory, now=lambda: sync.clock[0]),
        )
    )
    restarted.run_if_due()
    assert len(sync.worker.github.writes) == 1
    assert restarted.status()["last"]["state"] == "up_to_date"


@pytest.mark.parametrize(
    "status,state",
    [(409, "conflict"), (403, "permission_denied"), (422, "sync_rejected"), (500, "unconfirmed")],
)
def test_sync_failures_are_recorded_without_force_or_immediate_retry(sync, status, state):
    sync.worker.github.error = GitHubError("redacted", status)
    sync.run_if_due()
    sync.run_if_due()
    assert sync.status()["last"]["state"] == state
    assert sync.worker.github.head == "a" * 40
    assert len(sync.worker.github.writes) == 1
    assert "redacted" not in json.dumps(sync.status())


@pytest.mark.parametrize("change", ["repository_id", "parent_id", "parent_name", "branch", "fork"])
def test_unenrolled_identity_never_gets_a_write_token(sync, change):
    github = sync.worker.github
    if change == "repository_id":
        github.repo["id"] += 1
    elif change == "parent_id":
        github.repo["parent"]["id"] += 1
    elif change == "parent_name":
        github.repo["parent"]["full_name"] = "other/Mooncake"
    elif change == "branch":
        sync.settings.base_ref = "feature"
    else:
        github.repo["fork"] = False
    sync.run_if_due()
    assert sync.status()["last"]["state"] == "failed"
    assert not github.permissions and not github.writes


@pytest.mark.parametrize("switch", ["disabled", "paused", "preview"])
def test_disabled_or_paused_sync_does_not_write(sync, switch):
    if switch == "disabled":
        sync.settings.upstream_sync_enabled = False
    elif switch == "paused":
        (sync.state.directory / "STOP").touch()
    else:
        sync.config.publish = False
    sync.run_if_due()
    assert not sync.worker.github.permissions and not sync.worker.github.writes


def test_sync_still_runs_with_exhausted_model_budget_and_full_draft_queue(sync):
    sync.worker.config.max_model_calls_per_day = 1
    sync.state.reserve(1)
    sync.settings.max_open_proposals = 1
    # The scheduler must not ask the publication queue or a model to allow maintenance.
    sync.worker.open_proposals = lambda: (_ for _ in ()).throw(AssertionError("Draft queue"))
    asyncio.run(sync.worker.step())
    assert sync.status()["last"]["state"] == "synced"
    assert sync.state.used() == 1


def test_unconfirmed_history_is_reported_without_force_rollback(sync):
    sync.worker.github.preserved = False
    sync.run_if_due()
    assert sync.status()["last"]["state"] == "unconfirmed"
    assert len(sync.worker.github.writes) == 1


def test_lost_sync_response_is_reconciled_from_live_history_at_next_interval(sync):
    github = sync.worker.github
    original = github.request

    def lost_response(*args, **kwargs):
        original(*args, **kwargs)
        raise httpx.ReadTimeout("private transport details")

    github.request = lost_response
    sync.run_if_due()
    assert sync.status()["last"]["state"] == "unconfirmed"
    assert "private" not in json.dumps(sync.status())
    sync.clock[0] += sync.settings.upstream_sync_interval_seconds
    sync.run_if_due()
    assert sync.status()["last"]["state"] == "up_to_date"
    assert len(github.writes) == 1


def test_recovery_keeps_prewrite_receipt_without_replaying_the_merge(sync):
    run = sync.state.begin("upstream_sync", "interrupted-sync")
    sync.state.finish(run, "syncing", {"before": "a" * 40})
    sync.state.recover()
    assert sync.status()["last"]["state"] == "unconfirmed"
    assert sync.status()["last"]["result"]["before"] == "a" * 40
    sync.run_if_due()
    assert not sync.worker.github.writes


def test_sync_permission_is_not_cached_or_requested_for_ordinary_review_tokens(tmp_path):
    config = Config(root=tmp_path, installation_ids=[1], repository_id=123)
    github = GitHub(config)
    permissions = []
    github.assertion = lambda: "assertion"

    def request(method, path, token, **kwargs):
        permissions.append(copy.deepcopy(kwargs["json"]["permissions"]))
        assert kwargs["json"]["repository_ids"] == [123]
        return {"token": "token-" + str(len(permissions))}

    github.request = request
    read = github.token(1)
    synced = github.token(1, contents_write=True, workflows_write=True)
    assert read != synced and github.token(1) == read
    assert "workflows" not in permissions[0]
    assert permissions[1]["workflows"] == permissions[1]["contents"] == "write"
    with pytest.raises(GitHubError):
        github.token(1, workflows_write=True)
    assert len(permissions) == 2


def test_github_error_retains_http_status_without_response_body(tmp_path):
    github = GitHub(Config(root=tmp_path))
    github.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(409, text="sensitive upstream response")
        ),
        base_url="https://api.github.com",
    )
    with pytest.raises(GitHubError) as error:
        github.request("POST", "/repos/example/fork/merge-upstream", "secret-token")
    assert error.value.status_code == 409
    assert "sensitive" not in str(error.value) and "secret" not in str(error.value)
