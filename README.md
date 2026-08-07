# XiaoYi Runner、Agent Judge 与 HALO 使用指南

本文说明以下三个项目内 Skill 的职责和组合方式：

- [`run-xiaoyi-loop`](../../run-xiaoyi-loop/SKILL.md)：运行小艺 Task、收集产物并使用独立 Agent Judge 评分。
- [`halo-rlm-agent-driven`](../../halo-rlm-agent-driven/SKILL.md)：诊断单个或一组 OTel/JSONL Trace，生成严格的 HALO JSON 报告。
- [`run-xiaoyi-halo-loop`](../../run-xiaoyi-halo-loop/SKILL.md)：薄编排层，串联 Runner、Agent Judge 和每 Task 独立 HALO 诊断。

## 如何选择

| 目标 | 使用的 Skill |
| --- | --- |
| 运行小艺 Task 并评分 | `run-xiaoyi-loop` |
| 已有 JSONL，只需要诊断轨迹 | `halo-rlm-agent-driven` |
| 一键运行、Judge、再诊断 | `run-xiaoyi-halo-loop` |
| 已有批次 handoff，只继续 HALO | `run-xiaoyi-halo-loop` |

## 环境要求

- Python >= 3.10。
- 所有 JSON、JSONL、Prompt、Manifest 和报告使用 UTF-8。
- Windows 控制台编码异常时设置：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

- `run-xiaoyi-loop` 的 Python 依赖见：
  `run-xiaoyi-loop/scripts/requirements.txt`。
- `halo-rlm-agent-driven` 只使用 Python 标准库。
- Runner 需要可用的 HDC 和唯一、明确的小艺设备目标。
- Agent Judge 和 HALO 都不需要外部 Judge/LLM API Key。

## 快速使用案例

| 场景 | 可直接发送给 Agent 的请求 |
| --- | --- |
| 只运行并评分 | `使用 run-xiaoyi-loop，在当前 workspace 执行 Task 14、25、117，并完成 Agent Judge。` |
| 已有 Trace，只诊断 | `使用 halo-rlm-agent-driven 诊断 D:\workspace\xiaoyi_logs\task14\task14.jsonl，生成中文报告。` |
| 一键完整流程 | `使用 run-xiaoyi-halo-loop 执行、Judge 并诊断 Task 14、25、117，最后生成批次汇总。` |
| 只诊断错误项 | `使用 run-xiaoyi-halo-loop 执行并 Judge Task 14、25、117，只诊断失败或异常 Task。` |
| 从 handoff 继续 | `从 D:\workspace\xiaoyi_halo\handoff.json 继续 HALO 诊断和汇总，不要重新运行 Runner 或 Judge。` |

默认完整流程会诊断所有具有可用 Trace 的 Task，包括 Judge 分数为 `1` 的 Task。
自定义目录、具体命令、输入输出和失败处理见下方对应 Skill 的使用方法。

## 统一运行目录

没有配置自定义路径时，三个批次产物目录默认位于同一个 Agent workspace 下：

```text
<agent_workspace>/
├── xiaoyi_logs/
├── xiaoyi_judge/
├── xiaoyi_halo/
└── pipeline_state.json
```

主要产物结构：

```text
xiaoyi_logs/
└── task<ID>/task<ID>.jsonl

xiaoyi_judge/
├── task<ID>/
│   ├── metadata.json
│   ├── case_manifest.json
│   ├── agent.json
│   ├── normalized_runner_log.jsonl
│   ├── data/
│   ├── output/
│   └── judge_result.json
└── batch_summary.json

xiaoyi_halo/
├── handoff.json
├── task<ID>_halo/
│   ├── task<ID>.halo.jsonl
│   ├── halo-prepared-manifest.json
│   ├── halo_prompt.txt
│   └── halo_report.json
└── batch_summary.json
```

## 1. 使用 run-xiaoyi-loop

支持单个 ID、多个 ID、`1-10`、`1..10` 和逗号组合。Task 目录应包含
`metadata.json`；其 `absolute_id`、`task` 和 `rubrics` 必须有效。

### Runner 阶段

从项目根目录执行：

```powershell
python run-xiaoyi-loop/scripts/run_tasks.py 14 25 117 `
  --workspace "<agent_workspace>"
```

该命令只启动 Runner 阶段。Runner 通常顺序执行 Task，所有任务证据统一写到
`xiaoyi_logs/task<ID>/`。运行失败时，可用 Trace 仍保存在该目录并标记
`status=failed`；只要 `task<ID>.jsonl` 存在，就继续进入 Judge 和 HALO。

Runner 是长时间、低输出进程。若返回可等待的执行句柄，应继续等待同一句柄，
不得重复启动。Runner 不创建锁文件；同一批次是否仍在运行以原进程句柄和
`pipeline_state.json` 的阶段、当前 Task 与 deadline 为准。

### Prepare 与 Judge 阶段

Runner 达到 `runner-done` 后，对本批次所有成功收集 Trace 的 Task 准备证据；这些 ID
记录在 `runner.completed` 中，也可能同时出现在 `runner.timedOut` 或 `runner.failed`：

```powershell
python run-xiaoyi-loop/scripts/prepare_logs.py `
  --workspace "<agent_workspace>" `
  --task-id 14 --task-id 25 --task-id 117
```

随后为每个准备成功的 Task 派发一个独立 Judge subagent：

```text
Judge Agent 14 → xiaoyi_judge/task14/judge_result.json
Judge Agent 25 → xiaoyi_judge/task25/judge_result.json
Judge Agent 117 → xiaoyi_judge/task117/judge_result.json
```

Judge subagent 可以并发，但一个 subagent 只能 Judge 一个 Task。父 Agent 必须读取并
校验每个 `judge_result.json`，再写本批次 `xiaoyi_judge/batch_summary.json`。

Judge 使用 `normalized_runner_log.jsonl` 和实际 `data/`、`output/` 产物；不得调用
项目的外部 Judge API。

## 2. 使用 halo-rlm-agent-driven

### 准备 Trace

HALO 使用原始 Runner JSONL，不使用 Judge 的 `normalized_runner_log.jsonl`：

```powershell
python halo-rlm-agent-driven/scripts/prepare_trace.py `
  "<agent_workspace>\xiaoyi_logs\task14\task14.jsonl" `
  --output-root "<agent_workspace>\xiaoyi_halo"
```

后续必须使用 `halo-prepared-manifest.json` 返回的精确 `trace_path`、
`prompt_path` 和 `report_path`，不得自行重新推导。

原始 Trace、Task/Judge 输入目录和已有产物均按只读处理。编排流程传入单个 Task 的
精确 JSONL，不递归扫描 `xiaoyi_logs` 根目录。索引 sidecar 是可复用缓存，不需要也
不得逐个删除；已有 `halo_report.json` 可以由新的、通过校验的诊断报告直接覆盖。

### 构建 Prompt

从 `halo-rlm-agent-driven/scripts` 目录执行：

```powershell
python -m halo_rlm.agent_cli build-prompt `
  --output "<manifest.prompt_path>" `
  --task-json "<agent_workspace>\xiaoyi_judge\task14\metadata.json" `
  --judge-result "<agent_workspace>\xiaoyi_judge\task14\judge_result.json"
```

`task` 和 `expected_output_files` 不是独立文件。HALO 从 `--task-json` 指向的
`metadata.json` 中读取：

```text
task                  ← task 或 description
expected_output_files ← output_files 或 expected_output_files
```

每个 Trace 只能构建一次 Prompt。诊断必须使用该 Prompt，生成 schema-v5 UTF-8
JSON 报告，并反复执行以下校验直到成功：

```powershell
python -m halo_rlm.agent_cli validate-report "<manifest.report_path>"
```

默认不传 `--surface`，HALO 使用内置的逻辑目标名白名单；不需要准备任何 Surface
文件或目录。只有明确需要覆盖该白名单时才传目标名称。

`diagnosis` 和 `proposed_changes` 下的人类可读内容使用简体中文，JSON 字段名、枚举、
ID、时间戳、路径和原始证据保持原样。

## 3. 使用 run-xiaoyi-halo-loop

### 诊断模式

默认诊断所有具有可用原始 Trace 的 Task。只有用户明确要求只诊断错误项时才使用
`failed`。对应模式：

- `all`（默认）：诊断所有具有标准原始 Trace 的 Task，不要求 Judge 成功。
- `failed`：保守地诊断 Runner 失败/超时、Judge `passed=false`、Judge 执行错误、
  Judge 结果缺失或无效，以及 Judge fingerprint 不匹配的 Task。

### 执行顺序

```text
Runner：Task 14 → Task 25 → Task 117
                    ↓
Prepare：处理本批次所有已收集标准 Trace 的 Task
                    ↓
Judge：一 Task 一个独立 Judge subagent
                    ↓
等待全部 Judge 完成并验证
                    ↓
创建并解析一个批次 handoff.json
                    ↓
HALO：一 Task 一个独立诊断 subagent
                    ↓
父 Agent 重新验证报告并生成批次汇总
```

Judge 与 HALO 使用相同的外层调度模式：建立队列、一个 Task 一个 fresh subagent、
按可用并发槽填充、等待完成后继续补位。但两者的输入、职责和输出不同。

### 创建 handoff

Runner 和 Judge 整批完成后执行：

```powershell
python run-xiaoyi-halo-loop/scripts/handoff.py create `
  --workspace "<agent_workspace>" `
  --diagnose-mode all `
  --task-id 14 --task-id 25 --task-id 117
```

存在 `<agent_workspace>/.xiaoyi-loop/local.toml` 或 `XIAOYI_LOOP_CONFIG` 时，自动使用
其中的 `paths.logs_dir` 和 `paths.run_dir`；没有配置时使用 workspace 默认目录。
命令行 `--logs-root`、`--judge-run-root` 优先级最高。`xiaoyi_halo` 默认从解析后的
Judge 根目录同级推导。handoff 只保存解析后的批次根目录和 Task IDs。

### 解析 handoff

```powershell
python run-xiaoyi-halo-loop/scripts/handoff.py resolve `
  "<resolved_halo_output>\handoff.json"
```

`resolve` 负责严格验证 schema、三个互异的绝对根目录、Task ID、Judge 状态和 fingerprint，
并为 eligible Task 返回 HALO 所需的精确路径。HALO 本身不直接解析 XiaoYi handoff；
薄编排层负责把解析结果交给每个 HALO subagent。

每个 HALO subagent 只能处理一个 Task，并只能写自己的目录：

```text
HALO Agent 14  → xiaoyi_halo/task14_halo/
HALO Agent 25  → xiaoyi_halo/task25_halo/
HALO Agent 117 → xiaoyi_halo/task117_halo/
```

一个诊断失败不应中断其他 Task。全部诊断 subagent 结束后，父 Agent 必须读取每个
manifest 和 report，并再次运行 `validate-report`，不能只相信 subagent 完成消息。

### 批次汇总

每份 `halo_report.json` 通过严格校验后，默认执行：

```powershell
python run-xiaoyi-halo-loop/scripts/handoff.py summarize `
  "<resolved_halo_output>\handoff.json"
```

输出：

```text
<resolved_halo_output>/batch_summary.json
```

## 从已有 handoff 继续

如果 Runner 和 Judge 已经完成，可以直接请求：

```text
使用 run-xiaoyi-halo-loop，从
<resolved_halo_output>/handoff.json 继续执行 HALO 诊断和汇总，不要重新运行
Runner 或 Judge。
```

编排 Agent 将跳过 Runner/Judge，从 `handoff.py resolve` 开始。

## 关键边界

- Runner 状态不决定 Judge 资格；只要 `task<ID>/task<ID>.jsonl` 存在，就进入 Judge
  和 HALO。没有 Trace 的任务跳过并继续处理下一项。
- Judge 执行失败、结果缺失或无效不会阻止基于原始 Trace 的 HALO 诊断；此时不向
  HALO 传递无效 Judge 上下文。`failed` 模式会选中这些 Task。
- HALO 使用原始 `xiaoyi_logs/task<ID>/task<ID>.jsonl`。
- Judge 使用 `xiaoyi_judge/task<ID>/normalized_runner_log.jsonl`。
- 不得用历史日志或旧 Judge 结果冒充当前批次产物。
- `case_manifest.json` 与 `judge_result.json` 的 `inputFingerprint` 必须一致。
- 每个 Task 的 Prompt 只能构建一次；报告必须通过本地严格校验。
