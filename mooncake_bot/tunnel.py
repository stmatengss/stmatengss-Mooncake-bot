"""Supervised temporary tunnel with signed health probes and webhook reconciliation."""

import fcntl
import hashlib
import hmac
import json
import queue
import re
import signal
import subprocess
import threading
import time

import httpx

from .child import guarded_args
from .config import Config
from .github import GitHub

TUNNEL_URL = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com\b")


def probe(client, origin, secret):
    body = b"{}"
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    response = client.post(
        origin + "/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature},
    )
    response.raise_for_status()
    if response.json().get("accepted") is not True:
        raise ValueError("Signed tunnel health probe was not accepted")


def register(config_path, origin, github):
    if not TUNNEL_URL.fullmatch(origin):
        raise ValueError("Unexpected temporary tunnel origin")
    config = Config.load(config_path)
    expected = origin + "/webhooks/github"
    current = github.request("GET", "/app/hook/config", github.assertion())
    if current["url"] != expected:
        current = github.request(
            "PATCH", "/app/hook/config", github.assertion(), json={"url": expected}
        )
    if current["url"] != expected or str(current["insecure_ssl"]) != "0":
        raise ValueError("GitHub webhook registration did not match the secure endpoint")
    config.webhook_url = expected
    config.save(config_path)
    return expected


def run_tunnel(config_path):
    config = Config.load(config_path)
    data = config.path("data_dir")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (data / "tunnel.lock").open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    secret = config.path("webhook_secret_file").read_bytes().strip()
    github = GitHub(config)
    process = subprocess.Popen(
        guarded_args(
            [
                str(config.path("tunnel_bin")),
                "tunnel",
                "--url",
                f"http://{config.host}:{config.port}",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--metrics",
                "127.0.0.1:28821",
            ]
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stopping = threading.Event()
    urls = queue.Queue()

    def stop(*_):
        stopping.set()
        if process.poll() is None:
            process.terminate()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)

    def read_output():
        for line in process.stdout:
            print(line.rstrip(), flush=True)
            match = TUNNEL_URL.search(line)
            if match:
                urls.put(match.group(0))

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    origin, registered, failures, next_probe = None, False, 0, 0
    started = time.monotonic()
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            while process.poll() is None and not stopping.is_set():
                try:
                    discovered = urls.get(timeout=1)
                    if discovered != origin:
                        origin, registered, failures, next_probe = discovered, False, 0, 0
                except queue.Empty:
                    pass
                if not origin:
                    if time.monotonic() - started > 120:
                        raise RuntimeError("Tunnel did not provide an address within 120 seconds")
                    continue
                if time.monotonic() < next_probe:
                    continue
                try:
                    probe(client, origin, secret)
                    if not registered:
                        url = register(config_path, origin, github)
                        registered = True
                        print(f"GitHub webhook synchronized: {url}", flush=True)
                    failures = 0
                    temporary = data / "tunnel-state.tmp"
                    temporary.write_text(
                        json.dumps(
                            {
                                "url": origin + "/webhooks/github",
                                "cloudflared_pid": process.pid,
                                "checked_at": time.time(),
                                "registered": registered,
                            }
                        )
                        + "\n"
                    )
                    temporary.replace(data / "tunnel-state.json")
                except Exception as exc:
                    failures += 1
                    print(f"Tunnel probe or registration failed: {type(exc).__name__}", flush=True)
                    if failures >= 5:
                        raise RuntimeError("Tunnel unhealthy; supervisor must restart it") from None
                next_probe = time.monotonic() + (60 if registered else 10)
    finally:
        stop()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        process.stdout.close()
        github.client.close()
        lock.close()
