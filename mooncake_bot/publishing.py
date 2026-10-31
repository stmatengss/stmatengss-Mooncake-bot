"""Trusted publishers. Model output never chooses API routes, branches or credentials."""

import re
import threading

from .agent import check_result
from .commands import normalized_title
from .github import GitHubError
from .rsi import apply_change, atomic_json
from .store import Store

DESCRIPTION_START = "<!-- mooncake-bot:description:start -->"
DESCRIPTION_END = "<!-- mooncake-bot:description:end -->"
FIX_PUBLISH_LOCK = threading.Lock()


def managed_description(original, block):
    original = original or ""
    if original.count(DESCRIPTION_START) != original.count(DESCRIPTION_END):
        raise ValueError("Unbalanced bot description markers")
    if original.count(DESCRIPTION_START) > 1:
        raise ValueError("Multiple bot description blocks")
    while DESCRIPTION_START in block or DESCRIPTION_END in block:
        block = block.replace(DESCRIPTION_START, "").replace(DESCRIPTION_END, "")
    replacement = DESCRIPTION_START + "\n" + block + "\n" + DESCRIPTION_END
    if DESCRIPTION_START in original:
        start, end = original.index(DESCRIPTION_START), original.index(DESCRIPTION_END)
        if end < start:
            raise ValueError("Invalid bot description marker order")
        result = original[:start] + replacement + original[end + len(DESCRIPTION_END) :]
    else:
        result = original + ("\n\n" if original else "") + replacement
    if len(result) > 60000:
        raise ValueError("Description exceeds the publication limit")
    return result


def publish(github, task, context, body):
    current = github.assert_current(task, context)
    command = task["command"]
    token = github.token(task["installation"], write=True)

    def write(method, suffix, data):
        return github.request(method, github.prefix + suffix, token, json=data)

    def comment(message):
        return write("POST", f"/issues/{task['number']}/comments", {"body": message})["html_url"]

    if command in {"title", "auto-title"} and context.get("kind", "pr") == "pr":
        title = normalized_title(current["title"])
        changed = title != current["title"]
        if changed:
            write("PATCH", f"/pulls/{task['number']}", {"title": title})
        if command == "auto-title":
            return current["html_url"]
        return comment(body + f"\n\nTitle {'updated' if changed else 'already conforms'}: {title}")
    if command == "describe" and context.get("kind", "pr") == "pr":
        # Re-read author text immediately before replacing the managed block.
        current = github.assert_current(task, context)
        updated = managed_description(current.get("body"), body)
        write("PATCH", f"/pulls/{task['number']}", {"body": updated})
        return comment(
            "Updated only the bot-managed description block for `"
            + context["head"][:12]
            + "`. Human text and checklists were preserved."
        )
    if command == "rerun-ci" and context.get("_ci_run"):
        run = context["_ci_run"]
        fresh = github.get(github.prefix + f"/actions/runs/{run['id']}", task)
        if (
            fresh["head_sha"] != context["head"]
            or fresh.get("run_attempt", 1) != run["attempt"]
            or fresh.get("conclusion") not in {"failure", "timed_out"}
        ):
            raise GitHubError("Workflow changed; no CI retry was started")
        key = f"ci-retry:{run['id']}:{run['attempt']}"
        if not Store(github.config.path("data_dir")).reserve_action(key):
            return comment(
                body + "\n\nA retry was already attempted for this run/attempt; "
                "no additional retry was submitted."
            )
        action_token = github.token(task["installation"], actions_write=True)
        github.request(
            "POST", github.prefix + f"/actions/runs/{run['id']}/rerun-failed-jobs", action_token
        )
        return comment(
            body + "\n\nGitHub accepted the failed-jobs retry request. "
            "This confirms scheduling, not test success."
        )
    if context.get("_fix"):
        proposal = publish_fix(github, task, context)
        return comment(body + "\n\nCreated draft PR: " + proposal["html_url"])
    if command in {"review", "auto-review"} and context.get("kind", "pr") == "pr":
        result = context.get("_result", {"summary": "", "findings": [], "limitations": []})
        check_result({k: result[k] for k in ["summary", "findings", "limitations"]}, context)
        existing = github.pages(github.prefix + f"/pulls/{task['number']}/comments", task)
        own = github.actor_login()
        already = {
            (r["path"], r.get("line"))
            for r in existing
            if r["user"]["login"] == own and r.get("commit_id") == context["head"]
        }
        comments = [
            {
                "path": r["path"],
                "line": r["line"],
                "side": "RIGHT",
                "body": f"**[P{r['priority']}]** {r['body']}",
            }
            for r in result["findings"]
            if (r["path"], r["line"]) not in already
        ]
        response = write(
            "POST",
            f"/pulls/{task['number']}/reviews",
            {
                "commit_id": context["head"],
                "event": "COMMENT",
                "body": body,
                "comments": comments,
            },
        )
        return response["html_url"]
    if command in {"summary", "auto-summary", "auto-triage", "auto-ci"}:
        marker = f"<!-- mooncake-bot:{command.removeprefix('auto-')}:{task['number']} -->"
        own = github.actor_login()
        comments = github.pages(github.prefix + f"/issues/{task['number']}/comments", task)
        previous = next(
            (r for r in comments if r["user"]["login"] == own and r["body"].startswith(marker)),
            None,
        )
        content = {"body": marker + "\n" + body}
        if previous:
            return write("PATCH", f"/issues/comments/{previous['id']}", content)["html_url"]
        return write("POST", f"/issues/{task['number']}/comments", content)["html_url"]
    return comment(body)


def file_mode(github, base, path, task):
    parent = github.get(github.prefix + "/git/commits/" + base, task)
    tree = parent["tree"]["sha"]
    parts = path.split("/")
    for index, name in enumerate(parts):
        entries = github.get(github.prefix + "/git/trees/" + tree, task)
        if entries.get("truncated"):
            raise GitHubError("Cannot validate an incomplete Git tree")
        entry = next((r for r in entries["tree"] if r["path"] == name), None)
        if not entry:
            raise GitHubError("Fix target does not exist in the selected revision")
        if index == len(parts) - 1:
            if entry["type"] != "blob" or entry["mode"] not in {"100644", "100755"}:
                raise GitHubError("Fix targets must be regular tracked files")
            return entry["mode"]
        if entry["type"] != "tree":
            raise GitHubError("Fix target traverses a non-directory Git entry")
        tree = entry["sha"]
    raise GitHubError("Empty fix path")


def publish_fix(github, task, context):
    with FIX_PUBLISH_LOCK:
        return _publish_fix(github, task, context)


def _publish_fix(github, task, context):
    from .features import fix_allowed

    candidate = context["_fix"]
    config = github.config
    if not config.enable_fix_prs or not fix_allowed(config, candidate["path"]):
        raise GitHubError("Fix publication target is no longer enabled")
    if (
        context.get("kind", "pr") == "pr"
        and context.get("head_repository_id") != config.repository_id
    ):
        raise GitHubError("Fix base must belong to the enrolled repository")
    if candidate["review"]["findings"]:
        raise GitHubError("Fix candidate did not pass static review")
    opened = github.pages(github.prefix + "/pulls", task)
    if sum(r["head"]["ref"].startswith("sima/bot-fix-") for r in opened) >= config.max_fix_prs:
        raise GitHubError("Open fix draft limit reached")
    directory = context["_directory"]
    if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise GitHubError("Fix task has an invalid trusted identifier")
    receipt = directory / "publication.json"
    if receipt.exists():
        raise GitHubError("A fix publication was already attempted; inspect its receipt")
    base = context["head"]
    original = github.file(candidate["path"], base, task)
    updated, patch = apply_change(candidate["path"], original, candidate["proposal"]["change"])
    if updated != candidate["updated"] or patch != candidate["patch"]:
        raise GitHubError("Fix no longer matches the reviewed artifact")
    mode = file_mode(github, base, candidate["path"], task)
    token = github.token(task["installation"], write=True, contents_write=True)

    def post(suffix, value):
        return github.request("POST", github.prefix + suffix, token, json=value)

    parent = github.get(github.prefix + "/git/commits/" + base, task)
    tree = post(
        "/git/trees",
        {
            "base_tree": parent["tree"]["sha"],
            "tree": [
                {
                    "path": candidate["path"],
                    "mode": mode,
                    "type": "blob",
                    "content": updated,
                }
            ],
        },
    )
    title = "[Bugfix] " + candidate["proposal"]["summary"].splitlines()[0][:140]
    commit = post("/git/commits", {"message": title, "tree": tree["sha"], "parents": [base]})
    github.assert_current(task, context)
    branch = "sima/bot-fix-" + directory.name
    atomic_json(receipt, {"branch": branch, "commit": commit["sha"], "phase": "before_branch"})
    post("/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
    syntax = (
        "Python syntax" if candidate["path"].endswith(".py") else "Patch bounds (not compilation)"
    )
    body = (
        "## Description\n\n"
        + candidate["proposal"]["summary"]
        + f"\n\nRequested from #{task['number']}; source revision `{base}`.\n\n"
        "## Module\n\n- [x] Other: maintainer-requested bounded fix\n\n"
        "## Type of Change\n\n- [x] Bug fix\n\n## How Has This Been Tested?\n\n"
        + syntax
        + " and one exact snippet checked; a separate static review found no actionable "
        "defect. No repository code, unit tests, GPU or RDMA workload was executed by this worker.\n\n"
        "Proposed checks (not executed):\n"
        + candidate["proposal"]["test_plan"]
        + "\n\n## Checklist\n\n- [ ] Human reviewed every changed line\n"
        "- [ ] Relevant tests and pre-commit pass\n\n## AI Assistance Disclosure\n\n"
        "- [x] Kimi Code proposed and reviewed this patch. The trusted publisher created this "
        "draft. It does not imply test success or approval.\n"
    )
    response = post(
        "/pulls",
        {"head": branch, "base": context["head_ref"], "draft": True, "title": title, "body": body},
    )
    atomic_json(
        receipt,
        {
            "branch": branch,
            "commit": commit["sha"],
            "phase": "draft_created",
            "number": response["number"],
            "html_url": response["html_url"],
        },
    )
    return response
