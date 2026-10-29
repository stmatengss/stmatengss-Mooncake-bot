"""Tool-free Kimi Code CLI adapter. Provider configuration is deployment-owned."""

import asyncio
import json
import os
import signal

from jsonschema import validate

from .agent import POLICY, SCHEMA, command_policy
from .child import guarded_args
from .context import check_analysis, compact_context

# Linux limits each argv entry to 128 KiB. Reject large requests, never truncate them.
MAX_PROMPT_BYTES = 100_000
MAX_STREAM_BYTES = 1_000_000


def parse_stream(text):
    if len(text.encode()) > MAX_STREAM_BYTES:
        raise ValueError("Kimi output exceeds the stream limit")
    replies = []
    for line in text.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("Kimi emitted an invalid stream record")
        if message.get("tool_calls") or message.get("role") == "tool":
            raise ValueError("Tool activity is forbidden for bot analysis")
        if message.get("error") or message.get("type") == "error":
            raise ValueError("Kimi reported an execution error")
        if message.get("role") == "meta":
            continue
        if message.get("role") != "assistant" or not isinstance(message.get("content"), str):
            raise ValueError("Kimi emitted an unsupported stream record")
        replies.append(message["content"].strip())
    if len(replies) != 1:
        raise ValueError("Expected one complete Kimi analysis response")
    body = replies[0]
    if body.startswith("```json\n") and body.endswith("\n```"):
        body = body[8:-4]
    return json.loads(body)


def active_instruction(config):
    path = config.path("policy_file")
    if not path.exists():
        return ""
    value = json.loads(path.read_text())["instruction"]
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("Invalid active review instruction")
    return value


async def analyze_kimi(config, task, context, directory, instruction=None):
    view = compact_context(context)
    data = {"command": task["command"], "request": task["body"], "context": view}
    extension = active_instruction(config) if instruction is None else instruction
    policy = POLICY + "\n" + command_policy(task["command"]) + "\n" + extension
    result = await invoke(config, data, SCHEMA, policy, directory)
    return check_analysis(result, context, view)


async def invoke(config, data, schema, policy, directory):
    """Invoke an explicit tool-free agent; never execute proposed code or commands."""
    prompt = json.dumps(data, ensure_ascii=False)
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise ValueError("Kimi CLI request exceeds 100 KB; submit a smaller PR")
    if not (config.path("kimi_home") / "config.toml").is_file():
        raise ValueError("An isolated Kimi configuration is required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "context.json").write_text(
        json.dumps(data.get("context", data), ensure_ascii=False)
    )
    (directory / "prompt.json").write_text(prompt)
    (directory / "schema.json").write_text(json.dumps(schema))
    profile = directory / "reviewer.md"
    profile.write_text(
        "---\nname: mooncake-reviewer\n"
        "description: Static analysis of supplied PR context without tools\n"
        "tools: []\nsubagents: []\n---\n"
        + policy
        + "\nReturn exactly one JSON object, without Markdown fences, matching this schema:\n"
        + json.dumps(schema)
    )
    skills = directory / "empty-skills"
    skills.mkdir(mode=0o700)
    args = [
        config.kimi_bin,
        "--agent-file",
        str(profile),
        "--skills-dir",
        str(skills),
        "--output-format",
        "stream-json",
        "--prompt",
        prompt,
    ]
    if config.model:
        args += ["--model", config.model]
    env = {k: os.environ[k] for k in ["HOME", "PATH", "LANG", "TMPDIR"] if k in os.environ}
    env["KIMI_CODE_HOME"] = str(config.path("kimi_home"))
    output = directory / "agent.jsonl"
    with output.open("wb") as out, (directory / "agent.log").open("wb") as err:
        process = await asyncio.create_subprocess_exec(
            *guarded_args(args),
            cwd=directory,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(process.wait(), config.timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise
        finally:
            (directory / "invocation.json").write_text(
                json.dumps({"backend": "kimi", "exit_code": process.returncode})
            )
    if process.returncode:
        raise ValueError("Kimi did not complete; inspect its private local log")
    if output.stat().st_size > MAX_STREAM_BYTES:
        raise ValueError("Kimi output exceeds the stream limit")
    result = parse_stream(output.read_text())
    validate(result, schema)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False))
    return result
