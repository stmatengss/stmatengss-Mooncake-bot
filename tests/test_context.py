import copy

import pytest

from mooncake_bot.agent import added_lines
from mooncake_bot.config import Config
from mooncake_bot.context import (
    COVERAGE_LIMIT,
    MAX_CONTEXT_BYTES,
    check_analysis,
    compact_context,
    size,
)
from mooncake_bot.github import GitHub


def test_small_context_preserves_benchmark_inputs():
    context = {"files": [{"filename": "a.py", "content": "pass\n", "patch": "+pass"}]}
    assert compact_context(context) is context


def test_large_context_preserves_identity_and_validates_only_visible_anchors():
    patch = "@@ -0,0 +1,20000 @@\n" + "+print('日本語测试')\n" * 20000
    context = {
        "head": "a" * 40,
        "base": "b" * 40,
        "files": [{"filename": "scripts/example.py", "content": "pass\n" * 40000, "patch": patch}],
        "guidance": {"AGENTS.md": "Keep behavior unchanged.\n" * 3000},
    }
    original = copy.deepcopy(context)
    view = compact_context(context)
    assert context == original
    assert size(view) <= MAX_CONTEXT_BYTES
    assert view["head"] == context["head"] and view["base"] == context["base"]
    assert view["files"][0]["filename"] == "scripts/example.py"
    assert view["files"][0]["patch_complete"] is False
    assert view["files"][0]["content"] is None
    assert added_lines(view["files"][0]["patch"]) < added_lines(patch)
    result = {
        "summary": "Partial review",
        "limitations": [],
        "findings": [
            {"path": "scripts/example.py", "line": 1, "priority": 2, "body": "Example"},
        ],
    }
    assert COVERAGE_LIMIT in check_analysis(result, context, view)["limitations"]
    result["findings"][0]["line"] = 20000
    with pytest.raises(ValueError, match="added line"):
        check_analysis(result, context, view)


def test_bounded_ci_retains_failure_evidence():
    context = {
        "files": [],
        "ci": {
            "runs": [
                {
                    "name": "Failing CPU check",
                    "conclusion": "failure",
                    "jobs": [
                        {"name": "unit", "steps": [{"name": "pytest", "conclusion": "failure"}]},
                    ],
                }
            ],
            "annotations": [
                {"path": "test/a.py", "start_line": 1, "message": "Error: " + "x" * 150000}
            ],
        },
    }
    view = compact_context(context)
    assert size(view) <= MAX_CONTEXT_BYTES
    assert view["ci"]["runs"][0]["jobs"][0]["steps"][0]["conclusion"] == "failure"
    assert view["ci"]["annotations"][0]["message"].startswith("Error: ")
    assert view["coverage_limits"]


def test_ci_context_does_not_fetch_full_changed_source(tmp_path):
    github = GitHub(Config(root=tmp_path))
    github.authorize = lambda _task: None
    files = [{"filename": "large.cpp"}]
    github.pages = lambda *_args: files
    fetched = []

    def file(name, *_args):
        fetched.append(name)
        return None

    def get(path, *_args, **_kwargs):
        if path.endswith("/pulls/1"):
            return {
                "state": "open",
                "title": "Change",
                "body": "",
                "changed_files": 1,
                "head": {"sha": "head", "ref": "feature", "repo": {"id": 1}},
                "base": {"sha": "base", "ref": "main"},
            }
        if path.endswith("/check-runs"):
            return {"check_runs": []}
        if path.endswith("/actions/runs"):
            return {"workflow_runs": []}
        return []

    github.get, github.file = get, file
    context = github.context({"command": "explain-ci", "number": 1})
    assert "large.cpp" not in fetched
    assert context["files"] == [{"filename": "large.cpp"}]
    assert context["ci"]["raw_logs_included"] is False
