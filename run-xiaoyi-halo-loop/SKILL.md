---
name: run-xiaoyi-halo-loop
description: >-
  Orchestrate one-click XiaoYi batch execution, isolated Agent Judge evaluation,
  and parallel per-Task HALO RLM diagnosis for every collected raw Trace, including
  Runner timeouts and failures, through the existing run-xiaoyi-loop and
  halo-rlm-agent-driven skills. Use when users ask to run and Judge multiple
  XiaoYi tasks and diagnose all or only failed current-batch tasks with HALO, or
  when they provide a batch handoff.json for HALO diagnosis.
---

# Run XiaoYi + Judge + HALO

Act only as a thin coordinator. Do not copy or reinterpret Runner, Judge,
trace-inspection, or report-generation logic from the sibling skills.

## Required components

- Use Python >= 3.10 and UTF-8.
- Resolve sibling skills from the parent project directory:
  `run-xiaoyi-loop` and `halo-rlm-agent-driven`.
- Read both sibling `SKILL.md` files completely before acting and follow their
  phase-specific rules.
- Use this skill's `scripts/handoff.py` as the only handoff parser and validator.

HALO does not parse `handoff.json` directly. This coordinator runs
`handoff.py resolve`, then passes each resolved Task's exact trace, Task JSON,
Judge result, and output root to `halo-rlm-agent-driven`. Omit `--surface` so
HALO uses its logical default target-name allowlist; no surface files or surface
directory are handoff inputs.

Treat the resolved logs and Judge roots as read-only. Pass the exact
`paths.trace_jsonl` for each Task; never pass a batch root or recursively scan
it. Write generated diagnosis artifacts only below `paths.halo_artifact_dir`.
Reuse index sidecars and never delete them. A new HALO report may overwrite
that Task's existing `paths.halo_artifact_dir/halo_report.json` without a prior
delete operation.

## Runtime and handoff contract

Use these sibling roots by default when the workspace has no XiaoYi configuration:

```text
<agent_workspace>/xiaoyi_logs
<agent_workspace>/xiaoyi_judge
<agent_workspace>/xiaoyi_halo
```

Use one schema-v3 handoff for the batch:

```json
{
  "schema_version": 3,
  "producer": "run-xiaoyi-halo-loop",
  "task_ids": [14, 15],
  "diagnose_mode": "all",
  "roots": {
    "logs": "D:/workspace/xiaoyi_logs",
    "judge_run": "D:/workspace/xiaoyi_judge",
    "halo_output": "D:/workspace/xiaoyi_halo"
  }
}
```

When `<agent_workspace>/.xiaoyi-loop/local.toml`, `XIAOYI_LOOP_CONFIG`, or an
explicit `--config` supplies `paths.logs_dir` and `paths.run_dir`, use those
resolved Runner paths instead. Explicit handoff root flags override configuration.
Keep only batch roots and Task IDs in the handoff. `handoff.py` owns strict schema
checks, fixed-layout path derivation, optional Judge status/fingerprint checks,
and build-prompt context extraction.

When available, `task` and `expected_output_files` come from
`judge_run/task<ID>/metadata.json`. Task and Judge context are optional;
`resolve` emits only valid, available paths under `build_prompt_inputs`.

## Workflow

### 1. Obtain current Judge results

- For Task selectors or Task directories, apply `run-xiaoyi-loop` exactly as
  written. Wait for its Runner and Agent Judge phases to finish. Never launch a
  duplicate Runner. Pass the deduplicated union of `runner.completed` and
  `runner.failed` Task IDs into the handoff. All use
  `xiaoyi_logs/task<ID>/task<ID>.jsonl`; Judge every Task whose current JSONL
  exists, including timeouts and failures.
- For a user-supplied `handoff.json`, skip Runner and Judge and start at step 3.
- Never invoke an external Judge API or request a Judge API key.

### 2. Create one batch handoff

After Judge finishes, run:

```powershell
& <python> "<skill_root>\scripts\handoff.py" create `
  --workspace "<agent_workspace>" `
  --diagnose-mode all `
  --task-id <ID1> --task-id <ID2>
```

Add `--config "<custom_config_path>"` only for an explicit non-default config.
`create` reuses `run-xiaoyi-loop` configuration resolution. Without configuration
it uses the workspace defaults; with configuration it uses `paths.logs_dir` and
`paths.run_dir`. Explicit `--logs-root` or `--judge-run-root` values take priority.
Unless `--halo-output-root` is supplied, derive `xiaoyi_halo` beside the resolved
Judge root and write its `handoff.json` there.
`all` is the default: diagnose every selected Task with a usable raw Trace. Use
`--diagnose-mode failed` only when the user explicitly requests only errors. It
includes Runner failures/timeouts, Judge `passed=false`, Judge execution errors,
missing/invalid Judge results, and fingerprint mismatches.

### 3. Resolve the handoff

```powershell
& <python> "<skill_root>\scripts\handoff.py" resolve `
  "<resolved_halo_output>\handoff.json"
```

Stop only for an invalid handoff or missing logs root. For each Task,
`<resolved_logs_root>/task<ID>/task<ID>.jsonl` is the only required diagnosis input.
Missing or invalid Task/Judge context must be omitted, not treated as a blocker.
If the trace is missing, record `trace_missing`, skip that Task, and continue.
Read optional `task<ID>.meta.json` only to preserve Runner completed/failed
status. An empty eligible set is a successful no-op.

### 4. Dispatch one HALO subagent per eligible Task

Create a queue and assign exactly one eligible Task to each fresh diagnosis
subagent. Run up to the available concurrency limit, wait for completions, and
fill freed slots until the queue is empty. Never ask one diagnosis subagent to
handle multiple Tasks.

Give each subagent only the HALO skill root, Task ID, `paths.trace_jsonl`,
`roots.halo_output`, and that Task's `build_prompt_inputs`. Require it to:

1. Apply `halo-rlm-agent-driven` only to its assigned raw trace. Never use
   `normalized_runner_log.jsonl` and never inspect another Task.
2. Write only `paths.halo_artifact_dir` (`task<ID>_halo`) and use only the paths
   returned by its `halo-prepared-manifest.json`.
3. Run `build-prompt` exactly once with only the keys present in
   `build_prompt_inputs`. Either optional context may be absent. Do not pass
   `--surface`; use HALO's default
   target-name allowlist.
4. Apply the HALO skill through completion, then return its manifest and report
   paths. Do not copy, invoke, or reinterpret HALO's internal report-validation
   procedure in this coordinator.
5. Do not delete or recursively search for index sidecars. Leave cache and
   prepared artifacts in place after validation.

If the HALO subagent fails, record that Task's HALO failure and
continue the queue. If nested investigator capacity is unavailable, let that
HALO worker use the HALO skill's bounded single-agent fallback. The parent must
not run `validate-report`; report acceptance belongs entirely to HALO. Let the
final renderer perform only current-batch freshness, manifest/source/report
binding, and HALO v9 structure checks.

### 5. Render the batch HTML report

After every eligible Task has either completed HALO or recorded a HALO failure,
run by default:

```powershell
& <python> "<skill_root>\scripts\handoff.py" summarize `
  "<resolved_halo_output>\handoff.json"
```

`summarize` requires each manifest and report to be newer than the current
handoff and verifies that the manifest source/report paths match the selected
trace. It then renders the fixed interactive format to:

```text
<resolved_halo_output>/batch_diagnosis_report.html
```

When this path already contains a report generated by this renderer, merge the
current batch into that HTML instead of starting a new report. Preserve Tasks
that are not in the current batch, append new Trace fingerprints, and replace
an existing Trace fingerprint with its latest result. Compute the identity as
Task ID plus the source Trace's SHA-256, so reused Task IDs do not overwrite
different runs and identical fixture traces from different Tasks stay distinct.
Accept and migrate the previous unversioned or version 1 HTML payload, then
write payload schema version 2. Refuse to overwrite an unrecognized HTML file and update a
recognized report through a temporary file plus atomic replacement.

Keep at most 500 Trace-identified Task records in the main HTML by default.
When this threshold is exceeded, move the oldest overflow records into
self-contained `batch_diagnosis_report.archive-<UTC>.html` files in the same
directory and link them from the main report. Override the limit with
`summarize --archive-threshold <N>`; use `0` only when the user explicitly asks
to disable archiving.

Use `assets/halo_diagnostic_report.template.html` as the single source of truth
for the fixed HTML format. It must retain the same visual structure as the
workspace reference `xiaoyi_halo/halo_diagnostic_report.html`: dark-blue header,
sticky chip/filter toolbar, collapsible left directory, centered white Task
sections, expandable error/change cards, and dark JSON log excerpts. Do not
replace it with an alternate `.shell`, `.hero`, `.bundle`, or dashboard-card
layout.

On desktop, keep the centered report stream at the same target width whether
the left directory is expanded or collapsed. Let the directory consume extra
space on the left; never squeeze or widen the report stream. Center the
unchanged stream after collapse. On narrow screens, stack the directory above
the report and shrink the stream responsively. Support search and
Task/Judge/classification/error-category/change-priority/change-component/
recovery filters. Display error categories in Chinese, merge identical changes,
combine their `error_refs`, sort findings and changes from P0 to P4, and link to
available Judge, Trace, and HALO JSON files.

Render the Task body as an ordered stream of improvement items instead of two
separate error and suggestion lists. For each merged proposed change, resolve
all referenced `error_refs` and present one continuous card in this order:
关联错误（one or more complete problem summaries）→ 修改建议（target,
implementation, acceptance criteria, expected impact）→ 证据链（collapsed by
default）. Repeat an error in separate improvement cards only when multiple
genuinely distinct changes reference it; group multiple errors when one change
resolves them together. Preserve an unreferenced error as its own improvement
item and explicitly state that it has no linked suggestion rather than
inventing one.

Display every TRACE evidence item's `raw_log_excerpt` from the mapped
pre-conversion source JSONL events under that evidence card. Parse a JSON
object/array directly and parse multi-event JSONL line by line, then render each
event as two-space-indented JSON in `<pre class="json">`. If the excerpt is
plain text, wrap the unchanged readable value in a JSON object under
`raw_log_excerpt` so the HTML still presents valid formatted JSON. This is only
a display transformation: never alter the stored HALO report value. Preserve
enough context to show the operation, relevant input/result, execution status,
and exact failure. Convert escaped `\\r\\n`, `\\n`, and `\\t` sequences to
readable whitespace only in the displayed JSON string.
When one Task fails or lacks a trace, render the remaining valid Tasks and
record the failed or skipped Task in the same HTML.

Do not write a coordinator-level `batch_summary.json`. The Judge-owned
`<resolved_judge_root>/batch_summary.json` remains an upstream Judge artifact
and is not replaced by this step. Use `--output <path>.html` only when the user
requests a custom report path.

Return the Task/Judge/HALO statuses and report paths to the user. Finish when
every selected Task has a completed HALO report, a recorded HALO failure, a
missing-trace skip, or an explicit mode-filter skip, and the batch HTML report
has been written.
