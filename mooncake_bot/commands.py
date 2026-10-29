"""Maintainer commands and deduplicated automation tasks; no model-controlled routing."""

import hashlib
import re

COMMANDS = {
    "review",
    "summary",
    "describe",
    "title",
    "ask",
    "plan",
    "triage",
    "fix",
    "explain-ci",
    "rerun-ci",
    "help",
    "status",
}
BUILTIN = {"help", "status", "title", "auto-title", "rerun-ci"}
PR_ONLY = {"review", "summary", "describe", "title", "explain-ci", "rerun-ci"}
AUTO_FLAGS = {
    "auto-review": "auto_review",
    "auto-summary": "auto_summary",
    "auto-title": "auto_title",
    "auto-triage": "auto_triage",
    "auto-ci": "auto_ci",
}
SKIP = "<!-- mooncake-bot SKIP -->"
PREFIX = re.compile(
    r"^\[(?:Bugfix|Bug fix|CI/Build|CI|Build|Doc|Docs|Conductor|Integration|"
    r"P2PStore|Store|TransferEngine|Misc|Feature|EP|MUSA|PG|TE|TENT|Wheel)\]"
)


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def normalized_title(title):
    if PREFIX.match(title):
        return title
    if not title.strip() or len(title) > 220 or "\n" in title or "\r" in title:
        raise ValueError("PR title cannot be normalized safely")
    lowered = title.lower()
    category = "Misc"
    for prefix, words in [
        ("Bugfix", ["fix", "bug", "修复"]),
        ("Doc", ["doc", "文档"]),
        ("CI/Build", ["ci", "build", "test", "构建", "测试"]),
        ("Feature", ["feat", "add", "support", "新增", "支持"]),
    ]:
        if any(lowered.startswith(word) for word in words):
            category = prefix
            break
    return f"[{category}] {title}"


def automatic_task(config, item, command, kind="pr", run=None):
    flag = AUTO_FLAGS[command]
    if not getattr(config, flag) or item.get("state") != "open":
        return None
    authors = config.automation_author_ids or config.allowed_actor_ids
    if item["user"]["id"] not in authors or SKIP in (item.get("body") or ""):
        return None
    if kind == "pr" and item.get("draft") and command != "auto-title":
        return None
    if command == "auto-title" and normalized_title(item["title"]) == item["title"]:
        return None
    revision = (
        item["head"]["sha"] + ":" + item["base"]["sha"]
        if kind == "pr"
        else fingerprint(item["title"] + "\0" + (item.get("body") or ""))
    )
    if command == "auto-title":
        revision = fingerprint(item["title"])
    if run:
        revision += f":{run['id']}:{run.get('run_attempt', 1)}"
    return {
        "source": f"{command}:{item['number']}:{revision}",
        "event": "automation",
        "automatic": True,
        "kind": kind,
        "installation": config.installation_ids[0],
        "number": item["number"],
        "actor": item["user"]["login"],
        "actor_id": item["user"]["id"],
        "command": command,
        "body": "Analyze the current revision using repository guidance.",
        "expected_head": item["head"]["sha"] if kind == "pr" else None,
        "expected_base": item["base"]["sha"] if kind == "pr" else None,
        "issue_revision": fingerprint(item["title"] + "\0" + (item.get("body") or "")),
        "run_id": run["id"] if run else None,
        "run_attempt": run.get("run_attempt", 1) if run else None,
        "expected_title": item["title"] if command == "auto-title" else None,
    }


def pr_tasks(config, pr):
    commands = ["auto-title"]
    commands += ["auto-review"] if config.auto_review else ["auto-summary"]
    return [task for cmd in commands if (task := automatic_task(config, pr, cmd))]


def parse_tasks(config, event, payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected an object")
    comments = event in {"issue_comment", "pull_request_review_comment"}
    if not comments and event not in {"pull_request", "issues", "workflow_run"}:
        return []
    if comments and payload.get("action") != "created":
        return []
    if not comments and not any(getattr(config, name) for name in AUTO_FLAGS.values()):
        return []
    repo = payload["repository"]
    if repo["id"] != config.repository_id or repo["full_name"] != config.repository:
        raise ValueError("Repository is not enrolled")
    installation = payload["installation"]["id"]
    if installation not in config.installation_ids:
        raise ValueError("Installation is not enrolled")
    if not comments:
        if event == "pull_request" and payload.get("action") in {
            "opened",
            "reopened",
            "synchronize",
            "ready_for_review",
            "edited",
        }:
            return pr_tasks(config, payload["pull_request"])
        if event == "issues" and payload.get("action") in {"opened", "reopened", "edited"}:
            task = automatic_task(config, payload["issue"], "auto-triage", "issue")
            return [task] if task else []
        # Workflow associations are resolved against GitHub by the periodic scanner.
        return []
    actor, comment = payload["sender"], payload["comment"]
    if actor["type"] != "User" or actor["id"] not in config.allowed_actor_ids:
        return []
    if comment["user"]["id"] != actor["id"]:
        raise ValueError("Comment author and sender do not match")
    match = re.match(
        r"^\s*@" + re.escape(config.mention) + r"(?=\s|$)\s*(.*)", comment["body"], re.I | re.S
    )
    if not match:
        return []
    request = match[1].strip()
    if len(request) > 8000:
        raise ValueError("Command exceeds the request limit")
    word = request.split(maxsplit=1)[0].lower() if request else "help"
    if request.lower().startswith("please fix "):
        word = "fix"
    word = {"summarize": "summary", "retry-ci": "rerun-ci"}.get(word, word)
    command = word if word in COMMANDS else "ask"
    if command == "fix" and not config.enable_fix_prs:
        return []
    item = payload.get("issue") or payload.get("pull_request")
    kind = "pr" if event == "pull_request_review_comment" or "pull_request" in item else "issue"
    return [
        {
            "source": f"{event}:{comment['id']}",
            "event": event,
            "installation": installation,
            "number": item["number"],
            "kind": kind,
            "actor": actor["login"],
            "actor_id": actor["id"],
            "comment_id": comment["id"],
            "body": comment["body"],
            "command": command,
        }
    ]


def help_text(config):
    mention = "@" + config.mention
    return "\n".join(
        [
            "### Mooncake bot commands",
            "",
            *[
                f"- `{mention} {cmd}`"
                for cmd in [
                    "review",
                    "summary",
                    "describe",
                    "title",
                    "ask <question>",
                    "plan",
                    "triage",
                    "fix <request with a repository file path>",
                    "explain-ci",
                    "rerun-ci [run-id]",
                    "status",
                    "help",
                ]
            ],
            "",
            "Review posts one COMMENT review with validated inline findings. Summary updates a bot "
            "comment; describe updates only a marked bot block in the PR description. "
            "Fix creates a bounded draft PR; CI retry restarts failed jobs on the current PR revision.",
            "",
            f"Shared model budget: {config.max_model_calls_per_day}/UTC day, including RSI. "
            "Help, status, title and CI retry use no model. Only enrolled maintainers may issue commands.",
        ]
    )
