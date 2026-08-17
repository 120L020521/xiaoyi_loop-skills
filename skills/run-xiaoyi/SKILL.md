---
name: run-xiaoyi
description: >-
  Route and orchestrate XiaoYi Runner, Judge, and HALO requests from one
  user-facing control Skill, including the complete Judge-to-HALO batch workflow.
  Run only the Runner by default; append the shared batch Judge only when the user
  explicitly requests Judge/评分/打分, and append HALO only when the user explicitly
  requests 诊断/HALO/Trace diagnosis. Use for generic “让小艺执行任务”, 文件整理任务/
  FileOrganization IDs backed by setup.json/expect.json/source/prompts, numeric
  Task/task/WorkspaceBench requests backed by metadata.json, existing handoff.json
  batches, or standalone OTel/OpenTelemetry JSONL trace diagnosis. Resolve the
  terminal stage, wording, ID shape, and dataset schema before any HDC mutation.
---

# Route XiaoYi Runner Stages

Act as the single user-facing control entry. Select exactly one dataset Runner
when execution is requested, then execute only the stages explicitly requested.
The default terminal stage is Runner, not Judge. Own top-level routing and the
Judge-to-HALO batch orchestration, while keeping Runner, Judge, and actual HALO
trace diagnosis in their specialized Skills.

Never start a Judge subagent while a selected Runner case or Task is pending.
Never run both execution children for one selector. Do not copy their mechanical
HDC rules or the shared Judge's scoring rules into this coordinator.

## Route existing diagnosis artifacts

For a user request that diagnoses existing artifacts without running XiaoYi:

- apply the embedded workflow in `references/halo-loop.md` to a valid batch
  `handoff.json`, starting at its handoff-resolution phase;
- route an explicitly selected JSONL file or trace directory directly to
  `halo-rlm-agent-driven`;
- never mutate HDC state for either path.

## Choose the dataset adapter

| Mode | Runner child | Distinguishing contract |
| --- | --- | --- |
| `FILE_ORGANIZATION` | `xiaoyi-auto-continue` | `FileOrganization_*`; setup.json, expect.json, source/, prompt TXT; serial Agent-driven 1+3 confirmation loop |
| `WORKSPACE_BENCH` | `run-xiaoyi-loop` | non-negative numeric IDs; metadata.json with task and rubrics; one batch Runner start plus prepared evidence |

`Judge`, 评分, 打分, 诊断, and HALO are stage modifiers, never dataset-routing
signals.

## Choose the terminal stage independently

Resolve one stage before dispatch:

| Stage | User intent | Sequence |
| --- | --- | --- |
| `RUNNER_ONLY` | default; “运行/执行 XXX 任务，数据在……” or explicit “只运行/不要 Judge” | Runner only |
| `RUNNER_JUDGE` | explicit “并进行 Judge/评分/打分” | complete batch Runner → complete batch Judge |
| `RUNNER_JUDGE_DIAGNOSE` | explicit “并进行 Judge 和诊断”, HALO, or Trace diagnosis | complete batch Runner → Judge → diagnosis |

Do not infer Judge merely because metadata contains `rubrics`, because the user
provided a Judge-capable dataset, or because Runner produced Judgeable artifacts.
Do not infer diagnosis from ordinary words such as “查看结果” or “分析任务列表”.
Diagnosis requires Judge evidence, so an explicit diagnosis request implies the
Judge stage even when the user omits the word Judge. Explicit “只运行” always
stops after Runner. If one request contains both “只运行/不要 Judge” and an
explicit Judge or diagnosis instruction, treat the requested terminal stage as
contradictory and ask one question before HDC rather than guessing.

## Route in three passes

### 1. Choose a wording candidate

Choose `FILE_ORGANIZATION` when the user supplies any of:

- “文件整理任务”“文件整理 case”“整理文件任务”；
- “自动处理二次确认”“确认后继续”“最多继续三次”；
- `selected_cases.json` together with `test_file` and `prompts` paths.

Choose `WORKSPACE_BENCH` when the user supplies any of:

- `Task` or `task` with numeric selectors such as `112`, `14 25`, `1-10`, or
  `1..10`;
- `Workspace-Bench` or numeric Task data using metadata.json and rubrics;
- dataset folders named task, task1, or filestask with numeric case directories.

Treat standalone `task` as weak evidence. “Task FileOrganization_0_003” remains
file organization.

### 2. Confirm every selected ID

Load a supplied case-list JSON before routing and check every entry:

- `^FileOrganization_[0-9]+_[0-9]+$` confirms file organization;
- non-negative integers, numeric ranges, and comma-separated numeric selectors
  confirm WorkspaceBench;
- a list containing both shapes is mixed mode—stop before HDC and ask the user
  to separate the batches;
- arbitrary strings confirm neither mode, so continue to schema inspection.

Never reinterpret the numeric suffix of a FileOrganization ID as a Task ID.

### 3. Verify the scoped schema

Confirm file organization execution when every selected case resolves to:

```text
<test_file_base>/<case_id>/setup.json
<test_file_base>/<case_id>/expect.json
<test_file_base>/<case_id>/source/
<prompts_dir>/<case_id>.txt
```

`<test_file_base>/<case_id>/metadata.json` is a later Judge input. Its absence
does not change the Runner route or prevent execution; the batch Judge records a
case-level input error after the Runner batch finishes.

Confirm WorkspaceBench when every selected numeric Task resolves to metadata.json
with a non-empty `task` and non-empty string `rubrics`. Treat folder names only
as hints: `filestask` may still be WorkspaceBench.

Prefer concrete schema over generic wording. Use ID shape only when schema is
incomplete but not contradictory. Ask one routing question before HDC when ID
and schema contradict or two modes remain plausible.

## Runtime artifact policy

Resolve `<agent_workspace>` as the calling Agent's runtime workspace. Keep all
generated artifacts there by default, never under an installed Skill or dataset:

```text
<agent_workspace>/xiaoyi_file_runs/run_<YYYYMMDD>/ # FileOrganization Runner
<agent_workspace>/xiaoyi_logs/                     # WorkspaceBench Runner
<agent_workspace>/xiaoyi_judge/                    # shared Judge
<agent_workspace>/xiaoyi_halo/                     # HALO
```

Explicit user output paths win. Preserve one date-only FileOrganization `run_id`
formatted exactly as `YYYYMMDD` for the whole daily batch; never append a time or
uniqueness suffix.

## Dispatch file organization

1. Read and apply `xiaoyi-auto-continue` completely.
2. Pass the exact case list, dataset, prompts, config, and `<agent_workspace>`.
3. Let it finish each case's complete 1+3 loop serially and exhaust the host
   queue. Never call Runner batch mode.
4. Receive and verify `<run_dir>/runner_batch.json`, containing every selected
   case's terminal execution outcome, metadata path, and final outputs path.
5. For `RUNNER_ONLY`, return the handoff and stop. Do not read or invoke a Judge.
6. For `RUNNER_JUDGE`, read `judge-xiaoyi-results` completely and invoke its
   file-organization batch adapter once for the completed `runner_batch.json`.
   The Judge may run one case per subagent concurrently, but only after all
   selected Runner cases are terminal.
7. `RUNNER_JUDGE_DIAGNOSE` is not supported for FileOrganization while the
   diagnosis handoff accepts only numeric Task IDs. Detect this before HDC and
   ask whether the user wants Runner + Judge instead; never silently omit the
   requested diagnosis.

## Dispatch WorkspaceBench

1. Preserve exact directory-to-ID bindings and start Runner exactly once for the
   selected batch.
2. For `RUNNER_ONLY`, apply `run-xiaoyi-loop` in `runner-only` mode. Wait until
   every Task has a terminal Runner outcome, return raw Trace/output paths, and
   stop without Prepare or Judge.
3. For `RUNNER_JUDGE`, apply `run-xiaoyi-loop` in
   `runner-and-prepare-only` mode. Wait until every selected Task is prepared or
   has a recorded Runner/prepare failure, then apply `judge-xiaoyi-results` once
   to the whole WorkspaceBench batch.
4. For `RUNNER_JUDGE_DIAGNOSE`, read `references/halo-loop.md` completely and
   apply its explicit Runner → Judge → diagnosis → batch-report workflow. Never
   duplicate Runner or Judge. Its per-Task diagnosis workers apply
   `halo-rlm-agent-driven`; they never call back into this control workflow.

## Judge-to-HALO batch orchestration

Load `references/halo-loop.md` only for `RUNNER_JUDGE_DIAGNOSE` or an existing
batch handoff. Resolve `<skill_root>/scripts/halo-loop` exactly as the reference
specifies. That workflow owns batch handoff creation/resolution, one fresh HALO
worker per eligible numeric Task, failure isolation, and final HTML rendering.
Actual Trace investigation, JSON report generation, and manifest-bound report
validation remain exclusively owned by `halo-rlm-agent-driven`.

## Judge existing artifacts only

When the user explicitly asks only to Judge existing artifacts, skip both
Runners. Route by the supplied IDs and artifact schema, then invoke only the
matching `judge-xiaoyi-results` adapter.

## Examples

| Request | Route | Sequence |
| --- | --- | --- |
| “执行文件整理任务，列表在 selected_cases.json” | file organization | serial 1+3 Runner only |
| “执行文件整理任务，并进行 Judge” | file organization | serial 1+3 Runner for all cases → parallel batch Judge |
| “执行 Task 112、117 并完成 Judge” | WorkspaceBench | one batch Runner/prepare → parallel Agent Judge |
| “执行 Task 112、117，数据在 D:\\tasks” | WorkspaceBench | one batch Runner only; no Prepare/Judge |
| “执行 filestask 下的 39” with numeric metadata | WorkspaceBench | folder name does not override numeric ID/schema |
| “执行 Task FileOrganization_0_003” | file organization | standalone Task is weak; FileOrganization ID wins |
| “执行 Task 112，Judge 后做 HALO 诊断” | WorkspaceBench HALO | run → Judge → HALO |
| “诊断 D:\\logs\\task112.jsonl，不要运行小艺” | `halo-rlm-agent-driven` | trace-only diagnosis; no HDC/Runner/Judge |

Before dispatch, state the selected mode and decisive signals in one sentence.
After completion, report only stages that actually ran. Never imply Judge or
diagnosis ran merely because their input artifacts are available.
