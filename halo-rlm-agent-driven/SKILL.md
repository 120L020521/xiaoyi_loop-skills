---
name: halo-rlm-agent-driven
description: >-
  Diagnose OTel/OpenTelemetry JSONL traces locally, with the host agent acting
  as HALOAgent: no external LLM API or extra API key. Includes event-log
  conversion, HALO-style trace tools, P0-P4 evidence ranking, path-efficiency
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
scratch files. Never delete source JSONL or prepared artifacts.

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

Return one UTF-8 JSON object with schema version 5 and these fields:

```text
report_summary
  task
  expected_output_files[]
diagnosis
  execution_classification
  task_and_output_files_assessment (conditional)
    expected_output_files[]
    actual_output_files[]
    impact
    evidence
  primary_failure_mode
  conclusion
  evidence_chain[]
  error_span_inventory[]
  failure_chronology[]
proposed_changes[]
```

When prompt Context supplies `task` and `expected_output_files`, copy both
unchanged into `report_summary`. Omit either field only when its Context value
is `MISSING`.

Use the exact v5 fields emitted by the generated prompt; do not add ad-hoc
fields. Every evidence item must include all fixed fields, using an empty string
for an unavailable scalar and `occurrence_count: 1` for one observation. Keep
`task_and_output_files_assessment` only when evidence shows missing, misplaced,
misnamed, corrupt, or materially incorrect output. When present, give it exactly
the four child fields shown above and place it immediately after
`execution_classification`, before `primary_failure_mode`; omit the whole object
when output is not a problem. `validate-report` normalizes this canonical order
for reports whose JSON keys arrive in another order. Aggregate the error
inventory and use empty arrays when no evidence supports a section.

Write human-facing values under `diagnosis` and `proposed_changes` in Simplified
Chinese. This includes conclusions, failure descriptions, recovery and impact
explanations, inventory categories and summaries, chronology events and
consequences, assessment explanations, and change titles/problems/
implementations/impacts. Keep JSON field names, classification enums, P0-P4,
trace/span ids, timestamps, component/target values, tool and operation names,
paths, filenames, and raw `arguments`/`result`/`error` evidence unchanged.

Rank findings and changes with P0-P4 only as relative ordering, without fixed
severity/category meanings. Never invent findings to fill a priority.

For `FAILED`, produce exactly 3-5 actionable changes. For every other execution
classification, allow 0-5 and use an empty array when no trace-supported change
is warranted. Each change uses one component: `tool_definition`, `tool_impl`,
`new_tool`, `tool_merge`, `tool_split`, `middleware_in_tool`, or `prompt`.
Target only an allowed surface. Prefer trace-proven tool changes. Include
material, actionable efficiency findings and quantify expected
call/retry/turn/time reduction when supported; never force a proposal without
evidence.

Return no banner, Markdown fence, or preamble. Write the JSON object to the
manifest's exact `report_path`, then validate and normalize it locally:

```bash
python -m halo_rlm.agent_cli validate-report REPORT_PATH \
  [--surface EDITABLE_FILE]...
```

Fix the report and rerun validation until it exits zero. This enforces schema
version 5, exact allowed fields at every level, required fields and types,
classification and P0-P4 enums, section nesting, conditional output-assessment
shape, required Chinese narrative values, allowed component/target values, and
classification-dependent change counts without a model/API call.

After all reports pass verification, delete only index sidecars created by the
current run. Resolve every deletion target first; keep all prepared traces,
manifests, prompts, reports, and unrelated files.
