"""Diagnose the upstream Mooncake nightly using evidence from the exact job attempt."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path

import httpx

from .github import GitHubError
from .upstream import UPSTREAM, UPSTREAM_ID

REPO = f"/repos/{UPSTREAM}"
WEB = f"https://github.com/{UPSTREAM}"
WORKFLOW = ".github/workflows/nightly.yml"
BAD = {"failure", "timed_out", "action_required"}
LOG_LIMIT = 64 * 1024 * 1024
RUN_LINK = re.compile(rf"^\*\*Run\*\*:\s*{re.escape(WEB)}/actions/runs/([1-9]\d*)\s*$", re.M)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CTEST = re.compile(r"\bTest\s+#\d+:\s+([\w./:+-]+)\s+\.+\s*\*\*\*(Failed|Timeout|Exception)")


def gh_token(login=None):
    variable = "MOONCAKE_AGENT_TOKEN" if login else "MOONCAKE_NIGHTLY_READ_TOKEN"
    if token := os.environ.get(variable):
        return token
    if not login and (token := os.environ.get("GH_TOKEN")):
        return token
    args = ["gh", "auth", "token", "--hostname", "github.com"]
    env = os.environ.copy()
    if login:
        args += ["--user", login]
        for name in ("GH_TOKEN", "GITHUB_TOKEN"):
            env.pop(name, None)
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if not result or result.returncode or not result.stdout.strip():
        raise ValueError(f"Provide {variable} or authorize the requested GitHub CLI account")
    return result.stdout.strip()


class API:
    def __init__(self, token, transport=None):
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2026-03-10"},
            timeout=30,
            transport=transport,
        )

    def request(self, method, path, **kwargs):
        for attempt in range(3):
            try:
                response = self.client.request(method, path, **kwargs)
                if response.is_success:
                    return response.json()
                if (
                    method != "GET"
                    or response.status_code not in {500, 502, 503, 504}
                    or attempt == 2
                ):
                    raise GitHubError(
                        f"GitHub {method} {path}: HTTP {response.status_code}", response.status_code
                    )
            except httpx.TransportError:
                if method != "GET" or attempt == 2:
                    raise GitHubError(f"GitHub {method} {path}: transport error") from None
            time.sleep(1 + attempt)

    def jobs(self, run):
        path = f"{REPO}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs"
        rows = []
        for page in range(1, 51):
            data = self.request("GET", path, params={"per_page": 100, "page": page})
            rows.extend(data["jobs"])
            if len(rows) >= data["total_count"]:
                return rows
            if not data["jobs"]:
                break
        raise ValueError("GitHub returned an incomplete jobs list")

    def evidence(self, job, steps):
        prefix = f"{REPO}/actions/jobs/{job['id']}"
        signals = []
        for step in steps:
            found, coverage = self.read_log(f"{prefix}/steps/{step['number']}/logs", [step])
            if coverage != "read":
                # Older runs can have a job-level log without a step-level log.
                return self.read_log(prefix + "/logs", steps)
            signals.extend(found)
        return signals, "read"

    def read_log(self, path, steps):
        for attempt in range(3):
            try:
                # Signed log URLs are short-lived. Do not reuse a cached redirect on retries.
                with self.client.stream(
                    "GET",
                    path,
                    follow_redirects=True,
                    params={"t": int(time.time())} if attempt else {},
                ) as response:
                    if response.is_success:
                        return scan_log(response.iter_bytes(), steps)
                    code = response.status_code
                if code not in {403, 500, 502, 503, 504} or attempt == 2:
                    return [], f"logs unavailable (HTTP {code})"
            except httpx.HTTPError:
                if attempt == 2:
                    return [], "log download interrupted"
            time.sleep(1 + attempt)

    def runner_annotation(self, job):
        # A hosted runner can disappear before it uploads a log; the check still
        # has a service-side failure annotation. Only query this repository.
        match = re.fullmatch(
            rf"https://api.github.com{REPO}/check-runs/(\d+)", job.get("check_run_url") or ""
        )
        if match:
            rows = self.request(
                "GET", f"{REPO}/check-runs/{match[1]}/annotations", params={"per_page": 100}
            )
            if any(
                row.get("annotation_level") == "failure"
                and "hosted runner lost communication" in row["message"].lower()
                for row in rows
            ):
                return {
                    "step": None,
                    "time": "GitHub check annotation",
                    "category": "runner",
                    "evidence": "The hosted runner lost communication with GitHub",
                }
        return None


def signature(line, previous):
    """Return a bounded, shareable signature, without echoing raw runner output."""
    if re.search(r"(?:E: Failed to fetch|Err:\d+).*?\b403\s+Forbidden", line, re.I) or (
        re.search(r"^\s*403\s+Forbidden", line, re.I) and "Err:" in previous
    ):
        return "dependency", "APT package index download returned HTTP 403"
    if match := re.search(r"Failed to download metadata for repo '([\w.-]{1,80})'", line):
        status = re.findall(r"Status code:\s*([45]\d\d)", previous)
        suffix = f" (HTTP {status[-1]})" if status else ""
        return "dependency", f"Package metadata for {match[1]} could not be downloaded{suffix}"
    if re.search(
        r"(fatal: unable to access|error: RPC failed).*?(Operation too slow|timed out)", line, re.I
    ):
        return "dependency", "Git dependency fetch timed out"
    if match := CTEST.search(line):
        return "test failure", f"CTest {match[1][:180]}: {match[2]}"
    if "ERROR: LeakSanitizer: detected memory leaks" in line:
        return "sanitizer", "LeakSanitizer reported a memory leak"
    if match := re.search(
        r"not enough writable placement targets: reserved=(\d+) need=(\d+)", line
    ):
        return (
            "test failure",
            f"Insufficient writable placement targets: {match[1]} reserved, {match[2]} required",
        )
    if line.strip() == "job failed or stopped !":
        return "external test", "External test service reported that the job failed or stopped"
    if match := re.search(r"^test ([\w:.\-/]+) \.\.\. FAILED\s*$", line):
        return "test failure", f"Rust test failed: {match[1][:180]}"
    if match := re.search(r"^FAILED ([\w./:\[\]-]+)", line):
        return "test failure", f"pytest failed: {match[1][:180]}"
    if re.search(r"No space left on device|Disk quota exceeded", line, re.I):
        return "runner", "Runner storage exhausted"
    if re.search(
        r"fatal error: Killed signal terminated program|Killed process \d+ \(cc1plus\)", line
    ):
        return "build", "Compiler process was killed"
    if match := re.search(
        r"([\w./+-]+\.(?:cc|cpp|c|cu|h|hpp|rs)):(\d+)(?::\d+)?:\s*(?:fatal )?error:", line
    ):
        return "build", f"Compiler error at {Path(match[1]).name}:{match[2]}"
    return None


def scan_log(chunks, steps):
    windows = [
        (s["number"], s["started_at"][:19], s["completed_at"][:19])
        for s in steps
        if s.get("started_at") and s.get("completed_at")
    ]
    seen, matches = set(), []
    previous = deque(maxlen=12)
    pending, size, in_header = b"", 0, False
    for chunk in chain(chunks, (b"\n",)):
        size += len(chunk)
        if size > LOG_LIMIT:
            return matches, "log exceeded scan limit"
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        # An unusually long unterminated line cannot be a supported diagnostic.
        if len(pending) > 100_000:
            pending = b"oversized" + pending[-2000:]
        for raw in lines:
            text = raw[:24_000].decode("utf-8-sig", "replace").rstrip()
            timestamp, _, line = text.partition(" ")
            if not re.match(r"^\d{4}-\d\d-\d\dT", timestamp):
                continue
            line = ANSI.sub("", line)
            if line.startswith("##[group]Run "):
                in_header = True
            if line.startswith("##[endgroup]"):
                in_header = False
                continue
            step = next(
                (number for number, start, end in windows if start <= timestamp[:19] <= end), None
            )
            if step is None or in_header or line.startswith(("+ ", "[command]", "##[group]")):
                continue
            found = signature(line, "\n".join(previous))
            previous.append(line[:700])
            if found and (step, found) not in seen and len(matches) < 12:
                seen.add((step, found))
                matches.append(
                    {"step": step, "time": timestamp, "category": found[0], "evidence": found[1]}
                )
    return matches, "read" if windows else "missing step timestamps"


def get_run(api, issue_number):
    issue = api.request("GET", f"{REPO}/issues/{issue_number}")
    match = RUN_LINK.search(issue.get("body") or "")
    if (
        not match
        or "pull_request" in issue
        or "nightly-failure" not in {label["name"] for label in issue["labels"]}
    ):
        raise ValueError("Expected an upstream nightly-failure issue with its Actions run link")
    run = api.request("GET", f"{REPO}/actions/runs/{match[1]}")
    if (
        run["repository"]["id"] != UPSTREAM_ID
        or run["id"] != int(match[1])
        or run["path"] != WORKFLOW
        or run["head_branch"] != "main"
        or run["event"] not in {"schedule", "workflow_dispatch"}
    ):
        raise ValueError("Issue does not refer to the upstream main nightly")
    return run


def diagnose(api, run, number):
    report = {
        "issue": number,
        "run": run["id"],
        "attempt": run["run_attempt"],
        "head": run["head_sha"],
        "conclusion": run["conclusion"],
        "status": run["status"],
        "jobs": [],
        "earlier_runs_checked": [],
    }
    if run["status"] != "completed" or run["conclusion"] == "success":
        return report
    failed = [j for j in api.jobs(run) if j["conclusion"] in BAD or j["conclusion"] == "cancelled"]

    def inspect(job):
        steps = [
            s
            for s in job.get("steps", [])
            if s["conclusion"] in BAD or s["conclusion"] == "cancelled"
        ]
        signals, coverage = [], "no failing step recorded"
        if job["name"] == "nightly-gate":
            coverage = "aggregate gate"
        elif steps:
            signals, coverage = api.evidence(job, steps)
        if not signals and coverage != "aggregate gate":
            if annotation := api.runner_annotation(job):
                signals = [annotation]
                coverage = "check annotation"
        return {
            "id": job["id"],
            "name": job["name"],
            "conclusion": job["conclusion"],
            "steps": [{"number": s["number"], "name": s["name"]} for s in steps],
            "log_status": coverage,
            "signals": signals,
            "previous_pass": None,
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        report["jobs"] = list(pool.map(inspect, failed))
    history = api.request(
        "GET",
        f"{REPO}/actions/workflows/nightly.yml/runs",
        params={
            "branch": "main",
            "event": run["event"],
            "status": "completed",
            "created": "<" + run["created_at"],
            "per_page": 4,
        },
    )["workflow_runs"]
    for prior in sorted(history, key=lambda r: r["created_at"], reverse=True):
        if prior["created_at"] >= run["created_at"] or prior["id"] == run["id"]:
            continue
        passes = {j["name"]: j for j in api.jobs(prior) if j["conclusion"] == "success"}
        report["earlier_runs_checked"].append(prior["id"])
        for job in report["jobs"]:
            if job["previous_pass"] is None and (passed := passes.get(job["name"])):
                job["previous_pass"] = {
                    "run": prior["id"],
                    "job": passed["id"],
                    "head": prior["head_sha"],
                }
    return report


def markdown(report):
    url = f"{WEB}/actions/runs/{report['run']}"
    cancelled = sum(job["conclusion"] == "cancelled" for job in report["jobs"])
    parts = [
        f"# Mooncake nightly · issue #{report['issue']}",
        "",
        f"[Issue]({WEB}/issues/{report['issue']}) · [Run, attempt {report['attempt']}]({url}/attempts/{report['attempt']}) · `{report['head']}`",
        "",
        f"Workflow: {report['status']} / {report['conclusion']}. "
        f"Compared {len(report['earlier_runs_checked'])} earlier completed main nightlies of the same trigger.",
        f"Failed jobs: {len(report['jobs']) - cancelled}. Cancelled jobs: {cancelled}.",
    ]
    for job in report["jobs"]:
        parts += [
            "",
            f"## {job['name']}",
            "",
            f"[Job {job['id']}]({url}/job/{job['id']}) · {job['conclusion']}",
        ]
        for step in job["steps"]:
            parts.append(f"- Step {step['number']}: {step['name']}")
        for signal in job["signals"]:
            parts.append(f"- {signal['time']} · {signal['category']} · {signal['evidence']}")
        if not job["signals"]:
            parts.append(
                "- "
                + {
                    "read": "Cause undetermined from recognized log evidence.",
                    "aggregate gate": "Gate reports the upstream job outcomes.",
                }.get(job["log_status"], job["log_status"])
            )
        elif job["log_status"] != "read":
            parts.append(f"- Evidence coverage: {job['log_status']}")
        if passed := job["previous_pass"]:
            parts.append(
                f"- [Earlier passing job]({WEB}/actions/runs/{passed['run']}/job/{passed['job']}) at `{passed['head'][:12]}`"
            )
    return "\n".join(parts) + "\n"


def save(path, data):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
        try:
            out.write(data)
            out.flush()
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)


def report_for_issue(api, number, output, refresh=False):
    run = get_run(api, number)
    folder = output / f"{number}-{run['id']}-{run['run_attempt']}"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / "diagnosis.json"
    if not refresh and record.exists() and (folder / "diagnosis.md").exists():
        prior = json.loads(record.read_text())
        if (
            prior["status"] == run["status"] == "completed"
            and prior["conclusion"] == run["conclusion"]
            and all(
                j["log_status"]
                in {
                    "read",
                    "aggregate gate",
                    "check annotation",
                    "no failing step recorded",
                    "log exceeded scan limit",
                    "missing step timestamps",
                }
                for j in prior["jobs"]
            )
        ):
            return prior, folder
    report = diagnose(api, run, number)
    save(folder / "diagnosis.md", markdown(report))
    save(record, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report, folder


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("diagnose")
    scan.add_argument("--issue", type=int)
    scan.add_argument(
        "--limit",
        type=int,
        choices=range(1, 11),
        default=3,
        help="Number of recent issues to check",
    )
    scan.add_argument("--output-dir", type=Path, default=Path("data/nightly"))
    scan.add_argument("--refresh", action="store_true")
    from .nightly_publish import add_options, publish

    add_options(commands.add_parser("draft"))
    args = parser.parse_args()
    try:
        if args.command == "draft":
            print(publish(args))
            return
        api = API(gh_token())
        try:
            if args.issue:
                numbers = [args.issue]
            else:
                since = datetime.now(timezone.utc) - timedelta(days=7)
                items = api.request(
                    "GET",
                    REPO + "/issues",
                    params={
                        "labels": "nightly-failure",
                        "state": "all",
                        "sort": "created",
                        "direction": "desc",
                        "since": since.isoformat(),
                        "per_page": 10,
                    },
                )
                numbers = [
                    i["number"]
                    for i in items
                    if "pull_request" not in i and RUN_LINK.search(i.get("body") or "")
                ][: args.limit]
            incomplete = []
            for number in numbers:
                report, folder = report_for_issue(api, number, args.output_dir, args.refresh)
                print(folder / "diagnosis.md", flush=True)
                if any(
                    j["log_status"].startswith(("logs unavailable", "log download interrupted"))
                    for j in report["jobs"]
                ):
                    incomplete.append(number)
            if incomplete:
                raise ValueError(f"Partial reports saved; retry log access for issues {incomplete}")
        finally:
            api.client.close()
    except (ValueError, GitHubError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
