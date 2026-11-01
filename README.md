# stmatengss-Mooncake-bot

This standalone repository contains the GitHub App bot and the continuous-improvement
worker for [`stmatengss/Mooncake`](https://github.com/stmatengss/Mooncake). It does not
vendor the private LightSeek dispatcher. Keep credentials, live configuration and runtime
artifacts outside Git; the directory's `.gitignore` excludes them.


GitHub App：[`stmatengss-mooncake-bot`](https://github.com/apps/stmatengss-mooncake-bot)。
Bot 源码在本仓库独立维护；业务仓库是 `stmatengss/Mooncake`。

## 使用方法

在 `stmatengss/Mooncake` 的 Issue 或开放 PR 下新发一条评论。维护者可使用：

| 命令（前面加 `@stmatengss-mooncake-bot`） | 功能 | 适用位置 |
| --- | --- | --- |
| `review` | 一次 COMMENT review，包含摘要和经过行号校验的按行缺陷；同一提交的相同行不重复评论 | PR |
| `summary` | 更新 Bot 自己的摘要评论 | PR |
| `describe` | 更新 PR 描述中的 Bot 标记块，保留作者原文和 checklist | PR |
| `title` | 按 Mooncake 前缀规范整理标题 | PR |
| `ask <问题>` 或直接写问题 | 根据提供的代码与讨论回答 | Issue / PR |
| `plan <目标>` | 给出实现步骤与待执行的验证计划 | Issue / PR |
| `triage` | 分析模块、严重性、缺失复现信息和建议标签 | Issue / PR |
| `fix <文件路径和修复要求>` | 单文件、最多 80 行的补丁，经独立静态 review 后创建草稿 PR | Issue / PR |
| `explain-ci` | 解释当前提交失败的检查、步骤和 annotations | PR |
| `rerun-ci [run-id]` | 重跑当前提交上已完成失败的 GitHub Actions jobs，最多三次 attempt | PR |
| `help` / `status` | 命令说明 / 本 Issue 或 PR 的任务状态、共享预算和 RSI 进展 | Issue / PR |

示例：

```text
@stmatengss-mooncake-bot review
@stmatengss-mooncake-bot fix scripts/generate_cluster_topology.py 处理延迟解析失败的情况
@stmatengss-mooncake-bot status
```

Issue 中说明具体文件路径，可以让问答、计划和修复读取对应文件。
修改旧评论不会触发；命令必须位于新评论开头，只有配置中允许且当前仍有仓库写权限的维护者能触发。
CI 解释使用检查结果和失败步骤，不读取原始日志。重跑请求成功只说明 GitHub 接收了调度请求。

## 自动功能

启用相应配置后，无需每次输入命令：

- `auto_review`：非草稿 PR 打开、更新或转为 ready 后，生成摘要与按行缺陷审查。
- `auto_summary`：未启用自动 review 时，单独维护摘要评论；启用 review 时复用其摘要，不额外调用模型。
- `auto_title`：为缺少前缀的 PR 标题补充 `[Bugfix]`、`[Doc]`、`[CI/Build]` 等前缀。
- `auto_triage`：为新建、重新打开或修改的 Issue 提供排查方案和标签建议，不自动改标签。
- `auto_ci`：当前 PR 提交的 CI 失败后更新诊断评论，不自动重跑 CI。

自动任务只处理 `automation_author_ids` 中的作者；为空则使用维护者白名单。
如需处理本 App 创建的 PR，将其准确的 Bot 数字 ID 显式加入；其他 Bot 不会获得授权。
在 Issue / PR 描述加入 `<!-- mooncake-bot SKIP -->` 可跳过自动处理。

除 webhook 外，每 300 秒扫描最近更新的 20 个开放 PR 和 20 个开放 Issue，
恢复遗漏的事件。扫描按提交和任务类型持久化去重，已失败或中断的任务不会被重复发布。
扫描也支持当前仅订阅评论事件的安装。若要即时响应，可在 App 设置额外订阅
Pull request、Issues 和 Workflow run 事件。手动扫描：

```text
mooncake-bot scan-once --config /path/to/config.toml
```

`fix` 只修改 `fix_paths` 下提供的现有源码文件，最多三个输入文件、一个修改文件。
不修改 workflow、治理指令或密钥，不执行仓库代码；保留原文件 mode，拒绝符号链接。
Python 做语法检查；其他支持的语言只检查补丁边界，不等同于编译通过。
PR 中请求的修复会创建一个以该 PR 分支为 base 的子草稿 PR，Issue 中请求则以默认分支为 base。
跨仓库 fork PR 可审查，但不会在其分支上创建修复。默认最多两个开放 `sima/bot-fix-*` 草稿。

RSI 在 DSW 独立常驻：项目循环每 12 小时检查指定脚本，有实际缺陷时保存候选，
获得 App Contents 写权限并开启发布后创建草稿 PR；Bot 循环每 24 小时评估审查指令，
仅在留出集有提升且没有回归时启用新指令。所有入口共享可配置的每日模型调用上限（示例为 24 次），项目循环最多两个开放候选 PR。
候选 PR 由维护者审阅和合并。

## 主动巡检核心代码

在运行配置 `rsi.json` 中启用 `core_scan_enabled=true` 后，常驻 RSI worker 每小时自动
轮转 Store、TransferEngine/TENT、Common、P2P Store、Store-RS 和 Conductor 的原生源码。
不需要创建 Issue 或输入命令。`core_scan_roots` 可缩小到这些已登记的源码目录。

每轮从默认分支的固定提交选择最多三个模块的源码窗口，并读取对应头文件和目录级
`AGENTS.md`。大文件按有重叠的窗口处理，支持最多 1 MB 的现有文件；不会执行仓库代码。
游标持久保存在 SQLite 中，重启后继续轮转。源码内容变化后重新登记；不变内容完成一轮后
默认等待七天再扫。扫描是局部静态分析，计数表示窗口处理记录，不等同于完整审计或测试覆盖率。

候选必须有具体触发条件、影响和精确匹配的单文件补丁，最多改动 80 行。另一次独立模型调用
必须同时确认原问题成立、修复正确且上下文足够，才进入发布队列。C++/Rust/Go 等候选没有
编译或运行测试，草稿中会明确说明。与开放 PR 涉及的文件重叠时跳过；相同补丁不会反复提交。

开启 `publish_projects=true` 后，可信发布器自动创建 `rsi/core-*` 分支和草稿 PR。
它会复核全部输入 blob、目录指令和已审查补丁，并保留文件 mode。主分支有无关更新时，
输入完全不变的候选可以重新校验后发布到新 base。输入变化则放弃旧候选，不发布陈旧结果。

核心巡检默认最多使用每天 24 次调用，计入现有共享总额；失败调用同样计数。
Store 脚本候选与核心候选共享 `max_open_proposals` 上限（默认两个开放 PR）。
开放 PR 满额时，最多额外保存同样数量的已验证待发布候选；满额后等待审核释放容量。
容量释放后自动发布，无需重新生成或手动触发。未知远端写入结果不会自动重放。

查看当前预算、已处理窗口和候选状态：

```sh
mooncake-bot rsi-status --config /root/mooncake-bot/config.toml --rsi-config /root/mooncake-bot/rsi.json
```

`touch data/rsi/STOP` 会同时暂停脚本巡检、核心巡检和审查指令改进；删除后自动恢复。

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

- `stmatengss-mooncake-bot`: accepts signed, authorized comment and automation events,
  reconciles recent PRs/issues/workflow failures and processes the persistent SQLite queue.
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

- Manual commands, automatic tasks and RSI share a configurable daily allowance (**24 in examples**),
  including failed invocations. A fix normally needs two calls: proposal and independent review.
  Help, status, title normalization and CI retry use no model. Model tasks wait in the durable
  queue until the next UTC reset if the budget is exhausted; utility commands can still run.
  Eligible maintainer commands take priority over automatic work; tasks on the same PR remain serial.
  RSI also obeys its separately configured limit when lower than this global cap.
  To raise the deployed allowance, update `max_model_calls_per_day` consistently in the live
  `config.toml` and `rsi.json`, then restart the bot and RSI programs. Confirm the effective
  allowance with `status` and `rsi-status`; changing the limit does not clear recorded usage.
- At most two script/core candidate PRs may remain open by default. Legacy script proposals
  deduplicate identical source forever; proactive core windows use durable rotation, content
  identities, a revisit cooldown and patch-level duplicate suppression.
- Restart never automatically repeats an interrupted attempt or ambiguous remote
  write. Inspect its artifacts and GitHub before manually retrying.
- Kimi uses an isolated `KIMI_CODE_HOME`, a tool-free agent and an empty skills
  directory. No GitHub credentials enter the model subprocess environment. The
  current CLI adapter rejects serialized requests over 100,000 UTF-8 bytes. Analysis contexts
  above 64,000 bytes use bounded source/diff/check excerpts and explicitly disclose partial
  coverage. Findings must anchor to added lines actually included in that view. CI diagnosis
  omits full source files and uses check evidence. This does not establish whole-PR coverage.
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
gates, command routing, migration, inline publication, CI retry and child draft publication.
Real model evaluations and webhook deliveries are
separate operational checks.

Kimi agent restrictions follow the [official agent-file documentation](https://moonshotai.github.io/kimi-code/en/customization/agents).
