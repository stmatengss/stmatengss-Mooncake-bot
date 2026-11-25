"""Deliver one explicitly reviewed Git commit without exposing its credential to model tools."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .github import GitHubError
from .nightly import API, BAD, REPO, WEB, get_run, gh_token
from .upstream import UPSTREAM, UPSTREAM_ID


def add_options(parser):
    parser.add_argument("--account", required=True, help="Login of the independent machine user")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--job", required=True, type=int)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument(
        "--reviewed-commit", required=True, help="Full SHA of the signed-off commit"
    )
    parser.add_argument(
        "--body-file", required=True, type=Path, help="Completed upstream PR template"
    )


def git(directory, *args, env=None):
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=150,
    )
    if result.returncode:
        raise ValueError(f"Git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def reviewed_change(directory, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Specify the exact commit that was reviewed")
    parents = git(directory, "show", "-s", "--format=%P", sha).split()
    if len(parents) != 1:
        raise ValueError("Expected one reviewed non-merge commit based on upstream main")
    author = git(directory, "show", "-s", "--format=%an <%ae>", sha)
    trailers = git(directory, "show", "-s", "--format=%(trailers:key=Signed-off-by,valueonly)", sha)
    if author not in trailers.splitlines():
        raise ValueError("Commit requires a DCO sign-off matching its actual author")
    title = git(directory, "show", "-s", "--format=%s", sha)
    if not re.match(r"^\[[^\]\n]+\]\s*\S", title):
        raise ValueError("Use a Mooncake PR title prefix")
    return parents[0], title


def push_commit(checkout, sha, login, branch, token):
    # An isolated bare repo keeps checkout hooks, credential helpers, URL rewrites and
    # remote configuration out of the authenticated push. Transfer Git objects unchanged.
    with tempfile.TemporaryDirectory() as directory:
        bare = Path(directory) / "push.git"
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": directory,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GH_PROMPT_DISABLED": "1",
        }
        if not shutil.which("gh", path=env["PATH"]):
            raise ValueError("Install gh for authenticated pushes to the machine fork")
        git(directory, "init", "--bare", "--quiet", str(bare), env=env)
        # A temporary alternates file avoids fetching a full copy of Mooncake.
        objects = git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        (bare / "objects/info/alternates").write_text(objects + "\n")
        git(
            bare,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            "--porcelain",
            # Empty expected value is create-only, including in a race with another publisher.
            f"--force-with-lease=refs/heads/{branch}:",
            f"https://github.com/{login}/Mooncake.git",
            f"{sha}:refs/heads/{branch}",
            env={**env, "GH_TOKEN": token},
        )


def deliver(api, args, token, push=push_commit):
    login = args.account.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?", login) or login == "stmatengss":
        raise ValueError("Set the independent machine user's login, separate from the maintainer")
    actor = api.request("GET", "/user")
    if actor.get("type") != "User" or actor["login"].lower() != login:
        raise ValueError("Authenticated identity does not match the enrolled machine user")
    fork_api = f"/repos/{login}/Mooncake"
    fork = api.request("GET", fork_api)
    if (
        not fork["fork"]
        or fork["owner"]["id"] != actor["id"]
        or fork["parent"]["id"] != UPSTREAM_ID
        or fork["parent"]["full_name"].lower() != UPSTREAM.lower()
    ):
        raise ValueError("Machine account must own a direct fork of upstream Mooncake")
    sha = args.reviewed_commit
    parent, title = reviewed_change(args.checkout, sha)
    branch = f"sima/nightly-{args.issue}-{sha[:12]}"
    selector = {"head": f"{login}:{branch}", "base": "main", "state": "all"}

    def existing():
        rows = api.request("GET", REPO + "/pulls", params=selector)
        if not rows:
            return None
        pr = rows[0]
        if (
            pr["head"]["sha"] != sha
            or pr["user"]["id"] != actor["id"]
            or pr["head"]["repo"]["id"] != fork["id"]
            or pr["base"]["repo"]["id"] != UPSTREAM_ID
        ):
            raise ValueError("A different pull request already uses the proposed head")
        return pr["html_url"]

    if url := existing():
        return url
    run = get_run(api, args.issue)
    if run["status"] != "completed" or run["conclusion"] not in BAD:
        raise ValueError("The current nightly attempt no longer has a completed failure")
    if not any(
        j["id"] == args.job and j["conclusion"] in BAD and j["name"] != "nightly-gate"
        for j in api.jobs(run)
    ):
        raise ValueError("Select a failed non-gate job from the current nightly attempt")
    body = args.body_file.read_text().strip()
    if (
        not body
        or "## AI Assistance Disclosure" not in body
        or "## How Has This Been Tested?" not in body
    ):
        raise ValueError("Fill out the upstream template including validation and AI assistance")
    compare = api.request("GET", REPO + f"/compare/{parent}...main")
    if compare["status"] not in {"identical", "ahead"} or compare["behind_by"]:
        raise ValueError("The reviewed commit's parent must be on upstream main")
    body += (
        f"\n\nNightly evidence: [issue #{args.issue}]({WEB}/issues/{args.issue}), "
        f"[failed job, attempt {run['run_attempt']}]({WEB}/actions/runs/{run['id']}/job/{args.job})."
    )
    try:
        ref = api.request("GET", fork_api + "/git/ref/heads/" + branch)
    except GitHubError as exc:
        if exc.status_code != 404:
            raise
        push(args.checkout, sha, login, branch, token)
        ref = api.request("GET", fork_api + "/git/ref/heads/" + branch)
    if ref["object"]["sha"] != sha:
        raise ValueError("Fork branch differs from the reviewed commit")
    try:
        pr = api.request(
            "POST",
            REPO + "/pulls",
            json={
                "head": selector["head"],
                "base": "main",
                "draft": True,
                "title": title,
                "body": body,
            },
        )
    except GitHubError:
        # Another worker may have created the PR, or the response may have been lost.
        if url := existing():
            return url
        raise
    if (
        not pr.get("draft")
        or pr["head"]["sha"] != sha
        or pr["user"]["id"] != actor["id"]
        or pr["head"]["repo"]["id"] != fork["id"]
        or pr["base"]["repo"]["id"] != UPSTREAM_ID
    ):
        raise ValueError("GitHub response did not confirm the expected draft PR")
    return pr["html_url"]


def publish(args):
    token = gh_token(args.account)
    api = API(token)
    try:
        return deliver(api, args, token)
    finally:
        api.client.close()
