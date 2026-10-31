"""Five-minute reconciliation recovers missed PR/issue/workflow webhooks."""

from .commands import automatic_task, pr_tasks


def scan(config, github, store):
    if not config.connected or not any(
        [
            config.auto_review,
            config.auto_summary,
            config.auto_title,
            config.auto_triage,
            config.auto_ci,
        ]
    ):
        return []
    task = {"installation": config.installation_ids[0]}
    queued = []
    pulls = github.get(
        github.prefix + "/pulls",
        task,
        params={"state": "open", "per_page": 20, "sort": "updated", "direction": "desc"},
    )
    for pr in pulls:
        for request in pr_tasks(config, pr):
            queued.append(store.enqueue(request)["id"])
        if not config.auto_ci or pr.get("draft"):
            continue
        authors = config.automation_author_ids or config.allowed_actor_ids
        if pr["user"]["id"] not in authors:
            continue
        runs = github.get(
            github.prefix + "/actions/runs",
            task,
            params={"head_sha": pr["head"]["sha"], "per_page": 20},
        )["workflow_runs"]
        for run in runs:
            if (
                run["head_sha"] == pr["head"]["sha"]
                and run.get("event") == "pull_request"
                and run.get("status") == "completed"
                and run.get("conclusion") in {"failure", "timed_out"}
                and run.get("head_repository", {}).get("id") == config.repository_id
            ):
                request = automatic_task(config, pr, "auto-ci", run=run)
                if request:
                    queued.append(store.enqueue(request)["id"])
    if config.auto_triage:
        issues = github.get(
            github.prefix + "/issues",
            task,
            params={
                "state": "open",
                "per_page": 20,
                "sort": "updated",
                "direction": "desc",
            },
        )
        for issue in issues:
            if "pull_request" not in issue:
                request = automatic_task(config, issue, "auto-triage", "issue")
                if request:
                    queued.append(store.enqueue(request)["id"])
    return queued
