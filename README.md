# stmatengss-Mooncake-bot

This standalone repository contains the GitHub App bot and the continuous-improvement
worker for [`stmatengss/Mooncake`](https://github.com/stmatengss/Mooncake). It does not
vendor the private LightSeek dispatcher. Keep credentials, live configuration and runtime
artifacts outside Git; the directory's `.gitignore` excludes them.


GitHub App：[`stmatengss-mooncake-bot`](https://github.com/apps/stmatengss-mooncake-bot)。
Bot 源码在本仓库独立维护；业务仓库是 `stmatengss/Mooncake`。

## 使用方法

在目标仓库的开放 PR 下新发一条评论：

```text
@stmatengss-mooncake-bot review
```

解释失败的 CI：

```text
@stmatengss-mooncake-bot explain-ci
```

只有配置中允许的维护者能触发；修改旧评论、创建 PR、推送提交不会触发 review。
回复会注明分析的提交和 AI 辅助信息。CI 解释使用检查结果和失败步骤，不读取原始日志。

RSI 在 DSW 独立常驻：项目循环每 12 小时检查指定脚本，有实际缺陷时保存候选，
获得 App Contents 写权限并开启发布后创建草稿 PR；Bot 循环每 24 小时评估审查指令，
仅在留出集有提升且没有回归时启用新指令。每日最多 24 次 RSI 模型调用，最多两个开放候选 PR。
候选 PR 由维护者审阅和合并。

## 本地开发

```sh
git clone https://github.com/stmatengss/stmatengss-Mooncake-bot.git
cd stmatengss-Mooncake-bot
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/pre-commit run --files $(git ls-files)
cp config.example.toml config.toml
cp rsi.example.json rsi.json
```

填写真实 App 和 installation ID、私钥路径及 Kimi 配置，再启动服务。
生产配置、私钥、Kimi 凭据、运行数据均在 Git 外维护，复制本仓库不会复制现有授权。
Linux/DSW 使用 `deploy/dsw-supervisor.conf`；运维入口见下文。

源码导出自 [Mooncake PR #34](https://github.com/stmatengss/Mooncake/pull/34)，
完整来源见 [ORIGIN.md](ORIGIN.md)，采用 [Apache-2.0](LICENSE-APACHE) 许可。
未来 bot 代码以本仓库为维护和部署来源。

## What runs continuously

Three Supervisor programs run independently on the configured DSW instance:

- `stmatengss-mooncake-bot`: accepts signed, authorized PR comment events and
  processes the persistent SQLite queue. Commands are
  `@stmatengss-mooncake-bot review` and `@stmatengss-mooncake-bot explain-ci`.
- `stmatengss-mooncake-tunnel`: supervises Cloudflare, probes the public endpoint
  with signed pings, and updates the GitHub App webhook after an address change.
- `stmatengss-mooncake-rsi`: checks its persistent schedule every minute and runs
  the two improvement loops below. `autostart` and `autorestart` are enabled in
  `deploy/dsw-supervisor.conf`. Individual process restart is independent of a
  whole-instance reboot. The DSW instance must remain running; replacing its
  filesystem requires restoring this deployment.

## Improvement loops

The project loop runs at most once per 12 hours. It reads an explicitly enrolled
Python script from a fixed commit of this fork and asks Kimi for one concrete bug
fix, or no change. It checks that exactly one snippet in that file matches,
parses the proposed Python source, caps the diff at 80 lines, and obtains another
static review. Candidate code and suggested commands are never executed on DSW.
With publication enabled, the trusted publisher creates a new `rsi/project-*`
branch and **draft** PR in this fork. It never updates the default branch or
merges a PR. Review feedback and merged/closed outcomes feed later proposals;
merge status is an acceptance signal, not proof that a diagnosis was correct.
Without publication permission, validated patches remain `pending` in artifacts.
After an operator enables publication, the worker rechecks the stored patch,
static-review result, open-PR limit and base commit before publishing it without
another model generation. Ambiguous publication failures require inspection.

The review-instruction loop runs at most once per 24 hours. It evaluates the
current instruction on eight fixed synthetic cases, proposes an instruction using
only the four development cases and their results, and evaluates the candidate
on all cases. The four holdout cases and labels are excluded from optimizer input.
Promotion requires strictly more holdout true positives, no additional holdout
false positives, and no development regression. A valid alternative line anchor
counts as one defect. The active instruction, parent hash, benchmark hash and
metrics are written atomically. The immutable base policy, JSON schema, tool
restrictions, authorization, budgets and scoring code cannot be changed by a
candidate. Each promotion influences the next generation's baseline. At the
benchmark ceiling, prompt search waits for a reviewed benchmark or active-policy
change instead of spending calls on further ties.

This is bounded improvement of code proposals and review instructions. It does
not train model weights or establish general recursive intelligence. The small
synthetic holdout is a smoke benchmark; a win does not prove better reviews on
unseen real PRs. Expand and independently review benchmark versions before making
broader quality claims. A tie or regression retains the current instruction.

## Limits, failure and recovery

- The RSI budget defaults to **24 model invocations per UTC day**, including
  failed invocations. Manual PR commands have their own existing queue and timeout.
- At most two project candidate PRs may remain open. Identical source content is
  not proposed again after a completed, failed or interrupted attempt.
- Restart never automatically repeats an interrupted attempt or ambiguous remote
  write. Inspect its artifacts and GitHub before manually retrying.
- Kimi uses an isolated `KIMI_CODE_HOME`, a tool-free agent and an empty skills
  directory. No GitHub credentials enter the model subprocess environment. The
  current CLI adapter rejects serialized requests over 100,000 UTF-8 bytes.
- Project candidates receive syntax and static-review checks, **not repository
  test execution**. Their draft PRs state this. Native, GPU and RDMA validation
  remains the submitter's responsibility.
- Temporary tunnel address changes create a short delivery gap. Failed GitHub
  deliveries may need redelivery after the endpoint recovers.

## Deployed source and runtime layout

For the existing DSW deployment, source is checked out at
`/root/stmatengss-Mooncake-bot`; runtime state stays under `/root/mooncake-bot`.
The existing virtual environment installs this source checkout in editable mode.
Supervisor uses the source directory as its working directory and explicit
runtime configuration paths, preserving the queue, budgets, secrets and Kimi
home across the repository migration. The deployed revision and source URL are
recorded in runtime `data/deployment-source.json`.

## Setup and operator commands

Use Python 3.12 and an already configured Kimi Code installation. Do not modify
the shared Kimi configuration. Copy only its provider/model configuration into
`runtime/kimi-home/config.toml`, with permissions `0600` and parent `0700`.
Disable automatic CLI updates in that isolated home's `tui.toml` for reproducible
deployment. Keep the GitHub App private key and webhook secret in `secrets/`.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
cp rsi.example.json rsi.json
# Fill in the actual App/repository/installation IDs and actor allowlist.
# Select backend="kimi" and the absolute path of the installed Kimi executable.
.venv/bin/mooncake-bot status
.venv/bin/mooncake-bot rsi-status
.venv/bin/mooncake-bot rsi-once
```

New configurations default to preview-only publication. Enabling automatic draft
PRs additionally requires App Contents write permission for **only this fork**;
the ordinary reviewer still mints Contents read tokens. No personal GitHub token
needs to be copied to DSW. Install the pinned official `cloudflared` executable
at `.tools/cloudflared`, and install the Supervisor configuration using the
instance's existing Supervisor. Reload only these program groups.

```sh
# On the configured DSW, from /root/mooncake-bot:
.venv/bin/mooncake-bot rsi-status
touch data/rsi/STOP           # pause both RSI loops; PR review stays available
rm data/rsi/STOP              # resume at the next scheduling check
```

Inspect `data/rsi.log`, `data/rsi/state.sqlite3`, and
`data/rsi/<run-id>/outcome.json`. Each model call retains its prompt, raw JSONL,
stderr and exit status under that run directory. A project proposal also retains
`candidate.patch`; publication records the branch and commit before creating a
PR. To roll back an instruction, restore a promoted run's `previous-policy.json`
to the configured `policy_file`, or archive the active file to restore the base
policy. Pause the worker during rollback. Never publish raw operational logs or
configuration containing credentials.

## Validation

```sh
python -m pytest -q
ruff check mooncake_bot tests
ruff format --check mooncake_bot tests
```

The GitHub workflow runs CPU tests without model credentials or write permissions.
Unit tests cover event authentication, deduplication, stale-head rejection, Kimi
stream validation, budget persistence, patch scope, holdout separation, promotion
gates and draft publication. Real model evaluations and webhook deliveries are
separate operational checks.

Kimi agent restrictions follow the [official agent-file documentation](https://moonshotai.github.io/kimi-code/en/customization/agents).
