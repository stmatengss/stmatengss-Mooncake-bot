"""Source enrollment and bounded, line-preserving windows for proactive scans."""

from pathlib import PurePosixPath

ROOTS = (
    "mooncake-store/src/",
    "mooncake-store/include/",
    "mooncake-store/rust/src/",
    "mooncake-transfer-engine/src/",
    "mooncake-transfer-engine/include/",
    "mooncake-transfer-engine/tent/src/",
    "mooncake-transfer-engine/tent/include/",
    "mooncake-common/src/",
    "mooncake-common/include/",
    "mooncake-p2p-store/src/",
    "mooncake-store-rs/crates/",
    "mooncake-conductor/src/",
    "mooncake-conductor/include/",
)
SUFFIXES = {".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".cuh", ".rs", ".go", ".py"}
EXCLUDED = {
    "test",
    "tests",
    "example",
    "examples",
    "third_party",
    "thirdparty",
    "vendor",
    "external",
    "build",
    "generated",
    ".git",
    ".github",
    "cachelib_memory_allocator",
}
MAX_SOURCE_BYTES = 1_000_000
WINDOW_BYTES = 12000


def allowed(path, roots=ROOTS):
    name = PurePosixPath(path)
    return (
        str(name) == path
        and not name.is_absolute()
        and ".." not in name.parts
        and not set(name.parts) & EXCLUDED
        and name.suffix in SUFFIXES
        and not name.stem.startswith("test_")
        and not name.stem.endswith(("_test", "_tests"))
        and any(path.startswith(prefix) for prefix in roots)
        and any(path.startswith(prefix) for prefix in ROOTS)
    )


def windows(source):
    lines = source.splitlines(keepends=True)
    result, start = [], 0
    while start < len(lines):
        if len(lines[start].encode()) > WINDOW_BYTES:
            start += 1
            continue
        end, size = start, 0
        while end < len(lines) and end - start < 220:
            length = len(lines[end].encode())
            if size + length > WINDOW_BYTES:
                break
            size += length
            end += 1
        result.append(
            {
                "start_line": start + 1,
                "end_line": end,
                "source": "".join(lines[start:end]),
                "total_lines": len(lines),
            }
        )
        if end == len(lines):
            break
        start = max(start + 1, end - 15)
    return result


def guidance_paths(paths):
    result = {"AGENTS.md", "CONTRIBUTING.md", ".github/pull_request_template.md"}
    for path in paths:
        parts = PurePosixPath(path).parts[:-1]
        result.update("/".join(parts[:i]) + "/AGENTS.md" for i in range(1, len(parts) + 1))
    return sorted(result)


def risk(path):
    name = PurePosixPath(path).stem.lower()
    words = ("allocator", "metadata", "client", "transport", "master", "memory", "rpc")
    return (-sum(word in name for word in words), path)
