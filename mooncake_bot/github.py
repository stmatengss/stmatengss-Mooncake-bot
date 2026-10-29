import base64
import time
from urllib.parse import quote

import httpx
import jwt


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

    def token(self, installation, write=False, contents_write=False):
        if installation not in self.config.installation_ids:
            raise GitHubError("Installation is not enrolled")
        key = (installation, write, contents_write)
        cached = self.tokens.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        permissions = {
            "contents": "write" if contents_write else "read",
            "issues": "write" if write else "read",
            "pull_requests": "write" if write else "read",
            "checks": "read",
            "statuses": "read",
            "actions": "read",
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
        if task["actor_id"] not in self.config.allowed_actor_ids:
            raise GitHubError("Actor is no longer allowed")
        person = self.get(f"/users/{quote(task['actor'], safe='')}", task)
        if person["id"] != task["actor_id"]:
            raise GitHubError("Actor identity changed")
        permission = self.get(
            self.prefix + f"/collaborators/{quote(task['actor'], safe='')}/permission", task
        )
        if permission["permission"] not in {"admin", "write", "maintain"}:
            raise GitHubError("Current repository write permission is required")
        route = "issues" if task["event"] == "issue_comment" else "pulls"
        comment = self.get(self.prefix + f"/{route}/comments/{task['comment_id']}", task)
        if comment["user"]["id"] != task["actor_id"] or comment["body"] != task["body"]:
            raise GitHubError("Triggering comment changed; submit a new request")

    def file(self, name, sha, task):
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
        pr = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if pr["state"] != "open":
            raise GitHubError("PR is no longer open")
        files = self.pages(self.prefix + f"/pulls/{task['number']}/files", task)
        if len(files) != pr["changed_files"] or len(files) > 60:
            raise GitHubError("PR file list is incomplete or exceeds the 60-file limit")
        context = {
            "repository": self.config.repository,
            "number": task["number"],
            "title": pr["title"],
            "body": pr["body"],
            "head": pr["head"]["sha"],
            "base": pr["base"]["sha"],
            "files": [],
            "guidance": {},
            "checks": {},
        }
        used = 0
        for item in files:
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
        context["checks"] = self.get(
            self.prefix + f"/commits/{context['head']}/check-runs", task, params={"per_page": 100}
        )
        if task["command"] == "explain-ci":
            context["ci"] = self.ci_logs(context["head"], task)
        current = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if (current["head"]["sha"], current["base"]["sha"]) != (context["head"], context["base"]):
            raise GitHubError("PR changed while collecting context; submit a new request")
        return context

    def ci_logs(self, sha, task):
        result = self.get(
            self.prefix + "/actions/runs", task, params={"head_sha": sha, "per_page": 20}
        )
        runs = []
        for run in result["workflow_runs"]:
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
                        {k: j.get(k) for k in ("name", "conclusion", "steps", "html_url")}
                        for j in jobs["jobs"]
                    ],
                }
            )
        # This version intentionally exposes check annotations/steps, not secret-bearing raw logs.
        return {
            "runs": runs,
            "raw_logs_included": False,
            "limitation": "Diagnosis uses check outputs and failing steps; raw logs are not loaded.",
        }

    def publish(self, task, context, body):
        self.authorize(task)
        current = self.get(self.prefix + f"/pulls/{task['number']}", task)
        if (
            current["state"] != "open"
            or current["head"]["sha"] != context["head"]
            or current["base"]["sha"] != context["base"]
        ):
            raise GitHubError("PR changed; stale result was not published")
        result = self.request(
            "POST",
            self.prefix + f"/issues/{task['number']}/comments",
            self.token(task["installation"], write=True),
            json={"body": body},
        )
        return result["html_url"]
