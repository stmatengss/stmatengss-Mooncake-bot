import base64
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import jwt

from .commands import AUTO_FLAGS, SKIP, fingerprint


class GitHubError(Exception):
    """Safe to display: never contains response bodies, credentials or request URLs."""


class GitHub:
    def __init__(self, config):
        self.config = config
        self.tokens = {}
        self.client = httpx.Client(base_url="https://api.github.com", timeout=30)

    def request(self, method, path, token, **kwargs):
        response = self.client.request(
            method,
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            **kwargs,
        )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub {method} failed (HTTP {response.status_code})")
        return response.json() if response.content else None

    def token(self, installation, write=False, contents_write=False, actions_write=False):
        if installation not in self.config.installation_ids:
            raise GitHubError("Installation is not enrolled")
        key = (installation, write, contents_write, actions_write)
        cached = self.tokens.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        permissions = {
            "contents": "write" if contents_write else "read",
            "issues": "write" if write else "read",
            "pull_requests": "write" if write else "read",
            "checks": "read",
            "statuses": "read",
            "actions": "write" if actions_write else "read",
        }
        result = self.request(
            "POST",
            f"/app/installations/{installation}/access_tokens",
            self.assertion(),
            json={"repository_ids": [self.config.repository_id], "permissions": permissions},
        )
        self.tokens[key] = (time.time() + 3000, result["token"])
        return result["token"]

    def assertion(self):
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.config.app_id},
            self.config.path("app_key_file").read_text(),
            algorithm="RS256",
        )

    def get(self, path, task, **kwargs):
        return self.request("GET", path, self.token(task["installation"]), **kwargs)

    def pages(self, path, task):
        rows = []
        for page in range(1, 11):
            batch = self.get(path, task, params={"per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise GitHubError("Expected a paginated list")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise GitHubError("Context exceeds pagination limit; refusing incomplete results")

    @property
    def prefix(self):
        return f"/repos/{self.config.repository}"

    def authorize(self, task):
        if task.get("automatic"):
            flag = AUTO_FLAGS.get(task["command"])
            if not flag or not getattr(self.config, flag):
                raise GitHubError("This automation is no longer enabled")
            route = "pulls" if task.get("kind", "pr") == "pr" else "issues"
            item = self.get(self.prefix + f"/{route}/{task['number']}", task)
            authors = self.config.automation_author_ids or self.config.allowed_actor_ids
            if (
                item["user"]["id"] != task["actor_id"]
                or task["actor_id"] not in authors
                or item["state"] != "open"
                or SKIP in (item.get("body") or "")
            ):
                raise GitHubError("Automatic task is no longer authorized")
            if route == "pulls":
                if (item["head"]["sha"], item["base"]["sha"]) != (
                    task["expected_head"],
                    task["expected_base"],
                ) or (item.get("draft") and task["command"] != "auto-title"):
                    raise GitHubError("Automatic task revision changed")
                if task["command"] == "auto-title" and item["title"] != task["expected_title"]:
                    raise GitHubError("PR title changed")
            elif (
                fingerprint(item["title"] + "\0" + (item.get("body") or ""))
                != task["issue_revision"]
            ):
                raise GitHubError("Issue changed while the task was queued")
            if task.get("run_id"):
                run = self.get(self.prefix + f"/actions/runs/{task['run_id']}", task)
                if (
                    run["head_sha"] != task["expected_head"]
                    or run.get("run_attempt", 1) != task["run_attempt"]
                    or run.get("conclusion") not in {"failure", "timed_out"}
                ):
                    raise GitHubError("Failed workflow revision changed")
            if item["user"]["type"] == "Bot":
                if item["user"]["login"] != self.actor_login():
                    raise GitHubError("Only this App's own PRs may be automated")
                return
            self.authorize_actor(task, item["user"]["login"])
            return
        self.authorize_actor(task, task["actor"])
        route = "issues" if task["event"] == "issue_comment" else "pulls"
        comment = self.get(self.prefix + f"/{route}/comments/{task['comment_id']}", task)
        if comment["user"]["id"] != task["actor_id"] or comment["body"] != task["body"]:
            raise GitHubError("Triggering comment changed; submit a new request")

    def authorize_actor(self, task, login):
        if task["actor_id"] not in self.config.allowed_actor_ids:
            raise GitHubError("Actor is no longer allowed")
        person = self.get(f"/users/{quote(login, safe='')}", task)
        if person["id"] != task["actor_id"]:
            raise GitHubError("Actor identity changed")
        permission = self.get(
            self.prefix + f"/collaborators/{quote(login, safe='')}/permission", task
        )
        if permission["permission"] not in {"admin", "write", "maintain"}:
            raise GitHubError("Current repository write permission is required")

    def actor_login(self):
        if not hasattr(self, "_actor_login"):
            app = self.request("GET", "/app", self.assertion())
            self._actor_login = app["slug"] + "[bot]"
        return self._actor_login

    def file(self, name, sha, task):
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            return None
        try:
            result = self.get(
                self.prefix + "/contents/" + quote(name, safe="/"), task, params={"ref": sha}
            )
        except GitHubError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        if isinstance(result, list) or result.get("size", 0) > 150_000:
            return None
        if result.get("encoding") != "base64":
            return None
        return base64.b64decode(result["content"]).decode("utf-8", errors="replace")

    def context(self, task):
        self.authorize(task)
        if task.get("kind") == "issue":
            return self.issue_context(task)
        pr = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if pr["state"] != "open":
            raise GitHubError("PR is no longer open")
        context = {
            "kind": "pr",
            "repository": self.config.repository,
            "number": task["number"],
            "title": pr["title"],
            "body": pr["body"],
            "head": pr["head"]["sha"],
            "base": pr["base"]["sha"],
            "files": [],
            "guidance": {},
            "checks": {},
            "head_ref": pr["head"]["ref"],
            "head_repository_id": pr["head"]["repo"]["id"],
            "base_ref": pr["base"]["ref"],
        }
        if task["command"] in {"help", "status", "title", "auto-title", "rerun-ci"}:
            return context
        files = self.pages(self.prefix + f"/pulls/{task['number']}/files", task)
        if len(files) != pr["changed_files"] or len(files) > 60:
            raise GitHubError("PR file list is incomplete or exceeds the 60-file limit")
        used = 0
        for item in files:
            if task["command"] in {"explain-ci", "auto-ci"}:
                context["files"].append({"filename": item["filename"]})
                continue
            text = self.file(item["filename"], context["head"], task)
            used += len((text or "").encode()) + len(item.get("patch", "").encode())
            if used > 600_000:
                raise GitHubError("PR exceeds the context budget; use a smaller PR")
            context["files"].append(
                {k: item.get(k) for k in ("filename", "status", "patch", "additions", "deletions")}
                | {"content": text}
            )
        # Policies come from the base revision, not a PR's proposed policy changes.
        paths = {"AGENTS.md", "CONTRIBUTING.md", ".github/pull_request_template.md"}
        for item in files:
            parts = item["filename"].split("/")[:-1]
            paths.update("/".join(parts[:i]) + "/AGENTS.md" for i in range(1, len(parts) + 1))
        for path in sorted(paths):
            text = self.file(path, context["base"], task)
            if text:
                context["guidance"][path] = text
        checks = self.get(
            self.prefix + f"/commits/{context['head']}/check-runs", task, params={"per_page": 100}
        )
        context["checks"] = [
            {k: r.get(k) for k in ["name", "status", "conclusion", "html_url"]}
            | {
                "output": {
                    k: (r.get("output", {}).get(k) or "")[:1500]
                    for k in ["title", "summary", "text"]
                }
            }
            for r in checks.get("check_runs", [])[:30]
        ]
        if task["command"] in {"explain-ci", "auto-ci"}:
            context["ci"] = self.ci_logs(context["head"], task)
        previous = self.get(
            self.prefix + f"/pulls/{task['number']}/comments",
            task,
            params={"per_page": 12, "sort": "updated", "direction": "desc"},
        )
        context["previous_comments"] = [
            {k: r.get(k) for k in ["path", "line", "commit_id", "in_reply_to_id"]}
            | {"body": (r.get("body") or "")[:1000], "author": r["user"]["login"]}
            for r in previous[:12]
        ]
        current = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if (current["head"]["sha"], current["base"]["sha"]) != (context["head"], context["base"]):
            raise GitHubError("PR changed while collecting context; submit a new request")
        return context

    def issue_context(self, task):
        issue = self.get(self.prefix + f"/issues/{task['number']}", task)
        if "pull_request" in issue:
            raise GitHubError("Issue context does not match the triggering request")
        repo = self.get(self.prefix, task)
        base_ref = repo["default_branch"]
        head = self.get(self.prefix + "/commits/" + quote(base_ref, safe=""), task)["sha"]
        context = {
            "kind": "issue",
            "repository": self.config.repository,
            "number": task["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "head": head,
            "base": head,
            "base_ref": base_ref,
            "head_ref": base_ref,
            "issue_revision": fingerprint(issue["title"] + "\0" + (issue.get("body") or "")),
            "files": [],
            "guidance": {},
            "checks": {},
        }
        if task["command"] in {"help", "status"}:
            return context
        text = task["body"] + "\n" + context["body"]
        names = re.findall(r"(?:[\w.-]+/)+[\w.-]+\.(?:py|cpp|cc|c|h|hpp|cu|cuh|rs|go|md)\b", text)
        used = 0
        for name in dict.fromkeys(names + ["README.md"]):
            source = self.file(name, head, task)
            if source and used + len(source.encode()) <= 45000:
                context["files"].append({"filename": name, "content": source, "patch": ""})
                used += len(source.encode())
            if len(context["files"]) >= 5:
                break
        guidance = {"AGENTS.md", "CONTRIBUTING.md", ".github/pull_request_template.md"}
        for file in context["files"]:
            parts = file["filename"].split("/")[:-1]
            guidance.update("/".join(parts[:i]) + "/AGENTS.md" for i in range(1, len(parts) + 1))
        for path in sorted(guidance):
            source = self.file(path, head, task)
            if source:
                context["guidance"][path] = source
        labels = self.get(self.prefix + "/labels", task, params={"per_page": 100})
        context["labels"] = [r["name"] for r in labels]
        # Fetch the latest page, rather than silently using the oldest discussion.
        count = issue.get("comments", 0)
        if count:
            comments = self.get(
                self.prefix + f"/issues/{task['number']}/comments",
                task,
                params={"per_page": 10, "page": (count + 9) // 10},
            )
            context["discussion"] = [
                {"author": r["user"]["login"], "body": r["body"][:1500]} for r in comments[-10:]
            ]
        return context

    def ci_logs(self, sha, task):
        result = self.get(
            self.prefix + "/actions/runs", task, params={"head_sha": sha, "per_page": 20}
        )
        runs = []
        for run in result["workflow_runs"]:
            if task.get("run_id") and run["id"] != task["run_id"]:
                continue
            if run["head_sha"] != sha or run["conclusion"] not in {
                "failure",
                "timed_out",
                "cancelled",
            }:
                continue
            jobs = self.get(
                self.prefix + f"/actions/runs/{run['id']}/jobs", task, params={"per_page": 100}
            )
            runs.append(
                {
                    "name": run["name"],
                    "url": run["html_url"],
                    "conclusion": run["conclusion"],
                    "jobs": [
                        {k: j.get(k) for k in ("name", "conclusion", "html_url")}
                        | {
                            "steps": [
                                {k: s.get(k) for k in ("name", "conclusion", "number")}
                                for s in j.get("steps", [])[:30]
                            ]
                        }
                        for j in jobs["jobs"]
                        if j.get("conclusion") in {"failure", "timed_out", "cancelled"}
                    ][:20],
                    "job_list_complete": jobs.get("total_count", 0) <= 100,
                }
            )
            if len(runs) >= 5:
                break
        # This version intentionally exposes check annotations/steps, not secret-bearing raw logs.
        checks = self.get(
            self.prefix + f"/commits/{sha}/check-runs", task, params={"per_page": 100}
        )
        annotations = []
        for check in checks.get("check_runs", []):
            if check.get("conclusion") not in {"failure", "timed_out"}:
                continue
            details = self.get(
                self.prefix + f"/check-runs/{check['id']}/annotations",
                task,
                params={"per_page": 50},
            )
            annotations.extend(
                {k: r.get(k) for k in ["path", "start_line"]}
                | {"message": (r.get("message") or "")[:800]}
                for r in details[:20]
            )
            if len(annotations) >= 50:
                break
        return {
            "runs": runs,
            "annotations": annotations[:50],
            "raw_logs_included": False,
            "limitation": "Diagnosis uses check outputs and failing steps; raw logs are not loaded.",
        }

    def publish(self, task, context, body):
        from .publishing import publish

        return publish(self, task, context, body)

    def assert_current(self, task, context):
        self.authorize(task)
        if context.get("kind") == "issue":
            current = self.get(self.prefix + f"/issues/{task['number']}", task)
            if (
                fingerprint(current["title"] + "\0" + (current.get("body") or ""))
                != context["issue_revision"]
            ):
                raise GitHubError("Issue changed; stale result was not published")
            if context.get("files"):
                head = self.get(
                    self.prefix + "/commits/" + quote(context["base_ref"], safe=""), task
                )["sha"]
                if head != context["head"]:
                    raise GitHubError("Repository changed; stale issue analysis was not published")
            return current
        current = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if (
            current["state"] != "open"
            or current["head"]["sha"] != context["head"]
            or current["base"]["sha"] != context["base"]
        ):
            raise GitHubError("PR changed; stale result was not published")
        return current
