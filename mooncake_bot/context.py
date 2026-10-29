"""Bound analysis inputs without silently claiming full source coverage."""

import copy
import json
import re

MAX_CONTEXT_BYTES = 64_000
COVERAGE_LIMIT = (
    "Large input was reduced to bounded source/diff/check excerpts. "
    "This is partial analysis, not a complete audit of every changed line."
)


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def clip(text, maximum):
    if len(text.encode()) <= maximum:
        return text
    shortened = text.encode()[:maximum].decode("utf-8", errors="ignore")
    return shortened.rsplit("\n", 1)[0] if "\n" in shortened else shortened


def source_excerpt(source, patch):
    lines = source.splitlines()
    selected = set(range(min(24, len(lines))))
    for start in re.findall(r"^@@ .*?\+(\d+)", patch or "", re.M)[:8]:
        center = int(start) - 1
        selected.update(range(max(0, center - 5), min(len(lines), center + 30)))
    return clip("\n".join(f"L{i + 1}: {lines[i]}" for i in sorted(selected)), 6000)


def compact_context(context, maximum=MAX_CONTEXT_BYTES):
    if size(context) <= maximum:
        return context
    view = copy.deepcopy(context)
    view["coverage_limits"] = [COVERAGE_LIMIT]
    for file in view.get("files", []):
        if file.get("content"):
            file["source_excerpt"] = source_excerpt(file["content"], file.get("patch"))
            file["content"] = None
            file["content_complete"] = False
    ci = view.get("ci", {})
    if ci:
        ci["runs"] = ci.get("runs", [])[:3]
        ci["annotations"] = ci.get("annotations", [])[:20]
        for workflow in ci["runs"]:
            workflow["jobs"] = workflow.get("jobs", [])[:10]
            for job in workflow["jobs"]:
                job["steps"] = job.get("steps", [])[:10]

    def texts(value):
        if isinstance(value, dict):
            for key, child in value.items():
                # Revision, file identities and line anchors must never be rewritten.
                if key in {
                    "filename",
                    "path",
                    "head",
                    "base",
                    "head_ref",
                    "base_ref",
                    "repository",
                    "coverage_limits",
                    "commit_id",
                }:
                    continue
                if isinstance(child, str) and len(child.encode()) > 256:
                    yield value, key, child
                elif isinstance(child, (dict, list)):
                    yield from texts(child)
        elif isinstance(value, list):
            for child in value:
                yield from texts(child)

    while size(view) > maximum:
        candidates = list(texts(view))
        if not candidates:
            raise ValueError("Context metadata exceeds the bounded analysis input limit")
        parent, key, text = max(candidates, key=lambda item: len(item[2].encode()))
        parent[key] = clip(text, max(256, len(text.encode()) // 2))
        if key == "patch":
            parent["patch_complete"] = False
    return view


def check_analysis(result, context, view):
    from .agent import check_result

    check_result(result, view)
    if view is not context:
        result["limitations"] = list(dict.fromkeys(result["limitations"] + [COVERAGE_LIMIT]))
    return check_result(result, context)
