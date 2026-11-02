"""Scheduled, non-forcing upstream merges into the enrolled Mooncake fork."""

import json
import re
import uuid
from urllib.parse import quote

from .github import GitHubError

UPSTREAM = "kvcache-ai/Mooncake"
UPSTREAM_ID = 819733173


class UpstreamSync:
    def __init__(self, worker):
        self.worker, self.state = worker, worker.state
        self.settings, self.config = worker.settings, worker.config

    def status(self):
        rows = self.state.recent("upstream_sync")
        row = rows[0] if rows else None
        return {
            "enabled": self.settings.upstream_sync_enabled,
            "repository": UPSTREAM,
            "branch": "main",
            "interval_seconds": self.settings.upstream_sync_interval_seconds,
            "last": {
                "id": row["id"],
                "created": row["created"],
                "state": row["state"],
                "result": json.loads(row["result"]),
            }
            if row
            else None,
            "next_check_after": row["created"] + self.settings.upstream_sync_interval_seconds
            if row
            else None,
        }

    def run_if_due(self):
        if (
            not self.settings.upstream_sync_enabled
            or (self.state.directory / "STOP").exists()
            or not self.state.due("upstream_sync", self.settings.upstream_sync_interval_seconds)
        ):
            return
        run_id = self.state.begin("upstream_sync", uuid.uuid4().hex)
        record = {"upstream_repository": UPSTREAM, "branch": "main"}
        write_started = False
        try:
            if not self.config.publish:
                self.state.finish(run_id, "preview_disabled", record)
                return
            github, task = self.worker.github, self.worker.task
            repo = self.worker.get("")
            parent = repo.get("parent") or {}
            if (
                self.settings.base_ref != "main"
                or repo.get("id") != self.config.repository_id
                or repo.get("full_name", "").lower() != self.config.repository.lower()
                or not repo.get("fork")
                or repo.get("default_branch") != "main"
                or parent.get("id") != UPSTREAM_ID
                or parent.get("full_name", "").lower() != UPSTREAM.lower()
                or parent.get("default_branch") != "main"
            ):
                raise ValueError("Upstream identity or enrolled branch changed")
            before = self.worker.get("/commits/main")["sha"]
            if not re.fullmatch("[0-9a-f]{40}", before):
                raise ValueError("Invalid fork revision")
            compare = self.worker.get(
                "/compare/" + before + "..." + quote(UPSTREAM.split("/")[0], safe="") + ":main"
            )
            ahead, behind = compare["ahead_by"], compare["behind_by"]
            if any(type(value) is not int or value < 0 for value in [ahead, behind]):
                raise ValueError("Invalid upstream comparison")
            record.update(before=before, upstream_commits_ahead=ahead, fork_commits_ahead=behind)
            if not ahead:
                self.state.finish(run_id, "up_to_date", record | {"after": before})
                return
            # Record before the write. A later retry always compares the live branch again;
            # merging upstream is idempotent and never resets or force-updates a ref.
            self.state.finish(run_id, "syncing", record)
            token = github.token(task["installation"], contents_write=True, workflows_write=True)
            write_started = True
            response = github.request(
                "POST", github.prefix + "/merge-upstream", token, json={"branch": "main"}
            )
            after = self.worker.get("/commits/main")["sha"]
            if not re.fullmatch("[0-9a-f]{40}", after):
                raise ValueError("Invalid synced revision")
            ancestry = self.worker.get("/compare/" + before + "..." + after)
            if ancestry["behind_by"] != 0 or ancestry["status"] not in {"ahead", "identical"}:
                raise ValueError("Fork history preservation could not be confirmed")
            self.state.finish(
                run_id,
                "synced",
                record
                | {
                    "after": after,
                    "merge_type": response.get("merge_type"),
                    "previous_head_preserved": True,
                },
            )
        except Exception as exc:
            status = exc.status_code if isinstance(exc, GitHubError) else None
            state = {409: "conflict", 403: "permission_denied", 422: "sync_rejected"}.get(
                status, "unconfirmed" if write_started else "failed"
            )
            self.state.finish(
                run_id, state, record | {"error": type(exc).__name__, "http_status": status}
            )
