import asyncio
import json
from pathlib import Path

import pytest

from mooncake_bot import rsi
from mooncake_bot.config import Config
from mooncake_bot.rsi import Settings, Worker, apply_change, score, should_promote
from mooncake_bot.rsi_state import BudgetExhausted, State


def test_budget_is_persistent_and_failed_calls_cannot_bypass_it(tmp_path):
    state = State(tmp_path, now=lambda: 1793275200)
    state.reserve(2)
    state.reserve(2)
    restarted = State(tmp_path, now=lambda: 1793275200)
    with pytest.raises(BudgetExhausted):
        restarted.reserve(2)
    tomorrow = State(tmp_path, now=lambda: 1793275200 + 86400)
    tomorrow.reserve(2)
    assert tomorrow.used() == 1 and restarted.used() == 2


def test_recovery_preserves_deduplication_and_cooldown(tmp_path):
    state = State(tmp_path, now=lambda: 5000)
    run_id = state.begin("project", "source-hash")
    state.recover()
    assert state.recent()[0]["state"] == "interrupted"
    assert state.seen("project", "source-hash")
    assert not state.due("project", 3600)
    with pytest.raises(Exception):
        state.begin("project", "source-hash")
    state.finish(run_id, "published", {"url": "https://example.invalid/1"})
    assert state.recent()[0]["state"] == "published"


@pytest.mark.parametrize(
    "target", ["../secrets.py", ".github/workflows/run.py", "scripts/../secret.py"]
)
def test_settings_reject_out_of_scope_paths(tmp_path, target):
    path = tmp_path / "rsi.json"
    settings = Settings(targets=[target])
    path.write_text(json.dumps(settings.__dict__))
    config = Config(
        root=tmp_path, backend="kimi", repository=settings.repository, installation_ids=[1]
    )
    with pytest.raises(ValueError):
        Settings.load(path, config)


def test_settings_require_matching_repository_and_bounded_limits(tmp_path):
    path = tmp_path / "rsi.json"
    config = Config(
        root=tmp_path, backend="kimi", repository="stmatengss/Mooncake", installation_ids=[1]
    )
    for settings in [
        Settings(repository="other/repo"),
        Settings(max_model_calls_per_day=100),
        Settings(project_interval_seconds=1),
        Settings(max_open_proposals=100),
    ]:
        path.write_text(json.dumps(settings.__dict__))
        with pytest.raises(ValueError):
            Settings.load(path, config)


def test_patch_must_match_one_exact_selected_source_and_be_valid_python():
    original = "def last(values):\n    return values[len(values)]\n"
    change = {"path": "scripts/a.py", "before": "values[len(values)]", "after": "values[-1]"}
    updated, patch = apply_change("scripts/a.py", original, change)
    assert "values[-1]" in updated and "+    return values[-1]" in patch
    for bad in [
        change | {"path": "secrets/a.py"},
        change | {"before": ""},
        change | {"before": "absent"},
        change | {"after": "("},
    ]:
        with pytest.raises((ValueError, SyntaxError)):
            apply_change("scripts/a.py", original, bad)


def test_holdout_gate_requires_real_gain_and_no_false_positive_regression():
    baseline = {"dev": {"tp": 1, "fp": 0}, "holdout": {"tp": 1, "fp": 0}}
    assert not should_promote(baseline, baseline)
    better = {"dev": {"tp": 1, "fp": 0}, "holdout": {"tp": 2, "fp": 0}}
    assert should_promote(baseline, better)
    assert not should_promote(baseline, better | {"holdout": {"tp": 2, "fp": 1}})
    assert not should_promote(baseline, better | {"dev": {"tp": 0, "fp": 0}})


def test_score_treats_alternative_anchors_as_one_defect_and_counts_clean_false_positives():
    cases = [
        {"split": "holdout", "expected_lines": [2, 3]},
        {"split": "holdout", "expected_lines": []},
    ]
    results = [
        {"findings": [{"path": "fixture.py", "line": 3}]},
        {"findings": [{"path": "fixture.py", "line": 2}]},
    ]
    assert score(cases, results, "holdout") == {"tp": 1, "fp": 1, "fn": 0}


def test_stop_marker_prevents_model_and_github_activity(tmp_path):
    config = Config(root=tmp_path, backend="kimi", installation_ids=[1])
    worker = Worker(config, Settings())
    (worker.state.directory / "STOP").touch()

    async def forbidden(*args):
        raise AssertionError("Paused worker must remain idle")

    worker.project = worker.improve_policy = forbidden
    asyncio.run(worker.step())
    assert worker.state.used() == 0


def test_policy_optimizer_never_receives_holdout_labels(tmp_path):
    config = Config(root=tmp_path, backend="kimi", installation_ids=[1])
    worker = Worker(config, Settings())
    calls = []

    async def review(context, directory, instruction):
        directory.mkdir(parents=True)
        return {"summary": "None", "findings": [], "limitations": []}

    async def ask(data, schema, policy, directory):
        calls.append(data)
        return {"instruction": "Check boundary conditions and avoid unsupported claims."}

    worker.review, worker.ask = review, ask
    asyncio.run(worker.improve_policy())
    assert len(calls) == 1
    cases = json.loads(Path(rsi.__file__).with_name("rsi_cases.json").read_text())
    holdout_sources = {case["source"] for case in cases if case["split"] == "holdout"}
    assert all(item["source"] not in holdout_sources for item in calls[0]["development_cases"])
    assert worker.state.recent()[0]["state"] == "rejected_no_gain"
    assert not config.path("policy_file").exists()


def test_publisher_refuses_unknown_paths_before_requesting_write_token(tmp_path):
    config = Config(root=tmp_path, backend="kimi", installation_ids=[1])
    worker = Worker(config, Settings())
    with pytest.raises(ValueError):
        worker.publish("base", ".github/workflows/x.yml", "text", {}, "a" * 32, tmp_path)
