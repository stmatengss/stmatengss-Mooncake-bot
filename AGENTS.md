# AGENTS.md

This repository owns the bot source; `stmatengss/Mooncake` is the enrolled target.

- Keep live configuration, keys, provider credentials and data out of Git.
- Preserve signed webhook verification, enrolled installation/repository and actor checks.
- Keep generated candidates within configured targets and budgets. The bot opens draft PRs; merging remains a maintainer action.
- Keep model tools disabled and GitHub credentials outside model subprocesses.
- Run `python -m pytest -q` and PR-scoped `pre-commit run --files ...` for relevant changes.
- Report local tests, hosted CI, live model evaluation and deployment separately.
- Use `[Bugfix]`, `[CI/Build]`, `[Doc]` or `[Misc]` PR title prefixes and disclose AI assistance.
