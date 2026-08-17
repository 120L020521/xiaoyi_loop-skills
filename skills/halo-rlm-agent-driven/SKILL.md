---
name: halo-rlm-agent-driven
description: >-
  Diagnose OTel/OpenTelemetry JSONL traces locally, with the host agent acting
  as HALOAgent: no external LLM API or extra API key. Includes event-log
  conversion, HALO-style trace tools, P0-P4 error ranking, path-efficiency
  analysis, and UTF-8 JSON reports. Use for trace-only diagnosis, mixed trace
  directories, tool/LLM/subagent failures, Better Harness or Workspace-Bench
  failures, and harness optimization.
---

# halo-rlm-agent-driven

Act as the HALO root and use only `agent_cli` and `tool_cli`. Never request an
API key or invoke an external HALO/LLM engine; no model-engine CLI is bundled.
Treat prepared spans as evidence. Task, Judge, rubric, and trace-summary files
are optional evaluator context, never trace evidence or runner-visible input.

## Environment requirements

- Use Python >= 3.10. The bundled workflow uses only the Python standard
  library and requires no third-party package installation.
- Read and write all JSONL, JSON, prompt, manifest, and report files as UTF-8.
  On Windows, set `$env:PYTHONIOENCODING = "utf-8"` before running the Python
  CLIs when the active shell does not already use UTF-8.

## 1. Prepare traces

Run once before diagnosis:

```bash
python halo-rlm-agent-driven/scripts/prepare_trace.py INPUT --output-root OUTPUT_ROOT
```

`INPUT` may be one JSONL file or a directory. Directory names are opaque; do
not infer roles from suffixes. The script:

- converts raw `event + payload` logs or copies HALO span JSONL;
- recognizes both `agent_start`/`agent_end` and
  `session_started`/`session_ended` lifecycle dialects;
- partitions interleaved main/child events by `session_id`, then separates
  repeated runs inside each session by lifecycle boundaries and `run_id`;
- keeps auxiliary lifecycle events as source evidence without creating
  metadata-only AGENT roots, and exposes `session.id`, `session.parent_id`, and
  `agent.run_id` on AGENT spans when present;
- treats raw `foo.jsonl` as authoritative over paired `foo.halo.jsonl`;
- mirrors task paths into `OUTPUT_ROOT`, suffixing the task leaf with `_halo`;
- maps a root `foo.jsonl` to `OUTPUT_ROOT/foo_halo/`;
- creates the converted trace and `halo-prepared-manifest.json`, reserving exact
  `prompt_path` and `report_path` locations without creating either file;
- reuses current conversions, excludes generated outputs from scans, and never
  overwrites source logs;
- reports a collision instead of letting multiple logical traces overwrite one
  task artifact directory.

Treat the supplied source JSONL, its parent directory, Task/Judge inputs, and
all unrelated files as read-only. When the caller or a handoff supplies an
exact JSONL path, pass that file directly; never replace it with an ancestor
directory or recursively enumerate that directory. Recursive discovery is
allowed only when the user explicitly selected a directory as `INPUT`.

One raw JSONL may therefore produce multiple trace ids in one prepared HALO
JSONL, for example one main AGENT trace plus one trace per embedded child run.
Treat the manifest entry as a dataset path, not as proof that it contains only
one execution. Discover its trace ids with `get_dataset_overview` and
`query_traces`; never infer main/child count from input filenames.

Example with explicitly supplied roots:

```text
INPUT_ROOT/task13/task13.jsonl -> OUTPUT_ROOT/task13_halo/
INPUT_ROOT/foo.jsonl           -> OUTPUT_ROOT/foo_halo/
```

For a file, use only the returned `trace_path`. For a directory, stop when
`errors` is non-empty; otherwise process each `prepared_traces` entry once and
use its exact `selected`, `prompt_path`, `report_path`, and `manifest_path`.
Never rescan or derive alternate converted paths. Use `--check` only for
detection; `--output-root` selects artifacts, while `-o/--output` is invalid.

Use the native host shell. On Windows use PowerShell paths, not WSL paths.
Create no other files. Keep tool results in context; remove unavoidable OS-temp
scratch files. Never delete or modify source JSONL, Task/Judge inputs, or
unrelated files. Do not use `--force` merely to refresh a diagnosis. Generated
HALO files may be created, reused, or refreshed only under `OUTPUT_ROOT`; an
existing diagnosis report at the manifest's exact `report_path` may be
overwritten by the new diagnosis without deleting it first.

## 2. Build the prompt locally

Before building anything, resolve the task context and resolve a Judge directory
to exactly one JSON object. Decide the editable target names at the same time.
These surface names are only the allowlist for `proposed_changes[].target`; the
helper does not read them as files or require a surfaces directory. Missing
context must remain `MISSING`; never synthesize it.

Then run exactly once for each prepared trace from
`halo-rlm-agent-driven/scripts`:

```bash
python -m halo_rlm.agent_cli build-prompt --output PROMPT_PATH \
  [--task-json TASK_JSON] [--judge-result JUDGE_JSON] \
  [--surface EDITABLE_TARGET_NAME]... [-p "ADDITIONAL REQUEST"]
```

Use the manifest's exact `prompt_path`. `prepare_trace.py` does not create a
default prompt; `build-prompt` creates or replaces the authoritative prompt only
after all available Task, Judge, and surface inputs are final. Never call it with
placeholder inputs, never hand-edit the generated file, and never rebuild it
during diagnosis. A file's existence does not inject it automatically.

Evaluator context may explain the target but cannot establish runner behavior
without spans. Reopen `PROMPT_PATH` after the single build and use it unchanged as
the diagnosis contract.

The helper only reads JSON and writes text; it makes no model/API call.

## 3. Diagnose with the host agent

Use `--surface` only to override the editable target-name allowlist. Otherwise
accept the logical defaults `runner_skill.md` and `workspace_bench_tools.ts`;
their physical files and a shared surfaces directory are not required. Do not
propose rubric access, runner-core edits, or unlisted targets.

Plan the investigation, delegate independent trace inspection when available,
reconcile evidence, and write the report yourself. Give each subagent a
self-contained question; it inherits no parent or sibling context. If
delegation is unavailable, use a bounded single-agent fallback and state that
recursion was not reproduced.

### Inspect evidence

Each investigator must:

1. Call `get_dataset_overview` first, without regex.
2. Use only trace/span ids returned by discovery or search.
3. Prefer indexed filters; narrow regex when `has_more=true`.
4. Treat a trace as small only when both `span_count <= 40` and
   `raw_jsonl_bytes <= 40_000`. Only then use `view_trace`. If either value is
   larger, use `search_trace`, then `view_spans` or `search_span`. Never retry
   an oversized full view.
5. Check OTel errors plus semantic markers such as `success=false`, timeout,
   validation, rate limits, max turns/steps, and budget exhaustion.
6. Cite trace/span ids, operations, arguments, results/errors, timestamps, and
   repeated counts. Never fabricate evidence or recovery.
7. Diagnose path efficiency: repeated/similar calls, no-information-gain work,
   direction changes, ineffective retries, late stopping, and safe early
   termination. Distinguish necessary verification from redundancy.

Tool CLI grammar (local and API-free):

```bash
python -m halo_rlm.tool_cli TRACE TOOL [NAMED_TOOL_FLAGS]
python -m halo_rlm.tool_cli TRACE get_dataset_overview
python -m halo_rlm.tool_cli TRACE view_trace --trace-id TRACE_ID
python -m halo_rlm.tool_cli TRACE view_spans --trace-id TRACE_ID --span-id SPAN_ID
python -m halo_rlm.tool_cli TRACE --list
```

Use named flags for ordinary arguments. Use `--args` only for one non-empty
JSON object containing structured filters; never combine it with named flags.
Read tool data from the top-level `result` field.

Read `references/trace-format.md` only for span shape/truncation and
`references/architecture.md` only for recursion, compaction, or termination.

## 4. Classify, write, and validate

Identify the root AGENT span first. Classify exactly one:

- `FAILED`: root error or explicit terminal failure.
- `SUCCEEDED_WITH_RECOVERED_ERRORS`: root success and every material error has
  later recovery for the same operation with compatible arguments.
- `SUCCEEDED_WITH_UNPROVEN_RECOVERY`: root success but material recovery is
  bypassed, tolerated, or unproven.
- `SUCCEEDED_CLEANLY`: root success without material failures.
- `UNKNOWN`: terminal evidence is missing, ambiguous, or conflicting.

Root success proves execution completion, not external correctness. An
unrelated OK span never proves recovery.

Return one UTF-8 JSON object with schema version 7 and these fields:

```text
report_summary
  task_id
  task
  trace_ids[]
  expected_output_files[]
  judge_summary
diagnosis
  execution_classification
  primary_failure_mode
  error_findings[]
    error_id
    priority
    category
    title
    occurrence_count
    summary
    evidence[]
      source
      reference
      tool
      fact
      raw_log_excerpt
      error
    root_cause
    recovery_status
    impact
proposed_changes[]
  priority
  component
  target
  title
  error_refs[]
  problem
  implementation
  acceptance_criteria[]
  expected_impact
```

Copy the resolved `task_id` and `task` unchanged from prompt Context, including its explicit
`MISSING` value when unavailable. Copy `expected_output_files` unchanged when
supplied and omit it when missing. Include `judge_summary` only when Judge
context exists.

Use the exact v7 fields emitted by the generated prompt; do not add ad-hoc
fields. Group each distinct material problem into one `error_findings` item. Do not
repeat the same failed spans in a generic tool-failure error and another
semantic or validation error. Write `primary_failure_mode` as a brief Chinese
summary of the dominant root cause rather than an id.

Use `source` values `TRACE`, `TASK`, `JUDGE`, `SOURCE_FILE`, or `OUTPUT_FILE`.
For `TRACE`, put the real span id in `reference`; for every other source, use the
rubric reference, path, filename, or source location needed to verify the fact.
Keep evidence compact: `fact` states what the source proves and `error` preserves
the raw error text or uses an empty string. Treat `report_summary.trace_ids` as
the report-level TRACE anchor. An individual error may be proved entirely by
`TASK`, `JUDGE`, `SOURCE_FILE`, or `OUTPUT_FILE` evidence; do not attach an
irrelevant TRACE merely to satisfy that error. When an error uses `TRACE`
evidence, its `raw_log_excerpt` must be a verbatim excerpt copied from the
referenced Span's serialized log content. Include enough
contiguous context for a reader to understand what operation ran, which input or
result mattered, and where it failed. Prefer the triggering command/call plus
the decisive output, status, or exception (typically 3-20 relevant lines when
available), rather than only the final exception line. Omit unrelated noise;
preserve original punctuation, identifiers, and line breaks, and do not
translate or paraphrase it. Use an empty string for
`raw_log_excerpt` on non-TRACE evidence. Evidence has no id and no priority.

Write human-facing error and change values in Simplified Chinese. This includes
`primary_failure_mode`, error titles, summaries, facts, root causes and impacts, plus change titles,
problems, implementations, acceptance criteria, and expected impacts. Keep JSON
field names, enums, P0-P4, task/trace/span ids, component/target values, tool
names, paths, filenames, and raw errors unchanged. Use concise
`UPPER_SNAKE_CASE` error categories and exactly one recovery status:
`RECOVERED`, `UNRECOVERED`, `UNPROVEN`, or `NOT_APPLICABLE`.

Rank errors and changes with the following fixed policy:

- `P0`: directly causes a missing or materially wrong core output, or can make
  the system falsely accept a failed task as successful.
- `P1`: blocks reliable execution, recovery, or validation; violates an
  important required constraint; or creates a major correctness risk without
  being the dominant core-output failure.
- `P2`: materially wastes calls, retries, time, or context, or creates a
  recurring stability problem while preserving the result.
- `P3`: limited robustness or maintainability issue with low current impact.
- `P4`: optional polish or low-benefit improvement.

Choose priority from trace-supported impact and urgency, not category names or
tool error counts. Rank the dominant root cause above secondary symptoms. For
example, wrong source columns that corrupt the main data are `P0`; a required
chart-orientation mismatch or a broken output-verification script is normally
`P1`; repeated unchanged reads are normally `P2`. Missing root terminal
evidence is `P1`, but becomes `P0` when downstream automation uses it to decide
success, retry, or billing. Every change must cite one or more existing
`error_refs` and give concrete, verifiable `acceptance_criteria`. A change may
combine multiple errors only when one implementation at one layer genuinely
resolves all of them; otherwise split the changes. Never invent errors or
changes to fill a priority.

One error may be referenced by multiple proposed changes when they represent
genuinely different modification directions. Use separate changes for
independent layers or mutually exclusive alternatives, and state the applicable
condition in `problem` or `implementation`. Do not assign unsupported numeric
probabilities such as `50%`; prefer deterministic conditions. Avoid duplicate
changes that differ only in wording.

For `FAILED`, produce exactly 3-5 actionable changes. For every other execution
classification, allow 0-5 and use an empty array when no trace-supported change
is warranted. Each change uses one component: `tool_definition`, `tool_impl`,
`new_tool`, `tool_merge`, `tool_split`, `middleware_in_tool`, or `prompt`.
Target only an allowed surface. Prefer trace-proven tool changes. Include
material, actionable efficiency findings and quantify expected
call/retry/turn/time reduction when supported; never force a proposal without
evidence.

Return no banner, Markdown fence, or preamble. Write the JSON object to the
manifest's exact `report_path`, replacing an older report at that path when
present, then validate and normalize it locally:

```bash
python -m halo_rlm.agent_cli validate-report REPORT_PATH \
  --manifest MANIFEST_PATH \
  [--surface EDITABLE_FILE]...
```

Use the manifest's exact `manifest_path`; do not derive another one. Fix the
report and rerun validation until it exits zero and returns
`"validation": "complete"`. This single HALO-owned acceptance step enforces:

- schema version 7, exact fields, types, nesting, enums, Chinese narratives,
  error/reference integrity, allowed component/target values, and
  classification-dependent change counts;
- one error-free prepared-trace manifest whose source, selected trace, prompt,
  report, and manifest paths exist and bind to the current artifact directory;
- a prepared trace that is not older than its source and a report that is not
  older than the authoritative prompt;
- report trace ids and every `TRACE` evidence reference that actually exist in
  the prepared trace; every `raw_log_excerpt` must occur verbatim in its
  referenced Span; all proposed changes must reference report error findings.

Omitting `--manifest` performs schema-only compatibility validation and is not
sufficient to finish a HALO diagnosis. The complete validator makes no
model/API call.

Keep index sidecars in place and reuse them. They are fingerprint-checked query
caches under the HALO output tree; the trace tools rebuild stale caches
automatically and may atomically refresh a stale sidecar. Never enumerate
directories to find them, delete them before or after diagnosis, or issue
per-sidecar cleanup commands.
