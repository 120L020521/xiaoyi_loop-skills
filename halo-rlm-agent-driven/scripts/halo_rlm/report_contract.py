"""Single source of truth for HALO diagnostic report JSON."""

from __future__ import annotations

import json
from typing import Any, Iterable

REPORT_SCHEMA_VERSION = 5
REQUIRED_TOP_LEVEL_FIELDS = ("schema_version", "report_summary", "diagnosis", "proposed_changes")
DIAGNOSIS_ARRAY_FIELDS = ("evidence_chain", "error_span_inventory", "failure_chronology")
EXECUTION_CLASSIFICATIONS = (
    "FAILED",
    "SUCCEEDED_WITH_RECOVERED_ERRORS",
    "SUCCEEDED_WITH_UNPROVEN_RECOVERY",
    "SUCCEEDED_CLEANLY",
    "UNKNOWN",
)
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
REPORT_SUMMARY_REQUIRED_FIELDS = ("title", "protocol", "trace_ids")
REPORT_SUMMARY_OPTIONAL_FIELDS = ("judge_summary", "task", "expected_output_files")
DIAGNOSIS_REQUIRED_FIELDS = (
    "execution_classification",
    "primary_failure_mode",
    "conclusion",
    *DIAGNOSIS_ARRAY_FIELDS,
)
DIAGNOSIS_OPTIONAL_FIELDS = ("task_and_output_files_assessment",)
DIAGNOSIS_CANONICAL_FIELDS = (
    "execution_classification",
    "task_and_output_files_assessment",
    "primary_failure_mode",
    "conclusion",
    *DIAGNOSIS_ARRAY_FIELDS,
)
EVIDENCE_FIELDS = (
    "priority",
    "trace_id",
    "span_id",
    "timestamp",
    "operation",
    "tool_name",
    "arguments",
    "result",
    "error",
    "recovery",
    "impact",
    "occurrence_count",
)
ERROR_INVENTORY_FIELDS = (
    "priority",
    "category",
    "tool_name",
    "occurrence_count",
    "error_summary",
    "sample_span_ids",
)
CHRONOLOGY_FIELDS = ("timestamp", "priority", "span_id", "event", "consequence")
OUTPUT_ASSESSMENT_FIELDS = (
    "expected_output_files",
    "actual_output_files",
    "impact",
    "evidence",
)
PROPOSED_CHANGE_FIELDS = (
    "component",
    "priority",
    "title",
    "problem",
    "implementation",
    "expected_impact",
    "target",
)
REPORT_STRUCTURE_GUIDANCE = (
    "Use exactly the shown fields and nesting; do not add ad-hoc fields. Keep evidence_chain, "
    "error_span_inventory, and failure_chronology inside diagnosis, and keep proposed_changes "
    "top-level. Write human-facing diagnosis and proposed_changes narratives in Simplified "
    "Chinese. Keep JSON keys, enums, priorities, ids, timestamps, component/target values, "
    "tool names, paths, filenames, and raw arguments/results/errors unchanged. Every evidence "
    "item must contain the fixed v5 fields; use an empty string for an unavailable scalar and "
    "occurrence_count=1 for a single observation. When evaluator "
    "context supplies task and expected_output_files, copy both unchanged into report_summary. "
    "The optional diagnosis.task_and_output_files_assessment object is allowed only when output "
    "files are missing, misplaced, misnamed, corrupt, or materially incorrect; when present it "
    "must contain exactly expected_output_files, actual_output_files, impact, and evidence, and "
    "it must appear immediately after execution_classification and before primary_failure_mode. "
    "Omit that object when no output problem is supported. Keep the error inventory "
    "aggregate-only, and use [] when a section has no evidence. FAILED reports require exactly "
    "3-5 proposed_changes; every other execution classification allows 0-5 and must use [] when "
    "no trace-supported change is warranted."
)


def build_report(*, report_summary: dict[str, Any],
                 proposed_changes: list[dict[str, Any]] | None = None,
                 **diagnosis: Any) -> dict[str, Any]:
    """Build a report with the canonical v5 nesting."""
    diagnosis = dict(diagnosis)
    for field in DIAGNOSIS_ARRAY_FIELDS:
        diagnosis.setdefault(field, [])
    ordered_diagnosis = {
        field: diagnosis[field]
        for field in DIAGNOSIS_CANONICAL_FIELDS
        if field in diagnosis
    }
    ordered_diagnosis.update(
        (field, value)
        for field, value in diagnosis.items()
        if field not in ordered_diagnosis
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_summary": report_summary,
        "diagnosis": ordered_diagnosis,
        "proposed_changes": proposed_changes or [],
    }


def render_report_example(components: Iterable[str], *,
                          include_evaluator_context: bool = False) -> str:
    """Render the compact schema example embedded in model prompts."""
    summary: dict[str, Any] = {
        "title": "HALO RLM DIAGNOSTIC REPORT",
        "protocol": "HALO RLM agent-driven",
        "trace_ids": ["..."],
    }
    if include_evaluator_context:
        summary.update({
            "task": "...",
            "expected_output_files": ["..."],
            "judge_summary": "...",
        })
    report = build_report(
        report_summary=summary,
        execution_classification="<classification>",
        primary_failure_mode="主要失败模式说明",
        conclusion="诊断结论说明",
        evidence_chain=[{
            "priority": "P0", "trace_id": "...", "span_id": "...",
            "timestamp": "...", "operation": "...", "tool_name": "...",
            "arguments": "...", "result": "...", "error": "...",
            "recovery": "恢复情况说明", "impact": "影响说明", "occurrence_count": 1,
        }],
        error_span_inventory=[{
            "priority": "P0", "category": "错误类别", "tool_name": "...",
            "occurrence_count": 1, "error_summary": "错误摘要", "sample_span_ids": ["..."],
        }],
        failure_chronology=[{"timestamp": "...", "priority": "P0", "span_id": "...",
                             "event": "事件说明", "consequence": "后果说明"}],
        proposed_changes=[{
            "component": "|".join(components), "priority": "P0", "title": "修改标题",
            "problem": "问题说明", "implementation": "实施方案", "expected_impact": "预期影响",
            "target": "...",
        }],
    )
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"))


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"model diagnostic report {path} must be a JSON object")
    return value


def _validate_keys(
    value: dict[str, Any],
    path: str,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set.difference(value))
    if missing:
        raise ValueError(
            f"model diagnostic report {path} is missing fields: {', '.join(missing)}"
        )
    unexpected = sorted(set(value).difference(allowed))
    if unexpected:
        raise ValueError(
            f"model diagnostic report {path} has unsupported fields: {', '.join(unexpected)}"
        )


def _require_string(value: Any, path: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        suffix = "a non-empty string" if not allow_empty else "a string"
        raise ValueError(f"model diagnostic report {path} must be {suffix}")


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def _require_chinese_text(
    value: Any,
    path: str,
    *,
    allow_empty: bool = False,
) -> None:
    _require_string(value, path, allow_empty=allow_empty)
    if value and not _contains_cjk(value):
        raise ValueError(
            f"model diagnostic report {path} must contain Simplified Chinese narrative text"
        )


def _require_string_array(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"model diagnostic report {path} must be an array of strings")


def _require_positive_int(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"model diagnostic report {path} must be an integer >= 1")


def _require_priority(value: Any, path: str) -> None:
    if value not in PRIORITIES:
        raise ValueError(
            f"model diagnostic report {path} must be one of: {', '.join(PRIORITIES)}"
        )


def _validate_report_summary(value: Any) -> None:
    summary = _require_object(value, "report_summary")
    _validate_keys(
        summary,
        "report_summary",
        required=REPORT_SUMMARY_REQUIRED_FIELDS,
        optional=REPORT_SUMMARY_OPTIONAL_FIELDS,
    )
    if summary["title"] != "HALO RLM DIAGNOSTIC REPORT":
        raise ValueError(
            "model diagnostic report report_summary.title must be "
            "'HALO RLM DIAGNOSTIC REPORT'"
        )
    if summary["protocol"] != "HALO RLM agent-driven":
        raise ValueError(
            "model diagnostic report report_summary.protocol must be "
            "'HALO RLM agent-driven'"
        )
    _require_string_array(summary["trace_ids"], "report_summary.trace_ids")
    if not summary["trace_ids"] or any(not item.strip() for item in summary["trace_ids"]):
        raise ValueError(
            "model diagnostic report report_summary.trace_ids must contain non-empty strings"
        )
    for field in ("judge_summary", "task"):
        if field in summary:
            _require_string(summary[field], f"report_summary.{field}", allow_empty=False)
    if "expected_output_files" in summary:
        _require_string_array(
            summary["expected_output_files"], "report_summary.expected_output_files"
        )


def _validate_evidence_item(value: Any, index: int) -> None:
    path = f"diagnosis.evidence_chain[{index}]"
    item = _require_object(value, path)
    _validate_keys(item, path, required=EVIDENCE_FIELDS)
    _require_priority(item["priority"], f"{path}.priority")
    for field in EVIDENCE_FIELDS:
        if field not in ("priority", "occurrence_count"):
            _require_string(item[field], f"{path}.{field}")
    _require_chinese_text(item["recovery"], f"{path}.recovery", allow_empty=True)
    _require_chinese_text(item["impact"], f"{path}.impact")
    _require_positive_int(item["occurrence_count"], f"{path}.occurrence_count")


def _validate_error_inventory_item(value: Any, index: int) -> None:
    path = f"diagnosis.error_span_inventory[{index}]"
    item = _require_object(value, path)
    _validate_keys(item, path, required=ERROR_INVENTORY_FIELDS)
    _require_priority(item["priority"], f"{path}.priority")
    for field in ("category", "tool_name", "error_summary"):
        _require_string(item[field], f"{path}.{field}")
    _require_chinese_text(item["category"], f"{path}.category")
    _require_chinese_text(item["error_summary"], f"{path}.error_summary")
    _require_positive_int(item["occurrence_count"], f"{path}.occurrence_count")
    _require_string_array(item["sample_span_ids"], f"{path}.sample_span_ids")


def _validate_chronology_item(value: Any, index: int) -> None:
    path = f"diagnosis.failure_chronology[{index}]"
    item = _require_object(value, path)
    _validate_keys(item, path, required=CHRONOLOGY_FIELDS)
    _require_priority(item["priority"], f"{path}.priority")
    for field in CHRONOLOGY_FIELDS:
        if field != "priority":
            _require_string(item[field], f"{path}.{field}")
    _require_chinese_text(item["event"], f"{path}.event")
    _require_chinese_text(item["consequence"], f"{path}.consequence")


def _validate_output_assessment(value: Any) -> None:
    path = "diagnosis.task_and_output_files_assessment"
    assessment = _require_object(value, path)
    _validate_keys(assessment, path, required=OUTPUT_ASSESSMENT_FIELDS)
    _require_string_array(
        assessment["expected_output_files"], f"{path}.expected_output_files"
    )
    _require_string_array(assessment["actual_output_files"], f"{path}.actual_output_files")
    _require_chinese_text(assessment["impact"], f"{path}.impact")
    _require_chinese_text(assessment["evidence"], f"{path}.evidence")


def _validate_diagnosis(value: Any) -> None:
    diagnosis = _require_object(value, "diagnosis")
    _validate_keys(
        diagnosis,
        "diagnosis",
        required=DIAGNOSIS_REQUIRED_FIELDS,
        optional=DIAGNOSIS_OPTIONAL_FIELDS,
    )
    classification = diagnosis["execution_classification"]
    if classification not in EXECUTION_CLASSIFICATIONS:
        raise ValueError(
            "model diagnostic report diagnosis.execution_classification must be one of: "
            + ", ".join(EXECUTION_CLASSIFICATIONS)
        )
    _require_chinese_text(
        diagnosis["primary_failure_mode"],
        "diagnosis.primary_failure_mode",
    )
    _require_chinese_text(diagnosis["conclusion"], "diagnosis.conclusion")
    for field in DIAGNOSIS_ARRAY_FIELDS:
        if not isinstance(diagnosis[field], list):
            raise ValueError(
                f"model diagnostic report diagnosis.{field} must be a JSON array"
            )
    for index, item in enumerate(diagnosis["evidence_chain"]):
        _validate_evidence_item(item, index)
    for index, item in enumerate(diagnosis["error_span_inventory"]):
        _validate_error_inventory_item(item, index)
    for index, item in enumerate(diagnosis["failure_chronology"]):
        _validate_chronology_item(item, index)
    if "task_and_output_files_assessment" in diagnosis:
        _validate_output_assessment(diagnosis["task_and_output_files_assessment"])


def _validate_proposed_changes(
    value: Any,
    *,
    execution_classification: str,
    allowed_components: Iterable[str] | None,
    allowed_targets: Iterable[str] | None,
) -> None:
    if not isinstance(value, list):
        raise ValueError("model diagnostic report proposed_changes must be a JSON array")
    if execution_classification == "FAILED":
        if not 3 <= len(value) <= 5:
            raise ValueError(
                "FAILED diagnostic reports must contain exactly 3-5 proposed_changes"
            )
    elif len(value) > 5:
        raise ValueError(
            "non-FAILED diagnostic reports must contain at most 5 proposed_changes"
        )
    components = set(allowed_components or ())
    targets = set(allowed_targets or ())
    for index, raw_change in enumerate(value):
        path = f"proposed_changes[{index}]"
        change = _require_object(raw_change, path)
        _validate_keys(change, path, required=PROPOSED_CHANGE_FIELDS)
        if components and change["component"] not in components:
            raise ValueError(
                f"model diagnostic report {path}.component must be one of: "
                + ", ".join(sorted(components))
            )
        if targets and change["target"] not in targets:
            raise ValueError(
                f"model diagnostic report {path}.target must be one of: "
                + ", ".join(sorted(targets))
            )
        _require_priority(change["priority"], f"{path}.priority")
        for field in PROPOSED_CHANGE_FIELDS:
            if field != "priority":
                _require_string(change[field], f"{path}.{field}", allow_empty=False)
        for field in ("title", "problem", "implementation", "expected_impact"):
            _require_chinese_text(change[field], f"{path}.{field}")


def normalize_json_report(
    report: str,
    *,
    allowed_components: Iterable[str] | None = None,
    allowed_targets: Iterable[str] | None = None,
) -> str:
    """Validate an LLM report and return deterministic, pretty UTF-8 JSON."""
    candidate = report.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = "\n".join(candidate.splitlines()[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"model returned an invalid JSON diagnostic report: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("model diagnostic report must be a JSON object")
    _validate_keys(value, "root", required=REQUIRED_TOP_LEVEL_FIELDS)
    if value["schema_version"] != REPORT_SCHEMA_VERSION:
        raise ValueError(
            f"model diagnostic report schema_version must be {REPORT_SCHEMA_VERSION}"
        )
    _validate_report_summary(value["report_summary"])
    _validate_diagnosis(value["diagnosis"])
    _validate_proposed_changes(
        value["proposed_changes"],
        execution_classification=value["diagnosis"]["execution_classification"],
        allowed_components=allowed_components,
        allowed_targets=allowed_targets,
    )
    diagnosis = value["diagnosis"]
    value["diagnosis"] = {
        field: diagnosis[field]
        for field in DIAGNOSIS_CANONICAL_FIELDS
        if field in diagnosis
    }
    return json.dumps(value, ensure_ascii=False, indent=2)
