"""Single source of truth for HALO diagnostic report JSON."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

REPORT_SCHEMA_VERSION = 6
REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "report_summary",
    "diagnosis",
    "proposed_changes",
)
EXECUTION_CLASSIFICATIONS = (
    "FAILED",
    "SUCCEEDED_WITH_RECOVERED_ERRORS",
    "SUCCEEDED_WITH_UNPROVEN_RECOVERY",
    "SUCCEEDED_CLEANLY",
    "UNKNOWN",
)
PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
EVIDENCE_SOURCES = ("TRACE", "TASK", "JUDGE", "SOURCE_FILE", "OUTPUT_FILE")
RECOVERY_STATUSES = ("RECOVERED", "UNRECOVERED", "UNPROVEN", "NOT_APPLICABLE")
REPORT_SUMMARY_REQUIRED_FIELDS = ("task_id", "task", "trace_ids")
REPORT_SUMMARY_OPTIONAL_FIELDS = ("expected_output_files", "judge_summary")
DIAGNOSIS_REQUIRED_FIELDS = (
    "execution_classification",
    "primary_failure_mode",
    "error_findings",
)
DIAGNOSIS_CANONICAL_FIELDS = (
    "execution_classification",
    "primary_failure_mode",
    "error_findings",
)
ERROR_FIELDS = (
    "error_id",
    "priority",
    "category",
    "title",
    "occurrence_count",
    "summary",
    "evidence",
    "root_cause",
    "recovery_status",
    "impact",
)
EVIDENCE_FIELDS = ("source", "reference", "tool", "fact", "error")
PROPOSED_CHANGE_FIELDS = (
    "priority",
    "component",
    "target",
    "title",
    "error_refs",
    "problem",
    "implementation",
    "acceptance_criteria",
    "expected_impact",
)
REPORT_STRUCTURE_GUIDANCE = (
    "Use exactly the shown v6 fields and nesting; do not add ad-hoc fields. Group each "
    "distinct material problem into one diagnosis.error_findings item. Do not repeat the same "
    "spans in a generic tool-failure finding and a second semantic finding; split findings "
    "by root cause. Summarize the dominant root cause briefly in primary_failure_mode. "
    "Use P0-P4 only "
    "on error_findings and proposed_changes as relative ranking labels. Evidence "
    "has no id or priority: use source plus reference to identify TRACE spans, TASK/JUDGE "
    "items, or source/output files. Preserve raw error text; write error titles, summaries, "
    "facts, root causes, impacts, change titles/problems/implementations/acceptance criteria/"
    "impacts in Simplified Chinese. Keep JSON keys, enums, priorities, task/trace/span ids, "
    "component/target values, tool names, paths, filenames, and raw errors unchanged. Every "
    "proposed change must reference one or more existing error ids. Copy "
    "the resolved task_id and task from Context; use the explicit MISSING context value when unavailable. "
    "Copy expected_output_files unchanged when supplied and omit it when missing. Include "
    "judge_summary only when Judge context exists. Use [] when no error findings or changes are "
    "supported. FAILED reports require exactly 3-5 proposed_changes; every other execution "
    "classification allows 0-5."
)


def build_report(
    *,
    report_summary: dict[str, Any],
    proposed_changes: list[dict[str, Any]] | None = None,
    **diagnosis: Any,
) -> dict[str, Any]:
    """Build a report with canonical v6 nesting."""
    diagnosis = dict(diagnosis)
    diagnosis.setdefault("error_findings", [])
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_summary": report_summary,
        "diagnosis": {
            field: diagnosis[field]
            for field in DIAGNOSIS_CANONICAL_FIELDS
            if field in diagnosis
        },
        "proposed_changes": proposed_changes or [],
    }


def render_report_example(
    components: Iterable[str], *, include_evaluator_context: bool = False
) -> str:
    """Render the compact schema example embedded in model prompts."""
    summary: dict[str, Any] = {
        "task_id": "task15" if include_evaluator_context else "MISSING",
        "task": "根据源文件生成数据可视化图表。" if include_evaluator_context else "MISSING",
        "trace_ids": ["..."],
    }
    if include_evaluator_context:
        summary.update(
            {
                "expected_output_files": ["output.xlsx"],
                "judge_summary": "Judge指出数据提取和图表类型存在错误。",
            }
        )
    report = build_report(
        report_summary=summary,
        execution_classification="<classification>",
        primary_failure_mode="工具参数或环境与运行时不兼容，导致任务未完成预期操作。",
        error_findings=[
            {
                "error_id": "ERR1",
                "priority": "P0",
                "category": "TOOL_FAILURE",
                "title": "工具调用失败",
                "occurrence_count": 1,
                "summary": "Runner执行工具时发生错误。",
                "evidence": [
                    {
                        "source": "TRACE",
                        "reference": "...",
                        "tool": "bash",
                        "fact": "工具调用返回失败状态。",
                        "error": "raw error",
                    }
                ],
                "root_cause": "工具参数或环境与运行时不兼容。",
                "recovery_status": "UNRECOVERED",
                "impact": "任务未完成预期操作。",
            }
        ],
        proposed_changes=[
            {
                "priority": "P0",
                "component": "|".join(components),
                "target": "...",
                "title": "修复工具调用前置校验",
                "error_refs": ["ERR1"],
                "problem": "工具调用在不兼容环境中直接失败。",
                "implementation": "调用前检查环境与参数并提供兼容路径。",
                "acceptance_criteria": ["相同输入不再产生该工具错误。"],
                "expected_impact": "避免重复失败并缩短执行时间。",
            }
        ],
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


def _require_chinese_text(value: Any, path: str) -> None:
    _require_string(value, path, allow_empty=False)
    if not _contains_cjk(value):
        raise ValueError(
            f"model diagnostic report {path} must contain Simplified Chinese narrative text"
        )


def _require_string_array(
    value: Any, path: str, *, non_empty: bool = False, chinese: bool = False
) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"model diagnostic report {path} must be an array of strings")
    if non_empty and (not value or any(not item.strip() for item in value)):
        raise ValueError(f"model diagnostic report {path} must contain non-empty strings")
    if chinese:
        for index, item in enumerate(value):
            _require_chinese_text(item, f"{path}[{index}]")


def _require_positive_int(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"model diagnostic report {path} must be an integer >= 1")


def _require_priority(value: Any, path: str) -> None:
    if value not in PRIORITIES:
        raise ValueError(
            f"model diagnostic report {path} must be one of: {', '.join(PRIORITIES)}"
        )


def _validate_report_summary(value: Any) -> dict[str, Any]:
    summary = _require_object(value, "report_summary")
    _validate_keys(
        summary,
        "report_summary",
        required=REPORT_SUMMARY_REQUIRED_FIELDS,
        optional=REPORT_SUMMARY_OPTIONAL_FIELDS,
    )
    _require_string(summary["task_id"], "report_summary.task_id", allow_empty=False)
    _require_string(summary["task"], "report_summary.task", allow_empty=False)
    _require_string_array(summary["trace_ids"], "report_summary.trace_ids", non_empty=True)
    if "expected_output_files" in summary:
        _require_string_array(
            summary["expected_output_files"], "report_summary.expected_output_files"
        )
    if "judge_summary" in summary:
        _require_string(summary["judge_summary"], "report_summary.judge_summary", allow_empty=False)
    return summary


def _validate_evidence(value: Any, error_index: int, evidence_index: int) -> None:
    path = f"diagnosis.error_findings[{error_index}].evidence[{evidence_index}]"
    item = _require_object(value, path)
    _validate_keys(item, path, required=EVIDENCE_FIELDS)
    if item["source"] not in EVIDENCE_SOURCES:
        raise ValueError(
            f"model diagnostic report {path}.source must be one of: "
            + ", ".join(EVIDENCE_SOURCES)
        )
    _require_string(item["reference"], f"{path}.reference", allow_empty=False)
    _require_string(item["tool"], f"{path}.tool")
    _require_chinese_text(item["fact"], f"{path}.fact")
    _require_string(item["error"], f"{path}.error")


def _validate_error_findings(value: Any) -> set[str]:
    if not isinstance(value, list):
        raise ValueError(
            "model diagnostic report diagnosis.error_findings must be a JSON array"
        )
    error_ids: set[str] = set()
    for index, raw_error in enumerate(value):
        path = f"diagnosis.error_findings[{index}]"
        diagnostic_error = _require_object(raw_error, path)
        _validate_keys(diagnostic_error, path, required=ERROR_FIELDS)
        error_id = diagnostic_error["error_id"]
        if not isinstance(error_id, str) or not re.fullmatch(r"ERR[1-9]\d*", error_id):
            raise ValueError(f"model diagnostic report {path}.error_id must match ERR<number>")
        if error_id in error_ids:
            raise ValueError(f"model diagnostic report has duplicate error_id: {error_id}")
        error_ids.add(error_id)
        _require_priority(diagnostic_error["priority"], f"{path}.priority")
        category = diagnostic_error["category"]
        if not isinstance(category, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", category):
            raise ValueError(
                f"model diagnostic report {path}.category must be UPPER_SNAKE_CASE"
            )
        for field in ("title", "summary", "root_cause", "impact"):
            _require_chinese_text(diagnostic_error[field], f"{path}.{field}")
        _require_positive_int(
            diagnostic_error["occurrence_count"], f"{path}.occurrence_count"
        )
        if diagnostic_error["recovery_status"] not in RECOVERY_STATUSES:
            raise ValueError(
                f"model diagnostic report {path}.recovery_status must be one of: "
                + ", ".join(RECOVERY_STATUSES)
            )
        if not isinstance(diagnostic_error["evidence"], list) or not diagnostic_error["evidence"]:
            raise ValueError(f"model diagnostic report {path}.evidence must be a non-empty array")
        for evidence_index, evidence in enumerate(diagnostic_error["evidence"]):
            _validate_evidence(evidence, index, evidence_index)
    return error_ids


def _validate_diagnosis(value: Any) -> set[str]:
    diagnosis = _require_object(value, "diagnosis")
    _validate_keys(diagnosis, "diagnosis", required=DIAGNOSIS_REQUIRED_FIELDS)
    classification = diagnosis["execution_classification"]
    if classification not in EXECUTION_CLASSIFICATIONS:
        raise ValueError(
            "model diagnostic report diagnosis.execution_classification must be one of: "
            + ", ".join(EXECUTION_CLASSIFICATIONS)
        )
    _require_chinese_text(
        diagnosis["primary_failure_mode"], "diagnosis.primary_failure_mode"
    )
    error_ids = _validate_error_findings(diagnosis["error_findings"])
    if classification == "FAILED" and not error_ids:
        raise ValueError("FAILED diagnostic reports must contain at least one error")
    return error_ids


def _validate_proposed_changes(
    value: Any,
    *,
    execution_classification: str,
    error_ids: set[str],
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
        for field in ("component", "target"):
            _require_string(change[field], f"{path}.{field}", allow_empty=False)
        for field in ("title", "problem", "implementation", "expected_impact"):
            _require_chinese_text(change[field], f"{path}.{field}")
        _require_string_array(
            change["error_refs"], f"{path}.error_refs", non_empty=True
        )
        unknown_refs = sorted(set(change["error_refs"]) - error_ids)
        if unknown_refs:
            raise ValueError(
                f"model diagnostic report {path}.error_refs references unknown error ids: "
                + ", ".join(unknown_refs)
            )
        _require_string_array(
            change["acceptance_criteria"],
            f"{path}.acceptance_criteria",
            non_empty=True,
            chinese=True,
        )


def _ordered_object(value: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: value[field] for field in fields if field in value}


def _normalize_order(value: dict[str, Any]) -> dict[str, Any]:
    summary_fields = (*REPORT_SUMMARY_REQUIRED_FIELDS, *REPORT_SUMMARY_OPTIONAL_FIELDS)
    summary = _ordered_object(value["report_summary"], summary_fields)
    diagnosis = _ordered_object(value["diagnosis"], DIAGNOSIS_CANONICAL_FIELDS)
    normalized_findings = []
    for diagnostic_error in diagnosis["error_findings"]:
        ordered = _ordered_object(diagnostic_error, ERROR_FIELDS)
        ordered["evidence"] = [
            _ordered_object(item, EVIDENCE_FIELDS) for item in diagnostic_error["evidence"]
        ]
        normalized_findings.append(ordered)
    diagnosis["error_findings"] = normalized_findings
    changes = [
        _ordered_object(change, PROPOSED_CHANGE_FIELDS)
        for change in value["proposed_changes"]
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_summary": summary,
        "diagnosis": diagnosis,
        "proposed_changes": changes,
    }


def normalize_json_report(
    report: str,
    *,
    allowed_components: Iterable[str] | None = None,
    allowed_targets: Iterable[str] | None = None,
) -> str:
    """Validate a model report and return deterministic, pretty UTF-8 JSON."""
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
    error_ids = _validate_diagnosis(value["diagnosis"])
    _validate_proposed_changes(
        value["proposed_changes"],
        execution_classification=value["diagnosis"]["execution_classification"],
        error_ids=error_ids,
        allowed_components=allowed_components,
        allowed_targets=allowed_targets,
    )
    return json.dumps(_normalize_order(value), ensure_ascii=False, indent=2)
