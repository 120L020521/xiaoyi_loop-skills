---
name: run-xiaoyi-loop
description: >-
  Run specified XiaoYi Task datasets, including Workspace-Bench and custom datasets, through HDC, collect logs and outputs, then Judge each task with one isolated Codex subagent instead of an external Judge API. Use when users ask to run task IDs from workspace folders such as task, task1, filestask, or any folder whose name contains task; batch-Judge newly collected XiaoYi logs; Judge selected existing logs; or summarize XiaoYi task scores and failures. This workflow does not require a Judge API key.
---

# Run XiaoYi Loop with Agent Judge

Run XiaoYi tasks, prepare their evidence locally, and use Codex subagents as the Judge. Treat
the project configuration and generated artifacts as the source of truth.

## Non-negotiable rules

- Never call the project's external Judge API. Do not run `standalone_judge judge`, and do not
  bypass the bundled `scripts/run_tasks.py` launcher.
- Do not require, request, print, or validate a Judge API key. The calling Codex agent and its
  subagents perform the evaluation.
- Assign exactly one Task ID to each Judge subagent. Never ask one subagent to Judge multiple
  tasks or summarize the batch.
- Judge every current-batch Task with a collected canonical raw Trace, regardless of whether
  Runner status is completed, timeout, or failed. Never let old unrelated logs enter the batch.
- Start Runner exactly once for the requested batch. Wait on that same process for each Task's
  configured timeout; never relaunch Runner automatically after silence, output truncation, or
  an unexpected process exit.
- Preserve every directory-to-ID pair stated by the user. A request such as
  `A\\task 下的 112 和 B\\filetask 下的 39` is already fully disambiguated: build the exact
  Task paths `A\\task\\112` and `B\\filetask\\39`, then pass both in one Runner invocation.
  Never omit those IDs, run a partial/probe batch, or ask the user for selectors they already
  supplied.
- Keep runtime artifacts in the Agent workspace by default: `<agent_workspace>/xiaoyi_logs`,
  `<agent_workspace>/xiaoyi_judge`, and `<agent_workspace>/pipeline_state.json`. Never treat
  `<skill_root>` as their default destination.
- Treat `<skill_root>` as read-only. Never create `.venv`, `config/local.toml`, caches, logs, or
  other machine-local files inside the installed Skill.
- Store prepared evidence and the Agent Judge result together under `<run_dir>/task<ID>/`.
  Write the result as `<run_dir>/task<ID>/judge_result.json` and the current batch summary as
  `<run_dir>/batch_summary.json`. Do not overwrite API-backed profile results under
  `<run_dir>/results/<profile>/`.
- Use scripts and templates from this Skill directory. Do not depend on a separate XiaoYi Loop
  repository; only configured task data, HDC, runtime directories, and artifact-inspection
  skills may live outside it.

## Resolve inputs

1. Accept any non-negative integer ID, multiple IDs, `1-10`, `1..10`, and comma-separated
   combinations. Do not impose a Workspace-Bench-specific minimum or maximum ID.
2. Resolve the Task location in this order:
   - a directory or `metadata.json` explicitly supplied by the user;
   - a user-named dataset root such as `<workspace>/filestask`, passed with
     `--task-dir`, plus the requested ID;
   - the Agent's current workspace: `metadata.json`, `<ID>/metadata.json`, or any
     immediate child directory whose name contains `task` case-insensitively, with
     either `<dataset>/metadata.json` or `<dataset>/<ID>/metadata.json`;
   - `paths.tasks_root` only when the user previously configured it deliberately.
3. Never write a Task-data path into Skill source or assume the Skill directory contains the
   user's tasks. If the user names a dataset path, preserve that scope instead of searching a
   different dataset. If no candidate exists, or the requested ID exists in multiple datasets,
   ask the user for the Task dataset directory before running HDC.
4. Treat a directory containing `metadata.json` as one Task. Read its `absolute_id` for the
   Task ID and its non-empty `task` field for the HDC query. Require a non-empty string list in
   `rubrics` before HDC starts. Preserve the existing operational query suffix used by the
   Runner.
5. Ask for a selector only when several discovered tasks need disambiguation. Dataset folders
   may reuse integer IDs; never choose between duplicate IDs without an explicit dataset root.
   An explicit request to Judge every existing canonical Trace needs no selector.

### Preserve directory-ID bindings

Parse user requests into ordered `(dataset_root, task_id)` pairs before constructing any
command. When two or more pairs use different dataset roots, prefer exact Task paths as
positional arguments:

```powershell
& <python> -B "<skill_root>\scripts\run_tasks.py" `
  "D:\SKILL\0810\task\112" `
  "D:\SKILL\0810\filetask\39" `
  --workspace "<agent_workspace>"
```

This is one batch and one Runner start. Do not first invoke Runner with only `--task-dir`
values and then retry with IDs. Use `--task-dir <dataset_root>` only together with positional
ID selectors. Repeated `--task-dir` values that each point directly to one Task directory
containing `metadata.json` are also complete and require no selectors.

Treat an absolute path supplied by the user as a path on the host where `run_tasks.py` will
execute. Preserve it verbatim; do not reinterpret it relative to a different machine's current
workspace. Resolve the entire requested batch before HDC starts. If any exact Task path is
missing or invalid, stop the whole batch before submission instead of running the resolvable
subset.

## Prepare the environment

1. Set `<skill_root>` to the directory containing this `SKILL.md`. Resolve every bundled path
   from it; never infer paths from the shell's current directory.
2. Prefer an available Python 3.10+ environment. The scripts use the standard library except
   that Python 3.10 needs `tomli`. If a separate environment is necessary, create
   `<agent_workspace>/.xiaoyi-loop/.venv` and install
   `-r <skill_root>/scripts/requirements.txt` there. Never install into `<skill_root>`. Invoke
   bundled Python scripts with `-B` so they do not create `__pycache__` under the Skill.
3. Configuration is optional. The launchers automatically discover
   `<agent_workspace>/.xiaoyi-loop/local.toml`. Create it from
   `<skill_root>/config/local.example.toml` only when HDC or runner defaults need changing;
   never create it under `<skill_root>`. `paths.tasks_root` is optional because Task discovery
   is workspace-relative. Resolve relative runtime paths against `<agent_workspace>`.
4. Do not require Judge profile or API-key configuration. Validate the configured paths
   directly. For runner work, execute the configured equivalent of `hdc list targets`; skip
   HDC checks for existing-log diagnosis.

## Handle failures

- Report the stage, Task ID, exact path or field, and the next user action. Do not return only
  an exit code or raw traceback.
- For a missing or ambiguous Task, stop before HDC, list useful candidates when available, and
  ask the user for the Task directory or ID.
- For invalid metadata, missing/empty `task` or `rubrics`, invalid `data_manifest`, or a
  declared source file that is absent, stop before HDC or prepare and ask the user to correct
  the named metadata/file. Never guess rubrics or substitute another Task's data.
- Treat an absent/empty `data/` with no `data_manifest` declaration as a warning. Read `task`
  and `rubrics`; ask for data only if they require source files, otherwise continue.
- For missing HDC, no/multiple devices, or an invalid target, stop runner work and show the
  relevant `device.hdc`/`device.target` setting plus the `hdc list targets` check.
- Store every Runner outcome under `<logs_dir>/task<ID>/`. At timeout or another runtime failure,
  preserve any available raw Trace as `task<ID>.jsonl`, record the actual status in
  `task<ID>.meta.json`, and include the Task in Judge. If no Trace is collected, keep only failure
  metadata and mark it `runner failed / not judged`.
- Do not Judge a Task with a missing/invalid raw Trace or failed prepare. Do not fall back to stale
  prepared data or an old result. Record a Judge subagent failure as `status = "error"`.
- Finish partial batches with a table containing every selected Task and its failure stage,
  reason, recovery action, and artifact path.

## Collect the batch

For a new run, invoke only the runner phase:

```powershell
& <python> -B "<skill_root>\scripts\run_tasks.py" <task-selectors> `
  --workspace "<agent_workspace>"
```

Omit `<task-selectors>` when the workspace contains exactly one Task. When the user supplied a
dataset directory, add `--task-dir "<user_dataset_path>"`; a single Task directory or its
`metadata.json` may also be passed directly. Before HDC starts, verify the script prints the
resolved `metadata.json` for every Task. The Runner reads `metadata.task`, sends it as the query,
waits for completion, and pulls the JSONL log plus declared outputs. Unless explicitly
overridden, `<logs_dir>` means `<agent_workspace>/xiaoyi_logs`, `<run_dir>` means
`<agent_workspace>/xiaoyi_judge`, and `<state_file>` means
`<agent_workspace>/pipeline_state.json` throughout this workflow. Add
`--config "<custom_config_path>"` only for an explicit non-default config location.

Treat XiaoYi execution as a long-running quiet process. The Runner checks `stop_reason`
internally at `poll_seconds` but prints only an initial status, log-discovery events, and one
heartbeat every five minutes. Do not add `--verbose` for a normal run. If the command yields a
running process/cell handle, keep waiting on that same handle; do not relaunch the Runner or
start repeated HDC/log/process diagnostics. Silence between heartbeats and a long runtime are
normal. Let the Runner handle a configured timeout automatically; investigate only after an
explicit HDC error, process exit, failed timeout-artifact collection, device disconnect, or user
request, and never interrupt a healthy run merely because it is slow.

Treat terminal output truncation as a display limitation, not process termination. Do not work
around it by redirecting output and rerunning the command. If the original handle is genuinely
lost, read `paths.state_file` once without querying HDC. A `runner-waiting` state means wait until
the recorded `currentTaskDeadlineAt`, then read the state once more; do not start another Runner.
If the state is `runner-done`, continue from `runner.completed` and the pulled artifacts. If it is
`runner-interrupted`, or remains unfinished after the deadline, report the state and current Task
to the user without automatically resubmitting it.

Let the original Runner process own the full lifecycle for each Task. On either `stop_reason=stop` or the
configured timeout, pull the discovered log and declared outputs into `task<ID>`, force-stop
XiaoYi, then wait `restart_delay_seconds` (default five seconds) before submitting the next
Task. Do not manually perform or reorder these steps while that process is active.

Read the resolved workspace `paths.state_file` after it exits. The Judge batch is exactly
`runner.completed` from this run. It includes IDs also listed in `runner.timedOut` or
`runner.failed` whenever their canonical raw Trace was collected. Classify only failed IDs
without `<logs_dir>/task<ID>/task<ID>.jsonl` as `runner failed / not judged`. Running selected
IDs authorizes replacement of only those IDs' old `<logs_dir>/task<ID>` directories.

For existing-log Judge work, do not run the pipeline. Select only requested canonical
`<logs_dir>/task<ID>/task<ID>.jsonl` files. For an explicit all-existing-logs request, discover
every `task<ID>` directory containing that canonical Trace, regardless of Runner meta status.

## Prepare evidence without an API

Run one prepare command for the batch:

```powershell
& <python> -B "<skill_root>\scripts\prepare_logs.py" `
  --workspace "<agent_workspace>" `
  --task-id <ID1> --task-id <ID2>
```

Repeat `--task-id` once per selected task. Add `--task-dir "<user_task_path>"` for an explicit
directory. Use `--all` only when the user explicitly requested every existing canonical Trace.
Stop before Judge if prepare failed for a task; never silently substitute an older prepared
directory.

Prepare must read `rubrics` from each resolved `metadata.json`, copy its sibling `data/`
directory when present, normalize the selected XiaoYi log, and copy outputs. Do not infer
rubrics or source data from a fixed task root.

For each prepared task, use `<run_dir>/task<ID>/case_manifest.json` as the batch identity and
input fingerprint. A prior `<run_dir>/task<ID>/judge_result.json` may be resumed only when its
status is `success` and its input fingerprint exactly matches the current manifest. The prepare
launcher preserves this file while refreshing the other files in the Task directory. Spawn a
new Judge subagent for changed inputs, missing/error results, or an explicit re-Judge request.

## Dispatch one Judge subagent per task

Create a queue of tasks needing evaluation. Spawn up to the available concurrency limit, wait
for completed agents, and then fill freed slots until the queue is empty. Use a fresh subagent
with minimal inherited context for each Task ID. Give it only the Skill root, Task ID, prepared
directory, result path, and the following contract:

1. Judge exactly one Task ID and do not inspect or modify another task's result.
2. Read `metadata.json`, `case_manifest.json`, `agent.json`, and
   `normalized_runner_log.jsonl`. Inspect every file in `output/`; inspect corresponding files
   in `data/` when a rubric requires source/output comparison.
3. Use the relevant artifact skill when available: spreadsheets for Excel/CSV, documents for
   DOCX, PDF for PDF, presentations for PPTX, and image inspection for images. Judge actual
   artifact contents, not filenames or trace claims alone.
4. Evaluate every rubric independently and in metadata order. Use only concrete evidence.
   A different output filename is not itself a failure unless the rubric explicitly requires
   that name. If evidence is insufficient, set `passed` to `false` and explain what is missing.
5. Do not browse the web or invoke any external Judge/model API.
6. Write only `<run_dir>/task<ID>/judge_result.json`, then return a compact status to the parent
   agent. Do not modify the prepared evidence beside it.

Write each result as UTF-8 JSON with this shape:

```json
{
  "version": 1,
  "taskId": "117",
  "status": "success",
  "judgeType": "codex-subagent",
  "inputFingerprint": {"algorithm": "sha256", "value": "...", "fileCount": 3},
  "rubrics": [
    {
      "index": 0,
      "rubric": "rubric text",
      "passed": true,
      "confidence": 0.95,
      "evidence": "specific artifact or trace evidence"
    }
  ],
  "summary": {"total": 1, "passed": 1, "failed": 0},
  "passed": true,
  "score": 1.0,
  "feedback": "1/1 rubrics passed."
}
```

Copy `inputFingerprint` exactly from `case_manifest.json`. Set `score = passed / total` and
top-level `passed = true` only when every rubric passes. On an unrecoverable Judge failure,
write `status = "error"` with an `error` message; do not fabricate rubric decisions.

## Validate and report the batch

After every subagent finishes, read its result rather than trusting its completion message.
Verify the Task ID and fingerprint, that rubric indexes and text match metadata, and that
summary counts and score are arithmetically correct. Treat invalid output as a Judge failure.

Write `<run_dir>/batch_summary.json` for only this batch. All default result paths must remain
under `<agent_workspace>`. Follow the existing batch-summary structure where practical, set
`profile` to `agent`, and mark each result as `judged` or `resumed`. Do not include unrelated
historical tasks.

Return a concise table:

| Task | Runner | Judge | Score | Rubrics | Agent action | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 117 | success | success | 0.8500 | 17/20 | judged or resumed | result path |

Include failures with their stage and error, then batch totals. State explicitly that no
external Judge API or project API key was used. Do not generate Excel unless requested.

## Completion rules

- Finish only after every selected task is classified as Judge success, Judge failure, prepare
  failure, or current runner failure.
- Preserve matching successful Agent Judge results as resumed unless re-Judge was requested.
- Never present stale output as the result of the current batch.
- Do not claim success from process exit codes or subagent messages alone; verify artifacts.
