"""Local helpers for the no-API-key, host-agent HALO workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .better_harness import (
    BETTER_HARNESS_COMPONENTS,
    DEFAULT_EDITABLE_SURFACES,
    build_halo_prompt,
)
from .report_contract import (
    RAW_LOG_EXCERPT_CONTEXT_FLOOR_CHARS,
    RAW_LOG_EXCERPT_MAX_CHARS,
    normalize_json_report,
)
from .source_evidence import (
    SourceEvidence,
    build_source_evidence,
    choose_source_excerpt,
)


def _load_object(path: str | None, label: str) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _build_prompt(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    task = _load_object(args.task_json, "--task-json")
    if args.task_id:
        task["task_id"] = args.task_id
    elif task and not any(key in task for key in ("task_id", "id", "taskId")):
        task["task_id"] = Path(args.task_json).resolve().parent.name
    prompt = build_halo_prompt(
        task=task,
        judge_result=_load_object(args.judge_result, "--judge-result"),
        surface_filenames=args.surface or list(DEFAULT_EDITABLE_SURFACES),
        additional_request=args.prompt,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prompt.rstrip() + "\n", encoding="utf-8")
    return {"status": "ok", "prompt_path": str(output), "chars": len(prompt)}


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"{label} is empty: {path}")


def _trace_references(
    source_path: Path,
    trace_path: Path,
) -> tuple[
    set[str],
    set[tuple[str, str]],
    set[str],
    dict[tuple[str, str], SourceEvidence],
]:
    evidence_map = build_source_evidence(source_path, trace_path)
    span_pairs = set(evidence_map)
    trace_ids = {trace_id for trace_id, _span_id in span_pairs}
    span_ids = {span_id for _trace_id, span_id in span_pairs}
    if not trace_ids:
        raise ValueError(f"prepared trace contains no trace/span ids: {trace_path}")
    return trace_ids, span_pairs, span_ids, evidence_map


def _validate_report_references(
    report: dict[str, Any],
    *,
    trace_ids: set[str],
    span_pairs: set[tuple[str, str]],
    span_ids: set[str],
    source_evidence: dict[tuple[str, str], SourceEvidence],
) -> None:
    reported_trace_ids = set(report["report_summary"]["trace_ids"])
    unknown_traces = sorted(reported_trace_ids - trace_ids)
    if unknown_traces:
        raise ValueError(
            "HALO report references trace ids absent from the prepared trace: "
            + ", ".join(unknown_traces)
        )

    diagnosis = report["diagnosis"]
    for error_index, diagnostic_error in enumerate(diagnosis["error_findings"]):
        for evidence_index, item in enumerate(diagnostic_error["evidence"]):
            if item["source"] != "TRACE":
                continue
            reference = item["reference"]
            matching_pairs = {
                (trace_id, span_id)
                for trace_id, span_id in span_pairs
                if span_id == reference and trace_id in reported_trace_ids
            }
            path = (
                f"diagnosis.error_findings[{error_index}].evidence[{evidence_index}]"
            )
            if not any(span_id == reference for _trace_id, span_id in span_pairs):
                raise ValueError(
                    f"HALO report {path} references an absent span id: {reference}"
                )
            if not matching_pairs:
                raise ValueError(
                    f"HALO report {path} references a span outside "
                    f"report_summary.trace_ids: {reference}"
                )
            if len(matching_pairs) != 1:
                raise ValueError(
                    f"HALO report {path} span reference is ambiguous across reported traces: "
                    f"{reference}"
                )
            matched_pair = next(iter(matching_pairs))
            mapped = source_evidence[matched_pair]
            if item["span_index"] != mapped.span_index:
                raise ValueError(
                    f"HALO report {path}.span_index does not match referenced span "
                    f"{reference}: got {item['span_index']}, expected {mapped.span_index}"
                )
            excerpt = item["raw_log_excerpt"]
            sources = mapped.candidates
            if not sources:
                raise ValueError(
                    f"HALO report {path} has no mapped pre-conversion source events "
                    f"for referenced span: {reference}"
                )
            if not any(excerpt in serialized for serialized in sources):
                raise ValueError(
                    f"HALO report {path}.raw_log_excerpt is not a verbatim substring "
                    f"of mapped pre-conversion source events for span: {reference}"
                )
            available_context = max(len(serialized) for serialized in sources)
            required_context = min(
                RAW_LOG_EXCERPT_CONTEXT_FLOOR_CHARS,
                available_context,
            )
            if len(excerpt) < required_context:
                raise ValueError(
                    f"HALO report {path}.raw_log_excerpt is too short to show context: "
                    f"got {len(excerpt)} characters, require at least {required_context} "
                    f"for referenced span {reference}"
                )
            outcome_candidates = mapped.outcome_candidates
            if not any(
                excerpt in outcome or outcome in excerpt
                for outcome in outcome_candidates
            ):
                raise ValueError(
                    f"HALO report {path}.raw_log_excerpt must include verbatim "
                    f"execution status or error output from mapped pre-conversion "
                    f"source events for span: {reference}"
                )


def _validate_bundle(
    *,
    report_path: Path,
    report: dict[str, Any],
    manifest_path: Path,
) -> dict[str, str]:
    _require_file(manifest_path, "HALO manifest")
    manifest = _load_object(str(manifest_path), "--manifest")
    if manifest.get("schema_version") != 3:
        raise ValueError(f"HALO manifest schema_version must be 3: {manifest_path}")
    errors = manifest.get("errors")
    if not isinstance(errors, list):
        raise ValueError(f"HALO manifest errors must be an array: {manifest_path}")
    if errors:
        raise ValueError(f"HALO manifest contains preparation errors: {manifest_path}")
    entries = manifest.get("prepared_traces")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError(
            f"HALO manifest must contain exactly one prepared trace: {manifest_path}"
        )

    entry = entries[0]
    required_paths: dict[str, Path] = {}
    for field in ("source", "selected", "prompt_path", "report_path", "manifest_path"):
        raw_path = entry.get(field)
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(f"HALO manifest entry.{field} must be a non-empty path")
        required_paths[field] = Path(raw_path).resolve()

    if required_paths["manifest_path"] != manifest_path:
        raise ValueError(f"HALO manifest entry.manifest_path does not match: {manifest_path}")
    if required_paths["report_path"] != report_path:
        raise ValueError(f"HALO manifest report_path does not match: {manifest_path}")
    artifact_dir = manifest_path.parent
    for field in ("selected", "prompt_path", "report_path"):
        if required_paths[field].parent != artifact_dir:
            raise ValueError(
                f"HALO manifest entry.{field} must stay in the artifact directory: "
                f"{required_paths[field]}"
            )

    for field, label in (
        ("source", "source trace"),
        ("selected", "prepared trace"),
        ("prompt_path", "HALO prompt"),
        ("report_path", "HALO report"),
    ):
        _require_file(required_paths[field], label)

    source_path = required_paths["source"]
    trace_path = required_paths["selected"]
    prompt_path = required_paths["prompt_path"]
    if trace_path != source_path and trace_path.stat().st_mtime_ns < source_path.stat().st_mtime_ns:
        raise ValueError(f"prepared trace is older than its source: {trace_path}")
    if report_path.stat().st_mtime_ns < prompt_path.stat().st_mtime_ns:
        raise ValueError(f"HALO report is older than the authoritative prompt: {report_path}")

    (
        trace_ids,
        span_pairs,
        span_ids,
        source_evidence,
    ) = _trace_references(source_path, trace_path)
    _validate_report_references(
        report,
        trace_ids=trace_ids,
        span_pairs=span_pairs,
        span_ids=span_ids,
        source_evidence=source_evidence,
    )
    return {
        "manifest_path": str(manifest_path),
        "source_trace": str(source_path),
        "prepared_trace": str(trace_path),
        "prompt_path": str(prompt_path),
    }


def _source_evidence(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).resolve()
    _require_file(manifest_path, "HALO manifest")
    manifest = _load_object(str(manifest_path), "--manifest")
    entries = manifest.get("prepared_traces")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise ValueError(
            f"HALO manifest must contain exactly one prepared trace: {manifest_path}"
        )
    entry = entries[0]
    source_path = Path(str(entry.get("source") or "")).resolve()
    trace_path = Path(str(entry.get("selected") or "")).resolve()
    _require_file(source_path, "source trace")
    _require_file(trace_path, "prepared trace")
    evidence_map = build_source_evidence(source_path, trace_path)
    matches = [
        evidence
        for (trace_id, span_id), evidence in evidence_map.items()
        if span_id == args.span_id and (args.trace_id is None or trace_id == args.trace_id)
    ]
    if not matches:
        raise ValueError(f"prepared trace contains no span id: {args.span_id}")
    if len(matches) != 1:
        raise ValueError(
            f"span id is ambiguous; also pass --trace-id: {args.span_id}"
        )
    evidence = matches[0]
    excerpt = choose_source_excerpt(
        evidence,
        max_chars=RAW_LOG_EXCERPT_MAX_CHARS,
        pattern=args.pattern,
        context_buffer_chars=args.context_buffer_chars,
    )
    return {
        "status": "ok",
        "trace_id": evidence.trace_id,
        "span_id": evidence.span_id,
        "span_index": evidence.span_index,
        "source_path": str(source_path),
        "source_line_numbers": list(evidence.source_line_numbers),
        "raw_log_excerpt": excerpt,
        "chars": len(excerpt),
    }


def _validate_report(args: argparse.Namespace) -> dict[str, Any]:
    report_path = Path(args.report).resolve()
    _require_file(report_path, "HALO report")
    surfaces = args.surface or list(DEFAULT_EDITABLE_SURFACES)
    normalized = normalize_json_report(
        report_path.read_text(encoding="utf-8"),
        allowed_components=BETTER_HARNESS_COMPONENTS,
        allowed_targets=surfaces,
    )
    report = json.loads(normalized)
    bundle: dict[str, str] = {}
    if args.manifest is not None:
        bundle = _validate_bundle(
            report_path=report_path,
            report=report,
            manifest_path=Path(args.manifest).resolve(),
        )
    report_path.write_text(normalized + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "validation": "complete" if bundle else "schema_only",
        "report_path": str(report_path),
        **bundle,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo-rlm-agent",
        description="Local prompt/report helpers for host-agent diagnosis; no LLM API is used.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt = subparsers.add_parser("build-prompt", help="Write halo_prompt.txt locally")
    prompt.add_argument("--output", required=True, help="Destination halo_prompt.txt")
    prompt.add_argument("--task-json", default=None, help="Optional task JSON object")
    prompt.add_argument(
        "--task-id",
        default=None,
        help="Optional task id override; defaults to task JSON metadata or parent folder",
    )
    prompt.add_argument("--judge-result", default=None, help="Optional Judge JSON object")
    prompt.add_argument("--surface", action="append", default=None)
    prompt.add_argument("-p", "--prompt", default=None, help="Additional diagnostic request")
    prompt.set_defaults(handler=_build_prompt)

    source = subparsers.add_parser(
        "source-evidence",
        help="Map one prepared span to verbatim pre-conversion source JSONL events",
    )
    source.add_argument("--manifest", required=True, help="halo-prepared-manifest.json")
    source.add_argument("--span-id", required=True)
    source.add_argument("--trace-id", default=None)
    source.add_argument(
        "--pattern",
        default=None,
        help="Optional regex used to center an oversized source excerpt",
    )
    source.add_argument("--context-buffer-chars", type=int, default=800)
    source.set_defaults(handler=_source_evidence)

    validate = subparsers.add_parser(
        "validate-report", help="Validate and normalize a host-agent halo_report.json"
    )
    validate.add_argument("report", help="Path to halo_report.json")
    validate.add_argument(
        "--manifest",
        default=None,
        help=(
            "Optional halo-prepared-manifest.json; when supplied, also validate "
            "artifact binding, freshness, and trace/span references"
        ),
    )
    validate.add_argument("--surface", action="append", default=None)
    validate.set_defaults(handler=_validate_report)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
