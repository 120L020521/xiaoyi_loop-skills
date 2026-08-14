---
name: run-xiaoyi-loop
description: >-
  Run numeric XiaoYi Task datasets, including Workspace-Bench and compatible custom
  metadata/rubrics datasets, through HDC and collect logs and outputs. Stop after
  Runner by default; prepare frozen evidence and delegate to the shared
  judge-xiaoyi-results Skill only when Judge is explicitly requested. Support
  `runner-only` and `runner-and-prepare-only` child modes selected by run-xiaoyi,
  or explicit invocation for numeric Task IDs from task, task1, or filestask
  directories containing metadata.json. Generic Task requests should enter
  through run-xiaoyi so stages are not duplicated.
  Do not use for 文件整理任务, FileOrganization_* IDs, or datasets organized as
  setup.json/expect.json/source/prompts; use xiaoyi-auto-continue for those. The
  standalone word Task/task is insufficient: require numeric selectors or a
  metadata.json/rubrics schema. This workflow does not require a Judge API key.
---

# Run Numeric XiaoYi Tasks

Run XiaoYi tasks and collect current artifacts. Prepare evidence or delegate
evaluation only for an explicitly requested later stage. Treat project
configuration and generated artifacts as the source of truth.

## Non-negotiable rules

- Reject `FileOrganization_*` selectors and datasets whose case contract is
  `setup.json` + `expect.json` + `source/` + prompt TXT. Route them to
  `xiaoyi-auto-continue`; do not reinterpret their numeric suffix as a Task ID.
- Never call the project's external Judge API. Do not run `standalone_judge judge`, and do not
  bypass the bundled `scripts/run_tasks.py` launcher.
- Do not require, request, print, or validate a Judge API key. The calling Codex agent and its
  subagents perform the evaluation.
- When Judge is requested, let `judge-xiaoyi-results` assign exactly one Task ID
  to each Judge subagent. Never implement a second Judge loop in this Skill.
- When Prepare/Judge is requested, include every current-batch Task with a
  collected canonical raw Trace, regardless of whether Runner status is
  completed, timeout, or failed. Never let old unrelated logs enter the batch.
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

Read the resolved workspace `paths.state_file` after it exits. The current Runner
batch is exactly `runner.completed` from this run. It includes IDs also listed in
`runner.timedOut` or `runner.failed` whenever their canonical raw Trace was
collected. If a later Prepare/Judge stage was requested, use that set as the
Judgeable batch. Classify only failed IDs without
`<logs_dir>/task<ID>/task<ID>.jsonl` as `runner failed / not judged`. Running
selected IDs authorizes replacement of only those IDs' old
`<logs_dir>/task<ID>` directories.

For existing-log Judge work, do not run the pipeline. Select only requested canonical
`<logs_dir>/task<ID>/task<ID>.jsonl` files. For an explicit all-existing-logs request, discover
every `task<ID>` directory containing that canonical Trace, regardless of Runner meta status.

## Stop after Runner when requested

In `runner-only` mode—or a direct invocation with no explicit Judge intent—stop
after the single Runner process exits and every selected Task has a terminal
Runner outcome. Return `paths.state_file`, each Task's Runner status, canonical
`<logs_dir>/task<ID>/task<ID>.jsonl` when present, and collected output paths.
Do not run `prepare_logs.py`, create prepared Judge directories, read
`judge-xiaoyi-results`, or spawn Judge subagents.

## Prepare evidence for the shared Judge

Enter this section only in `runner-and-prepare-only` mode or when the user
explicitly requested Judge.

Run one prepare command for the batch:

```powershell
& <python> -B "<skill_root>\scripts\prepare_logs.py" `
  --workspace "<agent_workspace>" `
  --task-id <ID1> --task-id <ID2>
```

Repeat `--task-id` once per selected Task. Add `--task-dir "<user_task_path>"`
for an explicit directory. Use `--all` only when the user explicitly requested
every existing canonical Trace. Never silently substitute older prepared data.

Prepare must read rubrics from the resolved metadata, copy relevant `data/`,
normalize the current Trace, and copy outputs. Each successful Task produces:

```text
<run_dir>/task<ID>/metadata.json
<run_dir>/task<ID>/case_manifest.json
<run_dir>/task<ID>/agent.json
<run_dir>/task<ID>/normalized_runner_log.jsonl
<run_dir>/task<ID>/output/
```

## Delegate the Judge phase

Resolve `judge-xiaoyi-results` by exact Skill name and read its `SKILL.md`
completely. When the user explicitly requested Judge, apply its `workspacebench`
adapter to every successfully prepared current-batch Task. The shared Judge owns
subagent isolation, rubric evaluation, result validation, fingerprint resume
rules, scoring, and `batch_summary.json`; do not duplicate those instructions
here.

When a parent coordinator explicitly requests `runner-and-prepare-only`, stop
after every selected Task is prepared or has a recorded prepare/Runner failure.
Return exact prepared Task directories and current Trace paths without spawning
  Judge subagents. This mode exists so `run-xiaoyi` can orchestrate three
  distinct phases in order: run, Judge, then HALO. For a direct invocation, use
Runner-only unless the user explicitly requested Judge.

When Judge ran, return its concise result table with Runner status, Judge status,
score, rubrics, action, and result path. Include prepare/Runner failures and
state that no external Judge API or project API key was used. In `runner-only`
mode, return only Runner status and raw artifact paths. Do not generate Excel
unless requested.

## Completion rules

- In `runner-only` mode, finish after every selected Task has a terminal Runner
  outcome; do not Prepare or claim it was Judged.
- When Judge was explicitly requested, finish only after every selected Task is
  classified as Judge success, Judge failure, prepare failure, or current Runner
  failure.
- In `runner-and-prepare-only` mode, finish after every selected Task is prepared
  or has a recorded prepare/current Runner failure; do not claim it was Judged.
- Let the shared Judge preserve matching successful results as resumed unless
  re-Judge was requested.
- Never present stale output as the result of the current batch.
- Do not claim success from process exit codes or subagent messages alone; verify artifacts.
