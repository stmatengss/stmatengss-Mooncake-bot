import asyncio
import json

import pytest

from mooncake_bot.agent import analyze, render
from mooncake_bot.config import Config
from mooncake_bot.kimi import MAX_PROMPT_BYTES, parse_stream

RESULT = {"summary": "No established finding", "findings": [], "limitations": ["Tests not run"]}


def stream(result=RESULT):
    return "\n".join(
        json.dumps(record)
        for record in [
            {"role": "meta", "type": "system.version", "version": "2.2.0"},
            {"role": "assistant", "content": json.dumps(result)},
            {"role": "meta", "type": "session.resume_hint", "session_id": "example"},
        ]
    )


def test_kimi_stream_matches_real_cli_protocol():
    assert parse_stream(stream()) == RESULT


@pytest.mark.parametrize(
    "bad_record",
    [
        {"role": "assistant", "tool_calls": [{"name": "Bash"}], "content": ""},
        {"role": "tool", "content": "unexpected execution"},
        {"role": "meta", "type": "error", "error": "upstream failure"},
        {"role": "assistant", "content": "partial output"},
        {"role": "unknown", "content": "unsupported protocol"},
    ],
)
def test_kimi_rejects_tools_errors_and_multiple_replies(bad_record):
    with pytest.raises(ValueError):
        parse_stream(json.dumps(bad_record) + "\n" + stream())


def test_kimi_rejects_oversize_input_before_starting(tmp_path):
    config = Config(root=tmp_path, backend="kimi")
    with pytest.raises(ValueError, match="100 KB"):
        asyncio.run(
            analyze(
                config,
                {"command": "review", "body": "x" * MAX_PROMPT_BYTES},
                {},
                tmp_path / "task",
            )
        )
    assert not (tmp_path / "task").exists()


def test_kimi_backend_validates_result_and_excludes_github_environment(tmp_path, monkeypatch):
    config = Config(root=tmp_path, backend="kimi", kimi_bin="test-kimi")
    config.path("kimi_home").mkdir(parents=True)
    (config.path("kimi_home") / "config.toml").write_text('default_model = "test"\n')
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-agent")

    async def spawn(*args, **kwargs):
        assert "test-kimi" in args
        assert "GITHUB_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["KIMI_CODE_HOME"] == str(config.path("kimi_home"))
        profile = kwargs["cwd"] / "reviewer.md"
        assert "tools: []\nsubagents: []\n" in profile.read_text()
        kwargs["stdout"].write(stream().encode())

        class Process:
            returncode = 0

            async def wait(self):
                return self.returncode

        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(
        analyze(
            config,
            {"command": "review", "body": "review"},
            {"files": []},
            tmp_path / "task",
        )
    )
    assert result == RESULT
    assert json.loads((tmp_path / "task/result.json").read_text()) == RESULT
    assert "by Kimi Code" in render("task", {"head": "a" * 40}, result, "kimi")
    assert "by Codex" in render("task", {"head": "a" * 40}, result, "codex")


def test_unknown_backend_is_rejected(tmp_path):
    config = Config(root=tmp_path, backend="typo")
    config.save(tmp_path / "config.toml")
    with pytest.raises(ValueError, match="backend"):
        Config.load(tmp_path / "config.toml")
