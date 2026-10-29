import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from .agent import analyze, check_result, render
from .commands import parse_tasks
from .github import GitHub, GitHubError
from .store import Store

MAX_BODY = 2 * 1024 * 1024
LOG = logging.getLogger(__name__)


def parse_event(config, event, payload):
    tasks = parse_tasks(config, event, payload)
    return tasks[0] if tasks else None


async def execute(config, store, github, row, analyzer=analyze):
    from .features import run
    from .rsi_state import BudgetExhausted

    task = json.loads(row["payload"])
    directory = config.path("data_dir") / "tasks" / row["id"]
    try:
        context = await asyncio.to_thread(github.context, task)
        result = await run(config, store, github, task, context, directory, analyzer)
        if not result.get("builtin"):
            check_result(result, context)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = render(row["id"], context, result, config.backend)
        (directory / "comment.md").write_text(body)
        store.update(row["id"], "running", result=json.dumps(result))
        if config.publish:
            context["_result"] = result
            context["_directory"] = directory
            store.update(row["id"], "publishing")
            url = await asyncio.to_thread(github.publish, task, context, body)
            store.update(row["id"], "succeeded", published_url=url)
        else:
            store.update(row["id"], "preview")
    except BudgetExhausted:
        store.defer(row["id"], (int(time.time()) // 86400 + 1) * 86400)
    except asyncio.CancelledError:
        store.update(row["id"], "interrupted", error="Service stopped; no automatic replay")
        raise
    except Exception as exc:
        # HTTP errors from third parties may include sensitive data; keep only our safe errors.
        message = str(exc) if isinstance(exc, (GitHubError, ValueError)) else type(exc).__name__
        fields = {"error": message[:300]}
        receipt = directory / "publication.json"
        if receipt.exists():
            published = json.loads(receipt.read_text()).get("html_url")
            if published:
                fields["published_url"] = published
        store.update(row["id"], "failed", **fields)


def create_app(config, github=None, analyzer=analyze, workers=True):
    secret = config.path("webhook_secret_file").read_bytes().strip()
    if len(secret) < 32:
        raise ValueError("A strong webhook secret is required")
    store, github = Store(config.path("data_dir")), github or GitHub(config)
    scan_requested = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        active = set()

        async def reconcile():
            from .automation import scan

            while True:
                started = time.monotonic()
                try:
                    await asyncio.to_thread(scan, config, github, store)
                except Exception as exc:
                    LOG.warning("Automation scan failed (%s)", type(exc).__name__)
                try:
                    await asyncio.wait_for(scan_requested.wait(), config.scan_interval_seconds)
                    scan_requested.clear()
                    await asyncio.sleep(max(0, 30 - (time.monotonic() - started)))
                except TimeoutError:
                    pass

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
        scanner = asyncio.create_task(reconcile()) if workers and config.connected else None
        try:
            yield
        finally:
            for background in [scheduler, scanner]:
                if background:
                    background.cancel()
                    await asyncio.gather(background, return_exceptions=True)
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
            tasks = parse_tasks(config, event, json.loads(body))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(403, "Malformed or unauthorized event") from None
        if event == "workflow_run" and config.auto_ci:
            scan_requested.set()
        if not tasks:
            return {"accepted": False}
        rows = [store.enqueue(task) for task in tasks]
        reply = {"accepted": True, "task_id": rows[0]["id"], "state": rows[0]["state"]}
        if len(rows) > 1:
            reply["task_ids"] = [row["id"] for row in rows]
        return reply

    return app
