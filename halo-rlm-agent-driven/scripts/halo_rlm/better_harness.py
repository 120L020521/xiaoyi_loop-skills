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
        "- When task and expected_output_files are supplied, copy both unchanged into",
        "  report_summary; omit either only when its Context value is MISSING.",
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
        "Report frequencies and span-level evidence for each material pattern.",
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
        "- Every change needs one allowed component, P0-P4 priority, and one editable target.",
        "- Feed trace-supported trajectory inefficiencies into `proposed_changes` only when",
        "  material and actionable; quantify expected call/retry/turn/time reduction when",
        "  supported. Do not force an efficiency proposal without evidence.",
        "- Use P0-P4 only as machine-readable relative ranking labels. Do not assign",
        "  predefined issue categories or severity meanings; let the model rank by evidence",
        "  and impact, consistently across findings and changes.",
        "- Never invent findings or metadata to fill a section.",
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
