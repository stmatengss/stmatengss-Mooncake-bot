import asyncio
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .agent import analyze, render
from .github import GitHub, GitHubError
from .store import Store

MAX_BODY = 2 * 1024 * 1024


def parse_event(config, event, payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected an object")
    if (
        event not in {"issue_comment", "pull_request_review_comment"}
        or payload.get("action") != "created"
    ):
        return None
    repo, actor, comment = payload["repository"], payload["sender"], payload["comment"]
    if repo["id"] != config.repository_id or repo["full_name"] != config.repository:
        raise ValueError("Repository is not enrolled")
    installation = payload["installation"]["id"]
    if installation not in config.installation_ids:
        raise ValueError("Installation is not enrolled")
    if actor["type"] != "User" or actor["id"] not in config.allowed_actor_ids:
        return None
    if comment["user"]["id"] != actor["id"]:
        raise ValueError("Comment author and sender do not match")
    match = re.match(
        r"^\s*@" + re.escape(config.mention) + r"\s+(review|explain-ci)\b", comment["body"], re.I
    )
    if not match:
        return None
    item = payload.get("issue") or payload.get("pull_request")
    if event == "issue_comment" and "pull_request" not in item:
        return None
    return {
        "source": f"{event}:{comment['id']}",
        "event": event,
        "installation": installation,
        "number": item["number"],
        "actor": actor["login"],
        "actor_id": actor["id"],
        "comment_id": comment["id"],
        "body": comment["body"],
        "command": match[1].lower(),
    }


async def execute(config, store, github, row, analyzer=analyze):
    task = json.loads(row["payload"])
    directory = config.path("data_dir") / "tasks" / row["id"]
    try:
        context = await asyncio.to_thread(github.context, task)
        result = await analyzer(config, task, context, directory)
        body = render(row["id"], context, result, config.backend)
        (directory / "comment.md").write_text(body)
        store.update(row["id"], "running", result=json.dumps(result))
        if config.publish:
            store.update(row["id"], "publishing")
            url = await asyncio.to_thread(github.publish, task, context, body)
            store.update(row["id"], "succeeded", published_url=url)
        else:
            store.update(row["id"], "preview")
    except asyncio.CancelledError:
        store.update(row["id"], "interrupted", error="Service stopped; no automatic replay")
        raise
    except Exception as exc:
        # HTTP errors from third parties may include sensitive data; keep only our safe errors.
        message = str(exc) if isinstance(exc, (GitHubError, ValueError)) else type(exc).__name__
        store.update(row["id"], "failed", error=message[:300])


def create_app(config, github=None, analyzer=analyze, workers=True):
    secret = config.path("webhook_secret_file").read_bytes().strip()
    if len(secret) < 32:
        raise ValueError("A strong webhook secret is required")
    store, github = Store(config.path("data_dir")), github or GitHub(config)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        active = set()

        async def dispatch():
            while True:
                if config.connected and len(active) < config.max_concurrent:
                    row = store.claim(config.max_concurrent)
                    if row:
                        work = asyncio.create_task(execute(config, store, github, row, analyzer))
                        active.add(work)
                        work.add_done_callback(active.discard)
                        continue
                await asyncio.sleep(0.5)

        scheduler = asyncio.create_task(dispatch()) if workers else None
        try:
            yield
        finally:
            if scheduler:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            pending = list(active)
            for work in pending:
                work.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "github_connected": config.connected,
            "publication_enabled": config.publish,
            "analysis_backend": config.backend,
        }

    @app.post("/webhooks/github")
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Payload too large")
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        signature = request.headers.get("x-hub-signature-256", "")
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise HTTPException(401, "Invalid signature")
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"accepted": True}
        try:
            task = parse_event(config, event, json.loads(body))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(403, "Malformed or unauthorized event") from None
        if not task:
            return {"accepted": False}
        row = store.enqueue(task)
        return {"accepted": True, "task_id": row["id"], "state": row["state"]}

    return app
