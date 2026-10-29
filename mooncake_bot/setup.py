"""Loopback-only GitHub manifest registration; no OAuth client credentials are retained."""

import html
import json
import re
import secrets
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config

PERMISSIONS = {
    "contents": "read",
    "metadata": "read",
    "issues": "write",
    "pull_requests": "write",
    "checks": "read",
    "statuses": "read",
    "actions": "read",
}


def manifest(config, origin):
    return {
        "name": "Mooncake Bot",
        "url": f"https://github.com/{config.repository}",
        "description": "Maintainer-triggered Mooncake PR analysis. AI-assisted comments; human review required.",
        "hook_attributes": {"url": config.webhook_url, "active": False},
        "redirect_url": origin + "/callback",
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": ["issue_comment", "pull_request_review_comment"],
        "request_oauth_on_install": False,
    }


def page(title, content):
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font:17px system-ui;max-width:780px;margin:70px auto;padding:0 24px;"
        "line-height:1.7;background:#f6f8fa;color:#17202a}main{background:white;padding:32px;"
        "border:1px solid #d0d7de;border-radius:14px}h1{font-size:28px}button{padding:12px 18px;"
        "background:#126b39;color:white;border:0;border-radius:7px;font:inherit;cursor:pointer}"
        "code{background:#f0f2f5;padding:3px 6px;border-radius:4px}a{color:#0969da}</style>"
        f"<main><h1>{html.escape(title)}</h1>{content}</main></html>",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action https://github.com; frame-ancestors 'none'",
        },
    )


def create_setup(config_path, port=8822):
    config = Config.load(config_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    state, started, used = secrets.token_urlsafe(32), time.monotonic(), False
    origin = f"http://127.0.0.1:{port}"
    app.state.registration_state = state

    @app.middleware("http")
    async def loopback(request: Request, call_next):
        if request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(403, "Local access only")
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        owner = config.repository.split("/")[0]
        if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
            raise HTTPException(400, "Invalid organization")
        registration_path = (
            "/settings/apps/new"
            if config.registration_owner_type == "User"
            else f"/organizations/{owner}/settings/apps/new"
        )
        encoded = html.escape(json.dumps(manifest(config, origin)), quote=True)
        return page(
            "Mooncake bot · GitHub 接入",
            f"""
            <p>本地服务和配置已准备。此步骤将在 <b>{owner}</b> 账号中注册 GitHub App（{config.registration_owner_type}）。</p>
            <p><b>范围：</b>安装时只选择 <code>{html.escape(config.repository)}</code>。</p>
            <ul><li>读取代码、PR、检查和 CI 状态。</li>
            <li>写入 Issue/PR 评论及 review；首版仅发布评论。</li>
            <li>不申请代码推送、Actions 重跑、组织成员管理或管理员权限。</li></ul>
            <p>服务只接受当前维护者的命令，公开 webhook 尚未启用。模型使用本机已登录的 Codex。</p>
            <p>GitHub 二次验证由账号本人完成。创建完成后，私钥会自动保存到本机受限文件，页面不显示密钥。</p>
            <form method='post' action='https://github.com{registration_path}?state={state}'>
            <input type='hidden' name='manifest' value='{encoded}'>
            <button type='submit'>前往 GitHub 核对并创建 App</button></form>
            <p>注册后还需完成仓库安装与 webhook 连通性验证。</p>""",
        )

    @app.get("/callback", response_class=HTMLResponse)
    async def callback(code: str, state: str):
        nonlocal used
        if (
            used
            or time.monotonic() - started > 3600
            or not secrets.compare_digest(state, app.state.registration_state)
        ):
            raise HTTPException(403, "Expired or invalid registration state")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", code):
            raise HTTPException(400, "Invalid registration code")
        if config.app_id or config.path("app_key_file").exists():
            raise HTTPException(409, "App credentials already exist")
        used = True  # A failed/ambiguous conversion must not be replayed automatically.
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.github.com/app-manifests/{code}/conversions",
                headers={"Accept": "application/vnd.github+json"},
            )
        if response.status_code != 201:
            raise HTTPException(
                502, "GitHub registration conversion failed; inspect registration before retrying"
            )
        result = response.json()
        if not result.get("pem") or not result.get("webhook_secret") or not result.get("id"):
            raise HTTPException(502, "GitHub returned incomplete App credentials")
        slug = result.get("slug", "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
            raise HTTPException(502, "GitHub returned an invalid App slug")
        for key, text in [
            ("app_key_file", result["pem"]),
            ("webhook_secret_file", result["webhook_secret"]),
        ]:
            destination = config.path(key)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text(text)
            destination.chmod(0o600)
        config.app_id = str(result["id"])
        config.mention = slug
        config.save(config_path)
        return page(
            "App 已创建，密钥已保存",
            f"<p>接下来安装到 Mooncake 仓库。</p>"
            f"<p><a href='https://github.com/apps/{slug}/installations/new'>打开 GitHub 安装页面</a></p>"
            "<p>安装时请选择 Only select repositories，并只选 Mooncake。</p>"
            f"<p>安装后命令前缀为 <code>@{html.escape(slug)}</code>。</p>"
            "<p>本机配置仍为预览模式，尚未自动发评论。</p>",
        )

    return app
