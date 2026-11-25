import json
import subprocess
from argparse import Namespace

import httpx
import pytest

from mooncake_bot import nightly_publish
from mooncake_bot.github import GitHubError
from mooncake_bot.nightly import API, REPO, WEB, WORKFLOW, get_run, report_for_issue, scan_log
from mooncake_bot.nightly_publish import deliver, reviewed_change
from mooncake_bot.upstream import UPSTREAM, UPSTREAM_ID

T = "2026-11-23T01:00:"
STEP = {
    "number": 3,
    "name": "Configure",
    "conclusion": "failure",
    "started_at": T + "10Z",
    "completed_at": T + "20Z",
}
RUN = {
    "id": 61,
    "run_attempt": 2,
    "repository": {"id": UPSTREAM_ID},
    "path": WORKFLOW,
    "head_branch": "main",
    "head_sha": "a" * 40,
    "event": "schedule",
    "status": "completed",
    "created_at": T + "00Z",
    "conclusion": "failure",
}
ISSUE = {"labels": [{"name": "nightly-failure"}], "body": f"**Run**: {WEB}/actions/runs/61\n"}
JOB = {
    "id": 72,
    "name": "build-musa",
    "steps": [STEP],
    "conclusion": "failure",
    "check_run_url": f"https://api.github.com{REPO}/check-runs/72",
}


def test_scan_only_failed_steps_skips_echoes_and_handles_chunk_boundaries():
    content = "\n".join(
        T + line
        for line in (
            "05.1Z No space left on device",
            "10.1Z ##[group]Run cmake",
            "10.2Z Error: Failed to download metadata for repo 'echoed'",
            "10.5Z ##[endgroup]",
            "11.1Z - Status code: 404 for https://example.org/private-url",
            "11.2Z Error: Failed to download metadata for repo 'appstream'",
            "12.1Z E: Failed to fetch https://example.org/private-url 403 Forbidden",
            "13.1Z 63/319 Test #65: batch_counter_wait_test .....***Failed    1 sec",
            "13.2Z ERROR: LeakSanitizer: detected memory leaks",
            "13.3Z  1 FAILED TEST",
            '14.1Z Error: InvalidState("not enough writable placement targets: reserved=1 need=2")',
            "21.1Z fatal error: Killed signal terminated program",
        )
    ).encode()
    matches, coverage = scan_log([content[:34], content[34:71], content[71:]], [STEP])
    assert coverage == "read"
    assert [m["category"] for m in matches] == [
        "dependency",
        "dependency",
        "test failure",
        "sanitizer",
        "test failure",
    ]
    assert "HTTP 404" in matches[0]["evidence"] and "HTTP 403" in matches[1]["evidence"]
    assert "private-url" not in str(matches)


def test_rerun_uses_attempt_jobs_recovers_annotation_and_reuses_report(tmp_path):
    requests = []

    def handle(req):
        route = req.url.path.removeprefix(REPO)
        requests.append(route)
        if route == "/issues/8":
            data = ISSUE
        elif route == "/actions/runs/61":
            data = RUN
        elif route == "/actions/runs/61/attempts/2/jobs":
            data = {"jobs": [JOB], "total_count": 1}
        elif route == "/actions/jobs/72/steps/3/logs":
            return httpx.Response(404)
        elif route == "/actions/jobs/72/logs":
            return httpx.Response(302, headers={"location": "https://logs.example.com/file"})
        elif req.url.host == "logs.example.com":
            assert "authorization" not in req.headers
            return httpx.Response(404)
        elif route == "/check-runs/72/annotations":
            data = [
                {
                    "annotation_level": "failure",
                    "message": "The hosted runner lost communication with the server.",
                }
            ]
        elif route == "/actions/workflows/nightly.yml/runs":
            data = {
                "workflow_runs": [
                    {
                        "id": 55,
                        "run_attempt": 1,
                        "created_at": "2026-11-22T00:00:00Z",
                        "head_sha": "b" * 40,
                    }
                ]
            }
        elif route == "/actions/runs/55/attempts/1/jobs":
            data = {"jobs": [JOB | {"id": 56, "conclusion": "success"}], "total_count": 1}
        else:
            pytest.fail(route)
        return httpx.Response(200, json=data)

    api = API("read-only", httpx.MockTransport(handle))
    report, folder = report_for_issue(api, 8, tmp_path)
    assert json.loads((folder / "diagnosis.json").read_text()) == report
    assert "lost communication" in (folder / "diagnosis.md").read_text()
    assert report["jobs"][0]["previous_pass"]["job"] == 56
    report_for_issue(api, 8, tmp_path)
    assert requests.count("/actions/jobs/72/logs") == 1


@pytest.mark.parametrize(
    "change",
    [
        {"repository": {"id": 1}},
        {"path": ".github/workflows/release.yml"},
        {"head_branch": "release"},
        {"event": "pull_request"},
    ],
)
def test_refuses_unrelated_run(change):
    api = API(
        "read",
        httpx.MockTransport(
            lambda req: httpx.Response(
                200, json=ISSUE if "issues" in req.url.path else RUN | change
            )
        ),
    )
    with pytest.raises(ValueError, match="upstream main nightly"):
        get_run(api, 8)


def prepare(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Fixture Author")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "core.hooksPath", "/dev/null")
    git("config", "commit.gpgSign", "false")
    git("commit", "--allow-empty", "-qm", "base")
    git("commit", "--allow-empty", "-qsm", "[CI/Build] Preserve nightly diagnostics")
    sha = git("rev-parse", "HEAD")
    body = tmp_path / "pr.md"
    body.write_text(
        "## How Has This Been Tested?\nExercised fixture.\n## AI Assistance Disclosure\nAI assisted."
    )
    return Namespace(
        account="fixture-machine",
        issue=8,
        job=72,
        reviewed_commit=sha,
        checkout=tmp_path,
        body_file=body,
    )


class Publisher:
    def __init__(self):
        self.login = "fixture-machine"
        self.parent = UPSTREAM_ID
        self.ref = self.pr = None
        self.writes = []

    def request(self, method, path, **kwargs):
        if path == "/user":
            return {"type": "User", "id": 2, "login": self.login}
        if path == "/repos/fixture-machine/Mooncake":
            return {
                "fork": True,
                "id": 11,
                "owner": {"id": 2},
                "parent": {"id": self.parent, "full_name": UPSTREAM},
            }
        if path == REPO + "/pulls":
            if method == "GET":
                return [self.pr] if self.pr else []
            data = kwargs["json"]
            assert (
                data["draft"]
                and data["base"] == "main"
                and data["head"].startswith(self.login + ":sima/nightly-8-")
            )
            assert "actions/runs/61/job/72" in data["body"]
            self.writes.append(data)
            self.pr = {
                "draft": True,
                "head": {"sha": self.ref, "repo": {"id": 11}},
                "base": {"repo": {"id": UPSTREAM_ID}},
                "user": {"id": 2},
                "html_url": WEB + "/pull/100",
            }
            return self.pr
        if path.endswith("/issues/8"):
            return ISSUE
        if path.endswith("/actions/runs/61"):
            return RUN
        if "/compare/" in path:
            return {"status": "identical", "behind_by": 0}
        if "/git/ref/heads/" in path:
            if not self.ref:
                raise GitHubError("absent", 404)
            return {"object": {"sha": self.ref}}
        pytest.fail(path)

    def jobs(self, run):
        return [JOB]


def test_publish_and_reconcile_without_duplicate(tmp_path):
    args = prepare(tmp_path)
    api = Publisher()

    def push(checkout, sha, login, branch, token):
        assert token == "machine-only"
        api.ref = sha

    assert deliver(api, args, "machine-only", push) == WEB + "/pull/100"
    assert deliver(api, args, "machine-only", push) == WEB + "/pull/100"
    assert len(api.writes) == 1


def test_refuse_personal_identity_wrong_fork_and_unsigned_change(tmp_path):
    args = prepare(tmp_path)
    api = Publisher()
    api.login = "stmatengss"
    with pytest.raises(ValueError, match="identity"):
        deliver(api, args, "wrong")
    api.login = args.account
    api.parent = 1
    with pytest.raises(ValueError, match="direct fork"):
        deliver(api, args, "wrong")
    parent, _ = reviewed_change(tmp_path, args.reviewed_commit)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-qm", "[CI] unsigned"], check=True
    )
    sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(ValueError, match="DCO"):
        reviewed_change(tmp_path, sha)
    assert parent and not api.writes


def test_real_git_push_preserves_signed_commit_and_never_moves_an_existing_ref(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    args = prepare(checkout)
    remote = tmp_path / "fork.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    git = nightly_publish.git

    def local_push(directory, *git_args, env=None):
        if "push" in git_args:
            assert env["GH_TOKEN"] == "fixture"
            git_args = tuple(
                str(remote) if arg == "https://github.com/fixture-machine/Mooncake.git" else arg
                for arg in git_args
            )
        return git(directory, *git_args, env=env)

    monkeypatch.setattr(nightly_publish, "git", local_push)
    branch = "sima/nightly-8-fixture"
    nightly_publish.push_commit(checkout, args.reviewed_commit, args.account, branch, "fixture")
    assert git(remote, "rev-parse", branch) == args.reviewed_commit
    parent = git(checkout, "rev-parse", "HEAD^")
    with pytest.raises(ValueError):
        nightly_publish.push_commit(checkout, parent, args.account, branch, "fixture")
    assert git(remote, "rev-parse", branch) == args.reviewed_commit
