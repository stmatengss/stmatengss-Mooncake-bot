"""Bounded command implementations, with a model budget shared with RSI."""

import json
import re
from pathlib import PurePosixPath

from .agent import check_result
from .commands import BUILTIN, PR_ONLY, help_text
from .context import clip, compact_context
from .kimi import invoke
from .publishing import file_mode
from .rsi import DATA_POLICY, PROPOSAL, apply_change, atomic_json, digest
from .rsi_state import State


def builtin(summary):
    return {"summary": summary, "findings": [], "limitations": [], "builtin": True}


def fix_allowed(config, name):
    path = PurePosixPath(name)
    return (
        not name.startswith("/")
        and ".." not in path.parts
        and ".git" not in path.parts
        and ".github" not in path.parts
        and path.name not in {"AGENTS.md", "CONTRIBUTING.md", "CLAUDE.md", "REVIEW.md"}
        and path.suffix
        in {".py", ".cpp", ".cc", ".c", ".h", ".hpp", ".cu", ".cuh", ".rs", ".go", ".md"}
        and any(name.startswith(prefix) for prefix in config.fix_paths)
    )


def prepare_retry(github, task, context):
    if context.get("head_repository_id") != github.config.repository_id:
        return builtin("CI retries are limited to this repository's own PR branches.")
    match = re.search(r"(?:rerun-ci|retry-ci)(?:\s+(\d+))?\s*$", task["body"], re.I)
    if not match:
        return builtin("Use `rerun-ci` or `rerun-ci <run-id>`.")
    if match[1]:
        runs = [github.get(github.prefix + f"/actions/runs/{int(match[1])}", task)]
    else:
        runs = github.get(
            github.prefix + "/actions/runs",
            task,
            params={"head_sha": context["head"], "per_page": 30},
        )["workflow_runs"]
    for run in runs:
        if (
            run["head_sha"] != context["head"]
            or run.get("status") != "completed"
            or run.get("conclusion") not in {"failure", "timed_out"}
            or run.get("event") != "pull_request"
            or run.get("head_repository", {}).get("id") != github.config.repository_id
        ):
            continue
        attempt = run.get("run_attempt", 1)
        if attempt >= 3:
            return builtin("This workflow has already reached the three-attempt retry limit.")
        context["_ci_run"] = {"id": run["id"], "attempt": attempt}
        return builtin(
            f"Current revision `{context['head'][:12]}` has failed workflow "
            f"[{run['name']}]({run['html_url']}). Only its failed jobs are eligible."
        )
    return builtin(
        "No completed failed workflow eligible for retry was found on the current PR head."
    )


def status(config, store, task, context, budget):
    rows = [r for r in store.list() if r["number"] == task["number"]][:6]
    lines = [
        f"Shared model budget: **{budget.used()}/{config.max_model_calls_per_day}** "
        f"on UTC date `{budget.day()}`.",
        "",
        "Recent tasks on this issue/PR:",
    ]
    for row in rows:
        request = json.loads(row["payload"])
        state = row["state"] + (" (waiting for UTC reset)" if row.get("not_before", 0) else "")
        lines.append(
            f"- `{row['id'][:12]}` · `{request['command']}` · {state}"
            + (f" · [result]({row['published_url']})" if row.get("published_url") else "")
        )
    for row in budget.recent()[:3]:
        outcome = json.loads(row["result"])
        lines.append(
            f"- RSI `{row['kind']}` · {row['state']}"
            + (f" · [proposal]({outcome['url']})" if outcome.get("url") else "")
        )
    lines += [
        "",
        f"Analyzed revision: `{context['head']}`.",
        "Task logs, configuration, credentials and raw model output are private.",
    ]
    return builtin("\n".join(lines))


async def run(config, store, github, task, context, directory, analyzer):
    command = task["command"]
    budget = State(config.path("data_dir") / "rsi")
    if context.get("kind", "pr") == "issue" and command in PR_ONLY:
        return builtin(
            "This command requires a PR. Use ask, plan, triage, fix, help or status on issues."
        )
    if command == "help":
        return builtin(help_text(config))
    if command == "status":
        return status(config, store, task, context, budget)
    if command in {"title", "auto-title"}:
        return builtin("Check this PR title against Mooncake's documented title prefixes.")
    if command == "rerun-ci":
        return prepare_retry(github, task, context)
    if command == "fix":
        return await propose_fix(config, github, task, context, directory, analyzer, budget)
    assert command not in BUILTIN
    budget.reserve(config.max_model_calls_per_day)
    return await analyzer(config, task, context, directory)


async def propose_fix(config, github, task, context, directory, analyzer, budget):
    if not config.enable_fix_prs or config.backend != "kimi":
        return builtin("Bounded draft fixes require an enabled Kimi backend.")
    if (
        context.get("kind", "pr") == "pr"
        and context.get("head_repository_id") != config.repository_id
    ):
        return builtin(
            "Draft fixes require a branch in this repository. Fork PRs remain reviewable."
        )
    files, used = [], 0
    for row in context["files"]:
        length = len((row.get("content") or "").encode())
        if fix_allowed(config, row["filename"]) and 0 < length <= 30000 and used + length <= 40000:
            files.append(row)
            used += length
        if len(files) == 3:
            break
    if not files:
        return builtin(
            "No enrolled existing source file is available. Include an allowed repository "
            "file path in the issue/request, or request a plan instead."
        )
    opened = github.pages(github.prefix + "/pulls", task)
    if sum(r["head"]["ref"].startswith("sima/bot-fix-") for r in opened) >= config.max_fix_prs:
        return builtin(
            "The open fix-draft limit is reached. Review an existing draft before requesting another."
        )
    guidance = compact_context({"guidance": context.get("guidance", {})}, 16000)
    data = {
        "request": task["body"],
        "title": context["title"],
        "body": clip(context["body"] or "", 8000),
        "head": context["head"],
        "base": context["base"],
        "files": [
            {
                "filename": r["filename"],
                "content": r["content"],
                "patch": clip(r.get("patch") or "", 4000),
            }
            for r in files
        ],
        "guidance": guidance["guidance"],
        "coverage_limits": guidance.get("coverage_limits", []),
        "goal": "Propose at most one concrete correctness fix in one provided existing file. "
        "Return one exact before/after snippet, at most 80 changed lines, or change=null. "
        "Preserve unrelated behavior and follow repository guidance. Tests are proposals, not results.",
    }
    signature = digest(json.dumps(data, sort_keys=True, ensure_ascii=False))
    identity = directory / "fix-input.json"
    if identity.exists() and json.loads(identity.read_text())["fingerprint"] != signature:
        raise ValueError("Fix context changed; submit a new request")
    atomic_json(identity, {"fingerprint": signature})
    cached = directory / "proposal/result.json"
    if cached.exists():
        proposal = json.loads(cached.read_text())
    else:
        budget.reserve(config.max_model_calls_per_day)
        proposal = await invoke(config, data, PROPOSAL, DATA_POLICY, directory / "proposal")
    if proposal["change"] is None:
        return {
            "summary": proposal["summary"],
            "findings": [],
            "limitations": ["No bounded patch established; no branch was created."],
        }
    change = proposal["change"]
    original = next((r["content"] for r in files if r["filename"] == change["path"]), None)
    if original is None or not fix_allowed(config, change["path"]):
        raise ValueError("Fix selected a file outside the supplied enrolled sources")
    if not proposal["summary"].strip():
        raise ValueError("Fix requires a substantive summary")
    updated, patch = apply_change(change["path"], original, change)
    file_mode(github, context["head"], change["path"], task)
    (directory / "candidate.patch").write_text(patch)
    review_context = {
        "head": context["head"],
        "body": proposal["summary"],
        "guidance": context.get("guidance", {}),
        "files": [
            {
                "filename": change["path"],
                "content": updated,
                "patch": patch,
            }
        ],
    }
    review_file = directory / "fix-review/result.json"
    if review_file.exists():
        review = json.loads(review_file.read_text())
    else:
        budget.reserve(config.max_model_calls_per_day)
        review = await analyzer(
            config,
            {"command": "review", "body": "Review only this candidate patch."},
            review_context,
            directory / "fix-review",
        )
    check_result(review, review_context)
    if review["findings"]:
        return {
            "summary": "The candidate did not pass independent static review; no draft was published.",
            "findings": [],
            "limitations": [review["summary"]],
        }
    context["_fix"] = {
        "path": change["path"],
        "updated": updated,
        "patch": patch,
        "proposal": proposal,
        "review": review,
    }
    return {
        "summary": proposal["summary"],
        "findings": [],
        "limitations": [
            "An exact bounded patch and an independent static review passed. "
            "Repository tests and full compilation were not executed. Human review is required."
        ],
    }
