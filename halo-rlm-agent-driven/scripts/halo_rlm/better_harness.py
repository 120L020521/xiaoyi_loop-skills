"""Unified Better Harness prompt construction for every HALO diagnosis."""

from __future__ import annotations

import json

from .report_contract import REPORT_STRUCTURE_GUIDANCE, render_report_example

BETTER_HARNESS_COMPONENTS = (
    "tool_definition",
    "tool_impl",
    "new_tool",
    "tool_merge",
    "tool_split",
    "middleware_in_tool",
    "prompt",
)
DEFAULT_EDITABLE_SURFACES = ("runner_skill.md", "workspace_bench_tools.ts")
MISSING_CONTEXT = "MISSING (not supplied; trace-only context)"


def _resolve_task_id(task: dict) -> str:
    value = task.get("task_id") or task.get("id") or task.get("taskId")
    if value is None or value == "":
        return MISSING_CONTEXT
    text = str(value)
    return f"task{text}" if text.isdigit() else text


def _format_rubrics(judge_result: dict) -> str:
    rubrics = judge_result.get("rubrics") or []
    if not rubrics:
        return ""

    lines = ["", "## Rubric details", ""]
    for rubric in rubrics:
        passed = rubric.get("passed")
        passed_text = "PASS" if passed is True else "FAIL" if passed is False else "?"
        lines.append(
            f"- [{rubric.get('index', '?')}] {passed_text}: "
            f"{rubric.get('rubric', '')}"
        )
        evidence = rubric.get("evidence", "")
        if evidence:
            lines.append(f"    evidence: {evidence}")
        lines.append("")
    return "\n".join(lines)


def build_halo_prompt(
    task: dict,
    judge_result: dict,
    surface_filenames: list[str],
    additional_request: str | None = None,
) -> str:
    """Build the unified HALO prompt; task and judge context may be absent."""
    task_id = _resolve_task_id(task)
    description = task.get("task") or task.get("description") or MISSING_CONTEXT
    output_files = task.get("output_files") or task.get("expected_output_files")
    output_text = (
        json.dumps(output_files, ensure_ascii=False)
        if output_files is not None
        else MISSING_CONTEXT
    )
    surface_filenames = surface_filenames or list(DEFAULT_EDITABLE_SURFACES)
    surfaces = ", ".join(surface_filenames)

    def judge_value(field: str) -> object:
        value = judge_result.get(field)
        return MISSING_CONTEXT if value is None or value == "" else value

    parts = [
        "Diagnose the runner from its OTel trace. Cite trace_id, span_id, operation,",
        "arguments, result/error, timestamp, and occurrence count when material.",
        "",
        "## Context",
        f"task_id: {task_id}",
        f"task: {description}",
        f"expected_output_files: {output_text}",
        f"judge.passed: {judge_value('passed')}",
        f"judge.score: {judge_value('score')}",
        f"judge.feedback: {judge_value('feedback')}",
        _format_rubrics(judge_result),
        f"editable_surfaces: {surfaces}",
        "",
        "## Constraints",
        "- The runner cannot read evaluator-only criteria, metadata, Judge data, or rubrics.",
        "  Never propose rubric access/auditing or runner-core/unlisted-surface changes.",
        "- Treat supplied task, judge, and rubric data as evaluator context only; ground",
        "  behavior claims in spans and do not infer a cause from a score or missing context.",
        "- Copy task_id and task unchanged into report_summary, including the explicit",
        "  MISSING value when unavailable. Copy expected_output_files unchanged when",
        "  supplied and omit it only when its Context value is MISSING.",
        "- Without task/Judge context, report execution evidence only, not external correctness.",
        "- Prefer trace-proven tool changes; use prompt changes only when tools cannot fix it.",
        "",
        "## Diagnostic order",
        "1. Tool failures (STATUS_CODE_ERROR or tool.is_error=true).",
        "2. Wrong/incapable tool selection.",
        "3. Malformed, missing, wrong-path/type, or redundant arguments.",
        "4. Missing tools causing multi-step shell/manual workarounds.",
        "5. Redundant tool combinations that could be merged.",
        "6. Noisy, sparse, or inconsistent return values.",
        "7. TRAJECTORY EFFICIENCY: repeated/similar calls, no-information-gain exploration,",
        "   direction changes, ineffective retries, late stopping, and safe early termination.",
        "   Distinguish necessary verification from redundancy; outcome alone proves neither.",
        "8. Prompt issues, only after ruling out tool-level causes.",
        "Group each distinct material problem into one error. Report its frequency,",
        "root cause, impact, recovery status, and the shortest complete evidence chain.",
        "Use normally 1-3 evidence items and never more than 5, ordered as triggering",
        "input/operation, decisive failure, then recovery or impact when separate spans",
        "are needed. Do not repeat equivalent facts or excerpts.",
        "Use report_summary.trace_ids as the report-level TRACE anchor. An individual error",
        "may be proved entirely by TASK, JUDGE, SOURCE_FILE, or OUTPUT_FILE evidence. When",
        "using TRACE evidence, call agent_cli source-evidence for the referenced span,",
        "then copy its zero-based span_index and decoded raw_log_excerpt unchanged.",
        "The excerpt must come from mapped pre-conversion source JSONL events, not",
        "converted span attributes. Every TRACE evidence item in an error finding must contain",
        "verbatim execution status or error output; an input/command-only excerpt is",
        "invalid. Include the triggering command/input, decisive output, failure status",
        "or exception, and immediate recovery/impact when they coexist in that",
        "span. Prefer the complete relevant input/output/status payload when it fits.",
        "Use at least 400 characters whenever the span contains that much source context,",
        "and include all available context when it is shorter; target 5-20 readable lines",
        "or 400-3,000 characters and never exceed 5,000. For oversized single-line",
        "JSON, pass a specific failure regex to source-evidence --pattern and copy",
        "its returned contiguous source window. Do not pad, splice fragments,",
        "paraphrase, repeat an equivalent excerpt, or include unrelated noise.",
        "Do not duplicate the same failed calls in both a generic tool-failure error",
        "and a second semantic or validation error. Briefly summarize the dominant root",
        "cause in primary_failure_mode.",
        "",
        "## Outcome and changes",
        "- Identify the root AGENT span. Classify as FAILED,",
        "  SUCCEEDED_WITH_RECOVERED_ERRORS, SUCCEEDED_WITH_UNPROVEN_RECOVERY,",
        "  SUCCEEDED_CLEANLY, or UNKNOWN.",
        "- Recovery requires a later success/verification for the same operation with",
        "  compatible arguments; unrelated OK spans do not prove recovery.",
        "- For FAILED, propose exactly 3-5 surgical changes. For every other",
        "  execution classification, propose 0-5 and use [] when no trace-supported",
        "  change is warranted. Allowed components: "
        + ", ".join(BETTER_HARNESS_COMPONENTS)
        + ".",
        "- Every error and change needs one P0-P4 relative priority. Evidence has no",
        "  priority. Every change also needs one allowed component, one editable target,",
        "  error_refs, and concrete acceptance_criteria.",
        "- One error may support multiple changes when they are distinct implementation",
        "  directions. For mutually exclusive alternatives, state applicability conditions",
        "  instead of inventing probability percentages.",
        "- Feed trace-supported trajectory inefficiencies into `proposed_changes` only when",
        "  material and actionable; quantify expected call/retry/turn/time reduction when",
        "  supported. Do not force an efficiency proposal without evidence.",
        "- Apply this fixed priority policy to errors and changes:",
        "  P0 = missing/materially wrong core output or false-success decision;",
        "  P1 = reliable execution/recovery/validation blocker, important required",
        "  constraint violation, or major correctness risk; P2 = material call/retry/time/",
        "  context waste or recurring stability issue with preserved result; P3 = limited",
        "  robustness/maintainability issue; P4 = optional polish or low-benefit change.",
        "  Rank by trace-supported impact and urgency, not category or error count.",
        "  Combine errors in one change only when one implementation at one layer truly",
        "  resolves all of them; otherwise split the changes. Use a concise",
        "  UPPER_SNAKE_CASE category for each error.",
        "- Never invent errors or metadata to fill a section.",
    ]
    if additional_request:
        parts.extend(["", "## Additional diagnostic request", additional_request])
    parts.extend(
        [
            "",
            "## MANDATORY machine-readable report contract",
            "Return exactly one valid UTF-8 JSON object, no fence or preamble. Validate it",
            f"before returning; targets must be one of: {surfaces}.",
            "",
            "Required shape:",
            render_report_example(
                BETTER_HARNESS_COMPONENTS,
                include_evaluator_context=True,
            ),
            REPORT_STRUCTURE_GUIDANCE,
        ]
    )
    return "\n".join(parts)
