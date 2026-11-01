import asyncio
import base64
import copy
import hashlib
import json

import pytest

from mooncake_bot.config import Config
from mooncake_bot.core import CORE_PROPOSAL, CoreScanner
from mooncake_bot.core_scope import MAX_SOURCE_BYTES, ROOTS, allowed, windows
from mooncake_bot.rsi import Settings, Worker, apply_change
from mooncake_bot.rsi_state import BudgetExhausted, State

PATH = "mooncake-store/src/allocator.cpp"
SOURCE = "// Contract: return zero for a zero divisor.\nint ratio(int n, int d) {\n    return n / d;\n}\n"
PROPOSAL = {
    "summary": "Handle a zero divisor",
    "trigger": "ratio(1, 0)",
    "impact": "Division by zero",
    "test_plan": "Check zero and nonzero divisors in a focused CPU test.",
    "change": {"path": PATH, "before": "return n / d;", "after": "return d ? n / d : 0;"},
}
REVIEW = {
    "summary": "The explicit contract is violated and the bounded guard fixes it.",
    "findings": [],
    "limitations": ["No native compilation or tests ran."],
    "bug_confirmed": True,
    "fix_correct": True,
    "sufficient_context": True,
}


def identity(source):
    raw = source.encode() if isinstance(source, str) else source
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


class API:
    prefix = "/repos/stmatengss/Mooncake"

    def __init__(self, config):
        self.config = config
        self.head = "a" * 40
        self.sources = {PATH: SOURCE}
        self.pulls = []
        self.files = {}
        self.guidance = {"AGENTS.md": "Require a real correctness defect; no busywork."}
        self.writes = []
        self.truncated = False
        self.fail_publication = False

    def entries(self):
        return [
            {
                "path": path,
                "sha": identity(source),
                "type": "blob",
                "mode": "100755",
                "size": len(source.encode() if isinstance(source, str) else source),
            }
            for path, source in self.sources.items()
        ]

    def get(self, path, _task, **kwargs):
        if path.endswith("/commits/main"):
            return {"sha": self.head}
        if "/git/commits/" in path:
            return {"tree": {"sha": "root"}}
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[-1]
            raw = next(value for value in self.sources.values() if identity(value) == sha)
            raw = raw.encode() if isinstance(raw, str) else raw
            return {
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        if "/git/trees/" in path:
            if kwargs.get("params", {}).get("recursive"):
                return {"truncated": self.truncated, "tree": self.entries()}
            leaf = path.rsplit("/", 1)[-1]
            if leaf == "root":
                return {"tree": [{"path": "mooncake-store", "type": "tree", "sha": "module"}]}
            if leaf == "module":
                return {"tree": [{"path": "src", "type": "tree", "sha": "src"}]}
            return {"tree": [{"path": "allocator.cpp", "type": "blob", "mode": "100755"}]}
        raise AssertionError(path)

    def pages(self, path, _task):
        if path.endswith("/pulls"):
            return self.pulls
        if path.endswith("/issues"):
            return []
        return self.files[int(path.split("/")[-2])]

    def file(self, path, _base, _task):
        return self.guidance.get(path)

    def token(self, installation, **permissions):
        assert installation == 1 and permissions == {"write": True, "contents_write": True}
        return "opaque-test-token"

    def request(self, method, path, _token, **kwargs):
        assert method == "POST"
        self.writes.append((path, kwargs["json"]))
        if self.fail_publication:
            raise RuntimeError("Unknown remote outcome")
        if path.endswith("/pulls"):
            self.pulls.append({"number": 99, "head": {"ref": kwargs["json"]["head"]}})
            self.files[99] = [{"filename": PATH}]
            return {"number": 99, "html_url": "https://example.test/99"}
        return {"sha": "b" * 40}


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    config = Config(
        root=tmp_path,
        repository="stmatengss/Mooncake",
        backend="kimi",
        installation_ids=[1],
        max_model_calls_per_day=48,
    )
    settings = Settings(core_scan_enabled=True, max_model_calls_per_day=48)
    clock = [10000.0]
    worker = Worker(
        config, settings, github=API(config), state=State(tmp_path / "state", now=lambda: clock[0])
    )
    result = CoreScanner(worker)
    calls = []

    async def invoke(_config, data, schema, _policy, directory):
        calls.append(data)
        value = copy.deepcopy(PROPOSAL if schema is CORE_PROPOSAL else REVIEW)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps(value))
        return value

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    result.calls, result.clock = calls, clock
    return result


@pytest.mark.parametrize(
    "path",
    [
        "mooncake-store/src/../../secrets.py",
        "/mooncake-store/src/a.cpp",
        ".github/workflows/a.py",
        "docs/a.py",
        "scripts/a.py",
        "mooncake-store/src/tests/a.cpp",
        "mooncake-store/src/test_helper.cpp",
        "mooncake-store/include/vendor/a.h",
        "mooncake-store/src/cachelib_memory_allocator/a.cpp",
        "mooncake-transfer-engine/tent/include/thirdparty/a.h",
    ],
)
def test_core_scope_rejects_unenrolled_or_generated_files(path):
    assert not allowed(path)


def test_large_sources_are_scanned_in_overlapping_exact_windows():
    source = "".join(f"int value_{i} = {i};\n" for i in range(1500))
    chunks = windows(source)
    assert len(chunks) > 1
    assert all(len(row["source"].encode()) <= 12000 for row in chunks)
    assert all(source.count(row["source"]) == 1 for row in chunks)
    assert chunks[1]["start_line"] <= chunks[0]["end_line"]
    assert chunks[-1]["end_line"] == 1500


def test_settings_enroll_core_without_loosening_legacy_targets(tmp_path):
    config = Config(
        root=tmp_path, repository="stmatengss/Mooncake", backend="kimi", installation_ids=[1]
    )
    settings = Settings(core_scan_enabled=True)
    path = tmp_path / "rsi.json"
    path.write_text(json.dumps(settings.__dict__))
    assert Settings.load(path, config).core_scan_roots == list(ROOTS)
    for changes in [
        {"core_scan_roots": [".github/"]},
        {"core_model_calls_per_day": 0},
        {"core_scan_interval_seconds": 10},
        {"core_scan_enabled": "true"},
        {"targets": [PATH]},
    ]:
        path.write_text(json.dumps(settings.__dict__ | changes))
        with pytest.raises(ValueError):
            Settings.load(path, config)


def test_core_budget_is_atomic_with_the_shared_budget_and_persists(tmp_path):
    state = State(tmp_path, now=lambda: 10000)
    state.reserve(3, "core", 2)
    state.reserve(3, "core", 2)
    with pytest.raises(BudgetExhausted):
        State(tmp_path, now=lambda: 10000).reserve(3, "core", 2)
    assert state.used() == state.bucket_used("core") == 2
    state.reserve(3)
    with pytest.raises(BudgetExhausted):
        state.reserve(3, "other", 2)
    assert state.bucket_used("other") == 0 and state.used() == 3


def test_verified_core_candidate_resumes_as_a_draft_without_more_model_calls(scanner):
    asyncio.run(scanner.scan())
    assert scanner.state.used() == scanner.state.bucket_used("core") == 2
    assert scanner.state.recent()[0]["state"] == "pending"
    assert not scanner.github.writes
    scanner.settings.publish_projects = True
    scanner.publish_pending()
    assert len(scanner.calls) == 2
    assert scanner.state.recent()[0]["state"] == "published"
    tree, commit, branch, pr = [value for _, value in scanner.github.writes]
    assert tree["tree"][0]["mode"] == "100755"
    assert commit["parents"] == ["a" * 40]
    assert branch["ref"].startswith("refs/heads/rsi/core-")
    assert pr["draft"] is True and pr["base"] == "main"
    assert "not compilation" in pr["body"] and "No repository code" in pr["body"]
    assert scanner.state.core_progress()[PATH]["reviewed"] == 1


def test_rejected_original_bug_or_fix_never_requests_a_write_token(scanner, monkeypatch):
    async def invoke(_c, _data, schema, _policy, _directory):
        return copy.deepcopy(
            PROPOSAL if schema is CORE_PROPOSAL else REVIEW | {"bug_confirmed": False}
        )

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    scanner.settings.publish_projects = True
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "rejected_review"
    assert not scanner.github.writes


@pytest.mark.parametrize("renamed", [False, True])
def test_existing_open_pr_files_are_excluded_before_model_invocation(scanner, renamed):
    scanner.github.pulls = [{"number": 5, "head": {"ref": "feature"}}]
    scanner.github.files[5] = [
        {"filename": "mooncake-store/src/renamed.cpp", "previous_filename": PATH}
        if renamed
        else {"filename": PATH}
    ]
    asyncio.run(scanner.scan())
    assert not scanner.calls and not scanner.github.writes


def test_open_proposal_cap_defers_then_publishes_without_regeneration(scanner):
    scanner.settings.publish_projects = True
    scanner.settings.max_open_proposals = 1
    scanner.github.pulls = [{"number": 5, "head": {"ref": "rsi/project-existing"}}]
    scanner.github.files[5] = [{"filename": "scripts/a.py"}]
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "pending"
    asyncio.run(scanner.scan())
    assert len(scanner.calls) == 2 and not scanner.github.writes
    scanner.github.pulls = []
    scanner.publish_pending()
    assert len(scanner.calls) == 2 and scanner.state.recent()[0]["state"] == "published"


def test_ambiguous_publication_is_never_replayed_after_restart(scanner):
    scanner.settings.publish_projects = True
    scanner.github.fail_publication = True
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "publication_ambiguous"
    assert len(scanner.github.writes) == 1
    scanner.state.recover()
    scanner.publish_pending()
    asyncio.run(scanner.scan())
    assert len(scanner.github.writes) == 1 and len(scanner.calls) == 2


def test_same_patch_is_not_proposed_again_after_a_closed_draft(scanner):
    scanner.settings.publish_projects = True
    asyncio.run(scanner.scan())
    scanner.github.pulls = []
    scanner.state.core_retry(PATH, 0, 0)
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "duplicate"
    assert len(scanner.github.writes) == 4 and len(scanner.calls) == 3


@pytest.mark.parametrize("change", ["source", "guidance", "other_source"])
def test_pending_publication_rechecks_every_analyzed_input(scanner, change):
    if change == "other_source":
        scanner.github.sources["mooncake-common/src/helpers.cpp"] = "int helper() { return 1; }\n"
    asyncio.run(scanner.scan())
    if change == "source":
        scanner.github.sources[PATH] += "// changed\n"
    elif change == "guidance":
        scanner.github.guidance["AGENTS.md"] = "New maintainer rules"
    else:
        scanner.github.sources["mooncake-common/src/helpers.cpp"] += "// changed\n"
    scanner.settings.publish_projects = True
    scanner.publish_pending()
    assert scanner.state.recent()[0]["state"] == "stale_input"
    assert not scanner.github.writes


def test_unchanged_inputs_can_be_rebased_onto_a_new_default_branch_head(scanner):
    asyncio.run(scanner.scan())
    scanner.github.head = "d" * 40
    scanner.settings.publish_projects = True
    scanner.publish_pending()
    assert scanner.state.recent()[0]["state"] == "published"
    assert scanner.github.writes[1][1]["parents"] == ["d" * 40]
    assert len(scanner.calls) == 2


def test_unreadable_source_does_not_stall_a_module_forever(scanner):
    scanner.github.sources[PATH] = b"\xff\xfe"
    asyncio.run(scanner.scan())
    progress = scanner.state.core_progress()[PATH]
    assert progress["reviewed"] == 0 and progress["revisit_after"] > scanner.clock[0]
    assert not scanner.calls


def test_incomplete_tree_fails_closed_before_generation(scanner):
    scanner.github.truncated = True
    with pytest.raises(Exception, match="complete Git tree"):
        asyncio.run(scanner.scan())
    assert not scanner.calls


def test_core_scan_rotates_windows_and_resumes_durably(scanner, monkeypatch):
    scanner.github.sources[PATH] = "".join(f"int value_{i} = {i};\n" for i in range(900))
    seen = []

    async def invoke(_c, data, _schema, _policy, _directory):
        seen.append(data["sources"][0]["window"])
        return PROPOSAL | {"change": None}

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)

    async def idle():
        return None

    scanner.worker.project = scanner.worker.improve_policy = idle
    asyncio.run(scanner.worker.step())
    asyncio.run(scanner.worker.step())
    assert seen == [0]
    scanner.clock[0] += scanner.settings.core_scan_interval_seconds
    scanner.worker.state = State(scanner.state.directory, now=lambda: scanner.clock[0])
    asyncio.run(scanner.worker.step())
    assert seen == [0, 1]


def test_core_scan_revisits_unchanged_sources_after_the_configured_cooldown(scanner, monkeypatch):
    async def invoke(*_args):
        return PROPOSAL | {"change": None}

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    asyncio.run(scanner.scan())
    assert scanner.state.used() == 1
    asyncio.run(scanner.scan())
    assert scanner.state.used() == 1
    scanner.clock[0] += scanner.settings.core_revisit_seconds
    asyncio.run(scanner.scan())
    assert len(scanner.state.recent("core")) == 2


def test_native_core_patch_supports_large_files_without_relaxing_manual_defaults():
    original = "// context\n" * 10000 + SOURCE
    with pytest.raises(ValueError, match="oversized"):
        apply_change(PATH, original, PROPOSAL["change"])
    updated, patch = apply_change(
        PATH, original, PROPOSAL["change"], max_bytes=MAX_SOURCE_BYTES + 10000
    )
    assert "return d ?" in updated and patch.count("+") < 20


def test_policy_history_remains_visible_after_more_than_fifty_core_runs(scanner):
    run = scanner.state.begin("policy", "benchmark")
    scanner.state.finish(run, "benchmark_saturated", {})
    for number in range(55):
        scanner.clock[0] += 1
        row = scanner.state.begin("core", str(number))
        scanner.state.finish(row, "no_change", {})
    assert not any(r["kind"] == "policy" for r in scanner.state.recent())
    assert scanner.state.recent("policy")[0]["id"] == run


def test_changed_content_is_eligible_before_the_revisit_cooldown(scanner, monkeypatch):
    async def invoke(*_args):
        return PROPOSAL | {"change": None}

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    asyncio.run(scanner.scan())
    scanner.github.sources[PATH] += "// a real source update\n"
    asyncio.run(scanner.scan())
    assert scanner.state.used() == 2
    assert scanner.state.core_progress()[PATH]["reviewed"] == 1


def test_a_new_scan_never_selects_an_unprovided_patch_target(scanner, monkeypatch):
    async def invoke(*_args):
        return copy.deepcopy(PROPOSAL) | {"change": PROPOSAL["change"] | {"path": "scripts/a.py"}}

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "failed"
    assert scanner.state.used() == 1 and not scanner.github.writes


def test_budget_exhaustion_before_review_can_retry_after_reset_without_remote_replay(
    scanner, monkeypatch
):
    scanner.config.max_model_calls_per_day = scanner.settings.max_model_calls_per_day = 4
    exhausted = [False]

    async def invoke(_c, _data, schema, _policy, _directory):
        if schema is CORE_PROPOSAL and not exhausted[0]:
            exhausted[0] = True
            for _ in range(3):
                scanner.state.reserve(4)
        return copy.deepcopy(PROPOSAL if schema is CORE_PROPOSAL else REVIEW)

    monkeypatch.setattr("mooncake_bot.core.invoke", invoke)
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "budget_exhausted"
    assert scanner.state.core_problems({"budget_wait"})
    assert not scanner.github.writes and scanner.state.bucket_used("core") == 1
    scanner.clock[0] += 86400
    asyncio.run(scanner.scan())
    assert scanner.state.recent()[0]["state"] == "pending"
    assert scanner.state.used() == scanner.state.bucket_used("core") == 2


def test_missing_or_corrupt_pending_artifacts_do_not_crash_the_scheduler(scanner):
    asyncio.run(scanner.scan())
    row = scanner.state.core_problems({"pending"})[0]
    (scanner.state.directory / row["run_id"] / "candidate.json").unlink()
    scanner.settings.publish_projects = True
    scanner.publish_pending()
    assert scanner.state.recent()[0]["state"] == "stale_input"
    assert not scanner.github.writes
