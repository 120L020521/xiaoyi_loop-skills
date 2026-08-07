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
from .report_contract import normalize_json_report


def _load_object(path: str | None, label: str) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _build_prompt(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    prompt = build_halo_prompt(
        task=_load_object(args.task_json, "--task-json"),
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


def _trace_references(trace_path: Path) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    trace_ids: set[str] = set()
    span_pairs: set[tuple[str, str]] = set()
    span_ids: set[str] = set()
    with trace_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                span = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"prepared trace contains invalid JSON at line {line_number}: {trace_path}"
                ) from exc
            if not isinstance(span, dict):
                raise ValueError(
                    f"prepared trace line {line_number} must be a JSON object: {trace_path}"
                )
            trace_id = span.get("trace_id")
            span_id = span.get("span_id")
            if isinstance(trace_id, str) and trace_id and isinstance(span_id, str) and span_id:
                trace_ids.add(trace_id)
                span_pairs.add((trace_id, span_id))
                span_ids.add(span_id)
    if not trace_ids:
        raise ValueError(f"prepared trace contains no trace/span ids: {trace_path}")
    return trace_ids, span_pairs, span_ids


def _validate_report_references(
    report: dict[str, Any],
    *,
    trace_ids: set[str],
    span_pairs: set[tuple[str, str]],
    span_ids: set[str],
) -> None:
    reported_trace_ids = set(report["report_summary"]["trace_ids"])
    unknown_traces = sorted(reported_trace_ids - trace_ids)
    if unknown_traces:
        raise ValueError(
            "HALO report references trace ids absent from the prepared trace: "
            + ", ".join(unknown_traces)
        )

    diagnosis = report["diagnosis"]
    for index, item in enumerate(diagnosis["evidence_chain"]):
        reference = (item["trace_id"], item["span_id"])
        if reference not in span_pairs:
            raise ValueError(
                "HALO report diagnosis.evidence_chain"
                f"[{index}] references an absent trace/span pair: "
                f"{reference[0]}/{reference[1]}"
            )
        if item["trace_id"] not in reported_trace_ids:
            raise ValueError(
                "HALO report diagnosis.evidence_chain"
                f"[{index}].trace_id is missing from report_summary.trace_ids: "
                f"{item['trace_id']}"
            )

    for index, item in enumerate(diagnosis["error_span_inventory"]):
        unknown_spans = sorted(set(item["sample_span_ids"]) - span_ids)
        if unknown_spans:
            raise ValueError(
                "HALO report diagnosis.error_span_inventory"
                f"[{index}] references absent sample span ids: "
                + ", ".join(unknown_spans)
            )

    for index, item in enumerate(diagnosis["failure_chronology"]):
        if item["span_id"] not in span_ids:
            raise ValueError(
                "HALO report diagnosis.failure_chronology"
                f"[{index}] references an absent span id: {item['span_id']}"
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

    trace_ids, span_pairs, span_ids = _trace_references(trace_path)
    _validate_report_references(
        report,
        trace_ids=trace_ids,
        span_pairs=span_pairs,
        span_ids=span_ids,
    )
    return {
        "manifest_path": str(manifest_path),
        "source_trace": str(source_path),
        "prepared_trace": str(trace_path),
        "prompt_path": str(prompt_path),
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
    prompt.add_argument("--judge-result", default=None, help="Optional Judge JSON object")
    prompt.add_argument("--surface", action="append", default=None)
    prompt.add_argument("-p", "--prompt", default=None, help="Additional diagnostic request")
    prompt.set_defaults(handler=_build_prompt)

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
