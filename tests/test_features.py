import asyncio
import copy
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest

from mooncake_bot.automation import scan
from mooncake_bot.commands import automatic_task, normalized_title, parse_tasks, pr_tasks
from mooncake_bot.config import Config
from mooncake_bot.features import fix_allowed, prepare_retry, run, status
from mooncake_bot.github import GitHub, GitHubError
from mooncake_bot.publishing import (
    DESCRIPTION_END,
    DESCRIPTION_START,
    file_mode,
    managed_description,
    publish,
    publish_fix,
)
from mooncake_bot.rsi import apply_change
from mooncake_bot.rsi_state import State
from mooncake_bot.service import execute
from mooncake_bot.store import Store

RESULT = {"summary": "Observed change; tests not run", "findings": [], "limitations": []}
ORIGINAL = "def ratio(value):\n    return 10 / value\n"
PROPOSAL = {
    "summary": "Handle a zero denominator",
    "test_plan": "Check zero and positive inputs in isolation.",
    "change": {
        "path": "scripts/probe.py",
        "before": "    return 10 / value\n",
        "after": "    return 10 / value if value else 0\n",
    },
}


@pytest.fixture
def config(tmp_path):
    return Config(
        root=tmp_path,
        repository="owner/repo",
        repository_id=9,
        installation_ids=[123],
        allowed_actor_ids=[7],
        mention="test-bot",
        backend="kimi",
        enable_fix_prs=True,
        auto_review=True,
        auto_summary=True,
        auto_title=True,
        auto_triage=True,
        auto_ci=True,
    )


@pytest.fixture
def pr():
    return {
        "number": 29,
        "state": "open",
        "draft": False,
        "title": "fix zero input",
        "body": "Author text\n- [x] Actually checked",
        "html_url": "https://example.test/pr/29",
        "user": {"id": 7, "login": "owner", "type": "User"},
        "head": {"sha": "a" * 40, "ref": "feature", "repo": {"id": 9}},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


@pytest.fixture
def task():
    return {
        "source": "issue_comment:99",
        "event": "issue_comment",
        "kind": "pr",
        "number": 29,
        "actor": "owner",
        "actor_id": 7,
        "installation": 123,
        "comment_id": 99,
        "body": "@test-bot review",
        "command": "review",
    }


def command_payload(config, task):
    user = {"id": 7, "login": "owner", "type": "User"}
    return {
        "repository": {"id": config.repository_id, "full_name": config.repository},
        "installation": {"id": 123},
        "sender": user,
        "action": "created",
        "comment": {"id": 99, "user": user, "body": task["body"]},
        "issue": {"number": 29, "pull_request": {}},
    }


@pytest.mark.parametrize(
    "request_text,expected",
    [
        ("review", "review"),
        ("summarize", "summary"),
        ("retry-ci 1234", "rerun-ci"),
        ("fix scripts/probe.py", "fix"),
        ("please fix scripts/probe.py", "fix"),
        ("这个改动为什么需要锁？", "ask"),
        ("", "help"),
        ("plan", "plan"),
    ],
)
def test_command_routing(config, task, request_text, expected):
    task["body"] = "@test-bot " + request_text
    assert (
        parse_tasks(config, "issue_comment", command_payload(config, task))[0]["command"]
        == expected
    )


@pytest.mark.parametrize(
    "body", ["@test-bot-extra review", "> @test-bot review", "hello @test-bot"]
)
def test_partial_or_quoted_mentions_do_not_trigger(config, task, body):
    task["body"] = body
    assert parse_tasks(config, "issue_comment", command_payload(config, task)) == []


def test_issue_and_inline_commands_keep_their_kind(config, task):
    payload = command_payload(config, task)
    payload["issue"].pop("pull_request")
    assert parse_tasks(config, "issue_comment", payload)[0]["kind"] == "issue"
    payload["pull_request"] = payload.pop("issue")
    assert parse_tasks(config, "pull_request_review_comment", payload)[0]["kind"] == "pr"
    payload["action"] = "edited"
    assert parse_tasks(config, "pull_request_review_comment", payload) == []


def test_automatic_tasks_are_revision_bound_and_drafts_skip_analysis(config, pr):
    first = pr_tasks(config, pr)
    assert [r["command"] for r in first] == ["auto-title", "auto-review"]
    changed = copy.deepcopy(pr)
    changed["head"]["sha"] = "c" * 40
    assert pr_tasks(config, changed)[1]["source"] != first[1]["source"]
    changed["draft"] = True
    assert [r["command"] for r in pr_tasks(config, changed)] == ["auto-title"]
    changed["body"] += "\n<!-- mooncake-bot SKIP -->"
    assert pr_tasks(config, changed) == []
    changed["body"] = ""
    changed["user"]["id"] = 88
    assert pr_tasks(config, changed) == []


def test_automatic_authorization_rechecks_head_and_actor(config, pr):
    github = GitHub(config)
    item = copy.deepcopy(pr)
    task = automatic_task(config, pr, "auto-review")

    def get(path, *_args, **_kwargs):
        if path.endswith("/pulls/29"):
            return item
        if path.startswith("/users/"):
            return {"id": 7}
        return {"permission": "write"}

    github.get = get
    github.authorize(task)
    item["head"]["sha"] = "c" * 40
    with pytest.raises(GitHubError, match="revision"):
        github.authorize(task)
    item["head"]["sha"] = pr["head"]["sha"]
    item["user"]["type"] = "Bot"
    github.actor_login = lambda: "test-bot[bot]"
    with pytest.raises(GitHubError, match="own PRs"):
        github.authorize(task)


@pytest.mark.parametrize("long_title", [False, True])
def test_periodic_scan_reconciles_without_duplicate_tasks(config, pr, long_title):
    if long_title:
        pr["title"] = "x" * 256
    config.app_id = "1"
    config.path("app_key_file").parent.mkdir()
    config.path("app_key_file").write_text("not a real key")
    store = Store(config.path("data_dir"))

    class API:
        prefix = "/repos/owner/repo"

        def get(self, path, *_args, **_kwargs):
            if path.endswith("/pulls"):
                return [pr]
            if path.endswith("/actions/runs"):
                return {"workflow_runs": []}
            return []

    expected = 1 if long_title else 2
    assert len(scan(config, API(), store)) == expected
    assert len(scan(config, API(), store)) == expected
    assert len(store.list()) == expected


@pytest.mark.parametrize(
    "title,prefix",
    [
        ("address race", "Misc"),
        ("circular buffer", "Misc"),
        ("docker image", "Misc"),
        ("fixture cleanup", "Misc"),
        ("fixes a race", "Bugfix"),
        ("docs: update usage", "Doc"),
        ("building a wheel", "CI/Build"),
        ("adds a guard", "Feature"),
        ("修复内存泄漏", "Bugfix"),
        ("支持新的协议", "Feature"),
    ],
)
def test_title_keywords_use_word_boundaries(title, prefix):
    assert normalized_title(title) == f"[{prefix}] {title}"


def test_model_description_markers_do_not_corrupt_managed_block():
    author = "Human text\n- [x] Verified\n"
    block = DESCRIPTION_START + "summary" + DESCRIPTION_END
    # Removing one delimiter may expose another; all model delimiters must disappear.
    block += DESCRIPTION_START[:10] + DESCRIPTION_END + DESCRIPTION_START[10:]
    first = managed_description(author, block)
    assert first.count(DESCRIPTION_START) == first.count(DESCRIPTION_END) == 1
    assert first.startswith(author)
    assert managed_description(first, "updated").startswith(author)


def test_title_and_managed_description_preserve_author_content():
    assert normalized_title("fix zero input") == "[Bugfix] fix zero input"
    assert normalized_title("[Store] Add guard") == "[Store] Add guard"
    author = "Author context\n- [x] Real check\n"
    first = managed_description(author, "old bot block")
    changed = managed_description(first + "\nMore human text", "new bot block")
    assert changed.startswith(author)
    assert changed.endswith("\nMore human text")
    assert "old bot block" not in changed
    assert changed.count(DESCRIPTION_START) == 1
    for malformed in [DESCRIPTION_START, DESCRIPTION_END + DESCRIPTION_START, first + first]:
        with pytest.raises(ValueError):
            managed_description(malformed, "new")


class Publisher:
    prefix = "/repos/owner/repo"

    def __init__(self, config, pr):
        self.config, self.pr = config, pr
        self.writes, self.permissions = [], []
        self.comments = []

    def assert_current(self, *_args):
        return self.pr

    def token(self, _installation, **permissions):
        self.permissions.append(permissions)
        return "opaque-test-token"

    def actor_login(self):
        return "test-bot[bot]"

    def pages(self, path, *_args):
        return [] if path.endswith("/pulls") else self.comments

    def request(self, method, path, _token, **kwargs):
        data = kwargs.get("json", {})
        self.writes.append((method, path, data))
        if path.endswith("/git/trees") or path.endswith("/git/commits"):
            return {"sha": "c" * 40}
        return {"html_url": "https://example.test/result", "number": 30}

    def get(self, path, *_args, **_kwargs):
        if "/git/commits/" in path:
            return {"tree": {"sha": "root"}}
        if path.endswith("/git/trees/root"):
            return {"tree": [{"path": "scripts", "type": "tree", "sha": "scripts"}]}
        if path.endswith("/git/trees/scripts"):
            return {"tree": [{"path": "probe.py", "type": "blob", "mode": "100755"}]}
        raise AssertionError(path)

    def file(self, *_args):
        return ORIGINAL


def review_context(pr):
    return {
        "kind": "pr",
        "head": pr["head"]["sha"],
        "base": pr["base"]["sha"],
        "head_ref": pr["head"]["ref"],
        "head_repository_id": 9,
        "title": pr["title"],
        "body": pr["body"],
        "guidance": {},
        "files": [
            {
                "filename": "scripts/probe.py",
                "content": ORIGINAL,
                "patch": "@@ -0,0 +1,2 @@\n+def ratio(value):\n+    return 10 / value\n",
            }
        ],
    }


def test_review_batches_inline_comments_and_never_approves(config, pr, task):
    github = Publisher(config, pr)
    context = review_context(pr)
    context["_result"] = RESULT | {
        "findings": [
            {"path": "scripts/probe.py", "line": 2, "priority": 1, "body": "Zero raises."},
        ]
    }
    publish(github, task, context, "Summary")
    assert len(github.writes) == 1
    request = github.writes[0][2]
    assert request["event"] == "COMMENT" and request["commit_id"] == context["head"]
    assert request["comments"][0]["line"] == 2
    github.comments = [
        {
            "user": {"login": "test-bot[bot]"},
            "path": "scripts/probe.py",
            "line": 2,
            "commit_id": context["head"],
        }
    ]
    publish(github, task, context, "No duplicate inline comment")
    assert github.writes[-1][2]["comments"] == []
    context["_result"]["findings"][0]["line"] = 3
    with pytest.raises(ValueError, match="added line"):
        publish(github, task, context, "Invalid anchor")
    assert len(github.writes) == 2


def test_summary_updates_only_own_comment(config, pr, task):
    github = Publisher(config, pr)
    task["command"] = "summary"
    marker = "<!-- mooncake-bot:summary:29 -->"
    github.comments = [
        {"id": 1, "user": {"login": "human"}, "body": marker},
        {"id": 2, "user": {"login": "test-bot[bot]"}, "body": marker},
    ]
    publish(github, task, review_context(pr), "New summary")
    assert github.writes[0][0:2] == ("PATCH", github.prefix + "/issues/comments/2")


def test_shared_budget_defers_model_tasks_but_help_still_runs(config, pr, task):
    config.max_model_calls_per_day = 1
    budget = State(config.path("data_dir") / "rsi")
    budget.reserve(1)
    github = Publisher(config, pr)
    github.context = lambda _task: review_context(pr)
    store = Store(config.path("data_dir"))

    async def no_model(*_args):
        raise AssertionError("Budget exhausted / builtin must not invoke a model")

    review = store.enqueue(task)
    asyncio.run(execute(config, store, github, review, no_model))
    deferred = store.list()[0]
    assert deferred["state"] == "queued" and deferred["not_before"] > time.time()
    helper = store.enqueue(task | {"source": "issue_comment:100", "command": "help"})
    assert store.claim(2)["id"] == helper["id"]
    asyncio.run(execute(config, store, github, helper, no_model))
    assert store.list()[0]["state"] == "preview"
    assert budget.used() == 1


def test_old_queue_migration_preserves_rows_and_write_once_actions(config):
    directory = config.path("data_dir")
    directory.mkdir()
    with sqlite3.connect(directory / "tasks.sqlite3") as db:
        db.execute(
            "CREATE TABLE tasks(id TEXT PRIMARY KEY,source TEXT UNIQUE NOT NULL,"
            "number INTEGER NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,"
            "created REAL NOT NULL,updated REAL NOT NULL,error TEXT,result TEXT,published_url TEXT)"
        )
        db.execute("INSERT INTO tasks VALUES('old','old',29,'{}','succeeded',1,1,NULL,NULL,'url')")
    store = Store(directory)
    assert store.list()[0]["published_url"] == "url"
    assert store.list()[0]["not_before"] == 0
    assert store.reserve_action("run:1:attempt:1")
    assert not Store(directory).reserve_action("run:1:attempt:1")


def test_resumed_budget_task_clears_waiting_status(config, task, monkeypatch):
    store = Store(config.path("data_dir"))
    row = store.enqueue(task)
    reset = time.time() + 86400
    store.defer(row["id"], reset)
    assert store.claim(2) is None
    monkeypatch.setattr("mooncake_bot.store.time.time", lambda: reset + 1)
    assert store.claim(2)["id"] == row["id"]
    resumed = store.list()[0]
    assert resumed["state"] == "running" and resumed["not_before"] == 0


@pytest.mark.parametrize(
    "state,offset,waiting",
    [
        ("queued", 100, True),
        ("queued", -1, False),
        ("succeeded", 100, False),
        ("failed", 100, False),
    ],
)
def test_status_waiting_suffix_requires_a_future_queued_task(config, task, state, offset, waiting):
    store = Store(config.path("data_dir"))
    row = store.enqueue(task)
    store.defer(row["id"], time.time() + offset)
    store.update(row["id"], state)
    budget = State(config.path("data_dir") / "rsi")
    result = status(config, store, task, {"head": "a" * 40}, budget)
    assert ("waiting for UTC reset" in result["summary"]) is waiting


def test_maintainer_commands_take_priority_over_older_automation(config, task):
    store = Store(config.path("data_dir"))
    automatic = store.enqueue(task | {"automatic": True})
    manual = store.enqueue(task | {"source": "issue_comment:100", "number": 30})
    assert store.claim(1)["id"] == manual["id"]
    store.update(manual["id"], "succeeded")
    assert store.claim(1)["id"] == automatic["id"]


def failed_run(pr):
    return {
        "id": 11,
        "name": "CPU checks",
        "html_url": "https://example.test/run/11",
        "head_sha": pr["head"]["sha"],
        "head_repository": {"id": 9},
        "status": "completed",
        "conclusion": "failure",
        "event": "pull_request",
        "run_attempt": 1,
    }


def test_ci_retry_checks_revision_attempt_and_reserves_before_write(config, pr, task):
    task.update(command="rerun-ci", body="@test-bot rerun-ci 11")
    github = Publisher(config, pr)
    workflow = failed_run(pr)
    github.get = lambda *_args, **_kwargs: workflow
    context = review_context(pr)
    assert prepare_retry(github, task, context)["builtin"]
    assert context["_ci_run"] == {"id": 11, "attempt": 1}
    publish(github, task, context, "Eligible")
    publish(github, task, context, "Already attempted")
    posts = [r for r in github.writes if r[1].endswith("/rerun-failed-jobs")]
    assert len(posts) == 1
    assert {"actions_write": True} in github.permissions
    workflow["head_sha"] = "c" * 40
    with pytest.raises(GitHubError, match="Workflow changed"):
        publish(github, task, context, "Stale")
    assert len([r for r in github.writes if r[1].endswith("/rerun-failed-jobs")]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_attempt", 3),
        ("head_sha", "c" * 40),
        ("event", "push"),
        ("status", "in_progress"),
        ("head_repository", {"id": 77}),
    ],
)
def test_ci_ineligible_runs_never_get_a_retry_plan(config, pr, task, field, value):
    github = Publisher(config, pr)
    workflow = failed_run(pr) | {field: value}
    github.get = lambda *_args, **_kwargs: workflow
    context = review_context(pr)
    task.update(command="rerun-ci", body="@test-bot rerun-ci 11")
    prepare_retry(github, task, context)
    assert "_ci_run" not in context


@pytest.mark.parametrize(
    "name",
    [
        "scripts/../secret.py",
        "/scripts/a.py",
        ".github/workflows/a.py",
        "scripts/AGENTS.md",
        "scripts/.git/config.py",
        "outside/a.py",
        "scripts/program.sh",
    ],
)
def test_fix_scope_rejects_unenrolled_or_governance_files(config, name):
    assert not fix_allowed(config, name)


def test_fix_proposes_then_reviews_and_publishes_only_a_child_draft(config, pr, task, monkeypatch):
    task.update(command="fix", body="@test-bot fix scripts/probe.py zero denominator")
    github = Publisher(config, pr)
    context = review_context(pr)
    directory = config.path("data_dir") / "tasks" / ("d" * 32)
    calls = []

    async def propose(_config, data, _schema, _policy, _directory):
        calls.append("propose")
        assert data["files"][0]["filename"] == "scripts/probe.py"
        return copy.deepcopy(PROPOSAL)

    async def review(_config, review_task, review_context, _directory):
        calls.append("review")
        assert review_task["command"] == "review"
        assert "if value else 0" in review_context["files"][0]["content"]
        return RESULT

    monkeypatch.setattr("mooncake_bot.features.invoke", propose)
    result = asyncio.run(
        run(config, Store(config.path("data_dir")), github, task, context, directory, review)
    )
    assert "Human review" in result["limitations"][0]
    assert calls == ["propose", "review"]
    assert State(config.path("data_dir") / "rsi").used() == 2
    context["_directory"] = directory
    publish_fix(github, task, context)
    tree = next(r[2] for r in github.writes if r[1].endswith("/git/trees"))
    commit = next(r[2] for r in github.writes if r[1].endswith("/git/commits"))
    draft = next(r[2] for r in github.writes if r[1].endswith("/pulls"))
    assert tree["tree"][0]["mode"] == "100755"
    assert commit["parents"] == [context["head"]]
    assert draft["draft"] is True and draft["base"] == "feature"
    assert "No repository code, unit tests" in draft["body"]
    assert all(method == "POST" for method, _, _ in github.writes)
    assert json.loads((directory / "publication.json").read_text())["phase"] == "draft_created"
    with pytest.raises(GitHubError, match="already attempted"):
        publish_fix(github, task, context)
    assert len(github.writes) == 4


def test_symlink_fix_target_rejected(config, pr, task):
    github = Publisher(config, pr)
    original_get = github.get

    def get(path, *args, **kwargs):
        if path.endswith("/git/trees/scripts"):
            return {"tree": [{"path": "probe.py", "type": "blob", "mode": "120000"}]}
        return original_get(path, *args, **kwargs)

    github.get = get
    with pytest.raises(GitHubError, match="regular tracked"):
        file_mode(github, "a" * 40, "scripts/probe.py", task)
    assert not github.writes


def test_concurrent_fix_publishers_respect_the_open_draft_limit(config, pr, task):
    config.max_fix_prs = 1
    both_started, second_count = Barrier(2), Event()

    class SharedPublisher(Publisher):
        def __init__(self):
            super().__init__(config, pr)
            self.opened, self.counts, self.guard = [], 0, Lock()

        def pages(self, *_args):
            with self.guard:
                snapshot = list(self.opened)
                self.counts += 1
                first = self.counts == 1
            if first:
                second_count.wait(1)
            else:
                second_count.set()
            return snapshot

        def request(self, method, path, token, **kwargs):
            result = super().request(method, path, token, **kwargs)
            if path.endswith("/pulls"):
                self.opened.append({"head": {"ref": kwargs["json"]["head"]}})
            return result

    github = SharedPublisher()
    updated, patch = apply_change(PROPOSAL["change"]["path"], ORIGINAL, PROPOSAL["change"])

    def attempt(index):
        context = review_context(pr)
        context["_directory"] = config.path("data_dir") / "tasks" / (str(index) * 32)
        context["_fix"] = {
            "path": PROPOSAL["change"]["path"],
            "updated": updated,
            "patch": patch,
            "proposal": PROPOSAL,
            "review": RESULT,
        }
        both_started.wait(3)
        try:
            return publish_fix(github, task | {"number": 28 + index}, context)
        except GitHubError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [1, 2]))
    assert len(github.opened) == 1
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert "Open fix draft limit reached" in outcomes
