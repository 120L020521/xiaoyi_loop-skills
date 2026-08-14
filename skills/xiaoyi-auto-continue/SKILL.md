---
name: xiaoyi-auto-continue
description: >-
  Run one or more HarmonyOS XiaoYi file-organization cases named like
  FileOrganization_0_001 from a test_file dataset, then intelligently continue
  the same dialog when XiaoYi stops for confirmation, scope selection, or another
  blocking question. Use as the file-organization execution child selected by
  run-xiaoyi, or when the user explicitly invokes this Skill with
  FileOrganization_* IDs and test_file/setup.json/expect.json/source/prompts.
  Automatic confirmation is part of this Runner workflow. Drive at most one
  initial push plus three continue pushes per case, finish the whole selected
  batch serially, and return final artifact paths for a later batch Judge. Do
  not own result scoring and do not use for numeric WorkspaceBench IDs or HALO.
---

# XiaoYi Agent-Driven Auto-Continue

Drive `run_test.py` as the mechanical executor. After every
`stop_reason=stop`, personally decide from XiaoYi's latest answer whether the
original file-organization task is actually complete. When it is not complete,
write the affirmative reply that best unblocks the task and resume the same
dialog. Finish every selected case before handing the complete batch to a
separate Judge workflow.

Do not run `workflow_auto_continue.py` or `judge_completion.py`. Do not call an
external LLM/Judge API. The host agent owns completion judgment and continue-query
generation.

## Fixed division of labor

| Responsibility | Owner |
| --- | --- |
| Clean remote Desktop/Download/Documents | `run_test.py` |
| Read `setup.json`, send `source/`, and unzip ZIP inputs | `run_test.py` |
| Push the prompt and monitor the JSONL log | `run_test.py` |
| Detect the current round's main-agent `stop_reason=stop` | `run_test.py` |
| Pull JSONL, outputs, and the latest `content.txt` | `run_test.py` |
| Save `dialog_page_id` and resume the historical dialog | `run_test.py` |
| Decide whether the original task is truly complete | host agent |
| Generate the next affirmative query | host agent |

Never issue HDC commands directly and never edit the bundled Python scripts while
executing a case. A successful process exit or `completed.json` means only that a
round reached a stop event; it does not prove business completion.

## Resolve inputs

Set `<skill_root>` to the directory containing this `SKILL.md`. Run the bundled
`<skill_root>/run_test.py`; resolve its `scripts/` relative to `<skill_root>`.

Resolve these paths before starting:

- `agent_workspace`: the calling Agent's runtime workspace before entering
  `<skill_root>`.
- `test_file_base`: directory containing case folders such as
  `FileOrganization_0_001/`.
- `prompts_dir`: directory containing `<case_id>.txt`.
- `output_base`: an explicit user path, otherwise
  `<agent_workspace>/xiaoyi_file_runs`.
- `config_path`: `<skill_root>/config.json`, unless the user explicitly supplies
  another config.

Explicit user paths always win. Pass `output_base` with `run_test.py
--output-base`; do not write runtime artifacts under `<skill_root>`, the dataset,
or the project-configured `test_runs` default. Preserve other config fields and
do not expose, print, or copy any configured credential.

For every selected case, require:

```text
<test_file_base>/<case_id>/setup.json
<test_file_base>/<case_id>/expect.json
<test_file_base>/<case_id>/source/
<prompts_dir>/<case_id>.txt
```

`source/` may be empty only when `setup.json.file_send` is empty. Read the original
prompt before the first push and retain it as the task contract for all later
decisions. Treat `expect.json.result` and pulled outputs as supporting evidence,
not as permission to broaden the task.

Use one stable unique `<run_id>` for every round and every case in the selected
batch, preferably `YYYYMMDD_HHMMSS`:

```text
<output_base> = <agent_workspace>/xiaoyi_file_runs
<run_dir> = <output_base>/run_<run_id>
<case_dir> = <run_dir>/<case_id>
<content> = <case_dir>/<case_id>.content.txt
<meta> = <case_dir>/<case_id>.meta.json
```

If `<case_dir>/completed.json` already exists before a requested fresh first run,
do not delete or overwrite historical evidence merely to bypass the runner's skip
logic. Use an explicitly selected new run date/output root, or report that the
case is already present and request direction when the intended run is ambiguous.

## Execute strictly serially

Use one physical device and one XiaoYi instance. Finish the entire 1+3 loop for
case A before cleaning, setting up, or starting case B. Never parallelize cases or
rounds.

Treat a user-supplied case-list JSON as a host-agent queue only. Read the JSON
array yourself, validate every case id, preserve its order, and invoke the runner
once per case with exactly one `--case` value.

**Hard prohibition:** never invoke `run_test.py -b`, `run_test.py --batch`, or
pass `--cases-list` to `run_test.py` in this workflow. Runner batch mode advances
to the next case as soon as the current case reaches its first stop, before the
host agent can judge `content.txt` or send required confirmations. “Batch” in a
user request means the host agent loops over the queue; it never means Runner
batch mode.

Before starting case B, assert all three conditions:

1. case A's current `run_test.py` process has exited;
2. case A has a final outcome: `complete`,
   `incomplete-after-3-continues`, or `execution-error`;
3. no continue round remains pending for case A.

Treat `run_test.py` as a long-running, quiet process. Start each round once and
wait for that same process. Do not relaunch it because terminal output is quiet or
truncated.

## Round 0: first push

Run from `<skill_root>`:

```powershell
& <python> -B "<skill_root>\run_test.py" `
  --case "<case_id>" `
  --clean `
  --config "<config_path>" `
  --date "<run_id>" `
  --output-base "<output_base>"
```

This round must perform cleanup and setup. Do not add `--skip-setup` for a fresh
case. After the process returns, read the latest `<content>`, `<meta>`, the original
prompt, and—when useful—the pulled `outputs/` tree and `expect.json`.

If `<content>` is missing or empty, classify the round as a hard execution failure.
Report the script error/timeout and stop this case; do not spend continue rounds
without a stop answer. If the process exited nonzero but produced valid current
content and metadata, judge the content instead of using the exit code alone.

## Decide conversation completion semantically

Answer one question: **Has XiaoYi actually executed the full original task, with
no requested operation still waiting for confirmation or retry?**

### Complete

Classify as complete only when the answer describes actions already performed and
the full requested result, with no unresolved failure or blocking choice. Strong
signals include concrete past-tense outcomes such as “已移动到…”, “已删除…”,
“整理完成”, or a result summary consistent with the original prompt.

A courtesy closing after a concrete completed result is not a blocker. For example:

```text
文件已成功移动到 move_file。还有其他需要帮忙的吗？
```

This is complete. Do not send another query merely because it ends with a question.

### Not complete: confirmation or choice required

Classify as incomplete when XiaoYi has only inspected, planned, previewed, or asked
for approval/scope/strategy before executing. Typical signals include:

- “请确认是否删除/移动/覆盖”；
- “需要你确认后我才能执行”；
- “请选择方案 1/2/3”“你倾向哪种”；
- “要不要我继续”“是否按这个方案处理”；
- “准备执行”“将会执行” without a later concrete result.

### Not complete: partial result or execution failure

Classify as incomplete when any required part remains undone, even if the answer
contains words such as “完成” or “最终结果”. Examples:

- some files succeeded while other required files failed;
- “无法完成”“暂时没法执行”“需要手动处理”；
- a tool/environment error prevented the move, deletion, rename, compression, or
  verification;
- XiaoYi claims completion but explicitly admits it substituted or omitted a
  requested operation—for example, it only moved files when the original prompt
  required creating an archive/打包；
- XiaoYi gives a plan or file list but performs no operation.

Interpret negation and scope. Never keyword-match the isolated word “完成”.

When signals overlap, label the round `needs-confirmation` if XiaoYi presents an
explicit confirmation gate or choice that an affirmative reply can immediately
unlock; include any reported execution problem in the reply. Otherwise label an
incomplete result `partial-or-failed`. Both labels enter the same continue loop.

### Supporting artifact check

Use `expect.json.result` and `<case_dir>/outputs/` only to resolve doubt:

- expected outputs present and semantically correct support completion;
- missing required outputs contradict a completion claim;
- an empty expected result does not prove a deletion task completed;
- do not require exact MD5 during the between-round decision unless the user asks
  for verification.

Content remains the primary signal for whether XiaoYi is waiting for a reply.

## Generate one affirmative continue query

When incomplete, write one short, direct Chinese query that authorizes the next
action and preserves the original task's scope. Prefer explicit verbs and objects.
Do not ask XiaoYi another question, introduce a new goal, weaken the expected result,
or accept a partial workaround.

Map the actual stop answer to the reply:

| XiaoYi's blocker | Continue-query pattern |
| --- | --- |
| asks whether to delete | `确认删除，请继续完成原任务。` |
| asks whether to move/copy/rename | `确认执行，请按原任务要求继续完成。` |
| offers equivalent implementation plans | `确认，按最推荐且能完整满足原任务的方案继续执行。` |
| asks for a scope that the original prompt already defines | restate that scope affirmatively, e.g. `确认删除桌面上所有符合原任务条件的文档文件，请继续执行。` |
| completed only part of the work | `请继续完成尚未完成的部分，并在全部执行完成后汇报结果。` |
| reports a transient tool/environment failure | `请继续重试并使用当前可用方式完成原任务，不要只提供方案。` |

Choose the most specific safe answer supported by the original prompt. For a
destructive task, repeat the authorized object/scope instead of sending a bare
“确认”. If several options materially change the user's requested scope and the
original prompt does not resolve them, stop and ask the user rather than inventing
authorization.

## Continue rounds 1-3

Read `dialog_page_id` from current `<meta>`. If it is missing, report a hard failure
and stop the case. Otherwise run exactly one continuation:

```powershell
& <python> -B "<skill_root>\run_test.py" `
  --continue "<dialog_page_id>" `
  --case "<case_id>" `
  --query "<affirmative_query>" `
  --config "<config_path>" `
  --date "<run_id>" `
  --output-base "<output_base>"
```

Do not clean or resend setup files during continue rounds. The runner snapshots all
JSONL logs before the push and accepts only a new stop after that baseline.

After every continue process returns:

1. Reopen the overwritten latest `<content>`; never reuse the previous round's text.
2. Reopen `<meta>` when another continuation may be needed.
3. Judge completion again against the unchanged original prompt.
4. Stop immediately when complete.
5. Otherwise generate a new query from the new answer and continue only while the
   continue-round count is below three.

The fixed budget is four pushes total: round 0 plus continue rounds 1, 2, and 3.
Do not call a fourth continuation. After round 3, classify the case as incomplete if
the original task is still not actually complete.

## Freeze the case for later batch Judge

After the conversation loop ends, keep the current case's latest clean
`<case_dir>/outputs/` snapshot. `run_test.py` replaces this directory on every
pull; never merge round outputs or use an older run directory.

Do not score or spawn Judge subagents here. Record this handoff entry in the
host Agent's batch state:

```json
{
  "caseId": "FileOrganization_0_001",
  "executionOutcome": "complete",
  "metadata": "<test_file_base>/FileOrganization_0_001/metadata.json",
  "outputs": "<run_dir>/FileOrganization_0_001/outputs"
}
```

The metadata path is a later Judge input and need not block Runner execution.
Preserve entries for incomplete and execution-error cases so the batch Judge can
report missing or invalid evidence consistently. Do not start batch Judge until
every selected case has reached a terminal execution outcome.

## Batch and failure behavior

For a case list, preserve the user's selection and sorted/requested order. Process
each case independently and continue after a completed, incomplete, or hard-failed
case unless the user requested stop-on-error. Wait about three seconds before the
next case so the device can settle.

Do not let one case's `content.txt`, `dialog_page_id`, prompt, or outputs influence
another case. Do not use a historical case artifact as evidence for the current run.

After the host queue is exhausted, return the ordered batch handoff containing
`run_dir`, `test_file_base`, every selected case ID, execution outcome, metadata
path, and outputs path. This is the only point at which a parent workflow may
start `judge-xiaoyi-results` for the batch.

Also write the same handoff to `<run_dir>/runner_batch.json` only after the last
selected case becomes terminal. Use this stable contract so Judge does not depend
on conversation memory:

```json
{
  "version": 1,
  "adapter": "file-organization",
  "runId": "20260814_153000",
  "runDir": "<absolute_run_dir>",
  "testFileBase": "<absolute_test_file_base>",
  "runnerFinished": true,
  "cases": [
    {
      "caseId": "FileOrganization_0_001",
      "executionOutcome": "complete",
      "metadata": "<absolute_metadata_path>",
      "outputs": "<absolute_outputs_path>"
    }
  ]
}
```

Preserve selected order, use absolute paths, include every selected case exactly
once, and write `runnerFinished: true` only after the queue is exhausted. Never
create a partial file with `runnerFinished: true`. A parent Judge must reject a
handoff whose case count/order differs from the requested selection or whose
outcome is outside the three terminal values below.

## Report

After every round, tell the user concisely:

- case ID and round number;
- the decisive content meaning or short excerpt;
- verdict: complete / needs confirmation / partial or failed;
- the exact continue query, when one was sent.

Finish with one row per case:

| Case | Execution outcome | Pushes | Continue queries | Snapshot | Artifact directory |
| --- | --- | ---: | --- | --- | --- |

Use `complete`, `incomplete-after-3-continues`, or `execution-error` as the final
execution outcome. State that result Judge has not run yet and return the batch
handoff to the caller. Never call a case complete solely from an exit code, stop
event, `completed.json`, or the isolated word “完成”.

Map round verdicts to final outcomes consistently:

- any round judged `complete` → `complete`;
- valid stop content remains incomplete after continue round 3 →
  `incomplete-after-3-continues`, including persistent XiaoYi tool failures;
- the mechanical flow cannot produce current content or the metadata needed to
  continue → `execution-error`.
