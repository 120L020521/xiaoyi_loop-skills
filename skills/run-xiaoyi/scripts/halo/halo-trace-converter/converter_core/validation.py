"""Validate input rows, span shapes, and trace graphs."""

from __future__ import annotations

from .content import halo_time
from .models import Json


def validate_input(rows: list[Json], *, require_chronological: bool = True) -> None:
    if not rows:
        raise ValueError("input JSONL is empty")
    previous_time = ""
    for index, row in enumerate(rows, 1):
        if not isinstance(row.get("event"), str):
            raise ValueError(f"line {index}: missing string field 'event'")
        if not isinstance(row.get("payload"), dict):
            raise ValueError(f"line {index}: missing object field 'payload'")
        try:
            normalized_time = halo_time(row.get("timestamp"))
        except ValueError as exc:
            raise ValueError(f"line {index}: {exc}") from exc
        if require_chronological and previous_time and normalized_time < previous_time:
            raise ValueError(
                f"line {index}: timestamp {row.get('timestamp')!r} is earlier than "
                "the preceding event; input must be chronological"
            )
        previous_time = normalized_time


def validate_span(row: Json, index: int) -> None:
    required = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "trace_state",
        "name",
        "kind",
        "start_time",
        "end_time",
        "status",
        "resource",
        "scope",
        "attributes",
    )
    for key in required:
        if key not in row:
            raise ValueError(f"output line {index}: missing {key}")

    attrs = row["attributes"]
    if not isinstance(attrs, dict):
        raise ValueError(f"output line {index}: attributes must be an object")
    for key in (
        "inference.export.schema_version",
        "inference.project_id",
        "inference.observation_kind",
    ):
        if key not in attrs:
            raise ValueError(f"output line {index}: missing attributes.{key}")
    for key in ("trace_id", "span_id", "name", "kind", "start_time", "end_time"):
        if not isinstance(row[key], str) or not row[key]:
            raise ValueError(f"output line {index}: {key} must be a non-empty string")
    if not isinstance(row.get("parent_span_id", ""), str):
        raise ValueError(f"output line {index}: parent_span_id must be a string")
    if not isinstance(row.get("trace_state"), str):
        raise ValueError(f"output line {index}: trace_state must be a string")
    if row["kind"] not in {"SPAN_KIND_INTERNAL", "SPAN_KIND_CLIENT"}:
        raise ValueError(f"output line {index}: unsupported span kind {row['kind']!r}")

    status = row["status"]
    if not isinstance(status, dict) or status.get("code") not in {
        "STATUS_CODE_OK",
        "STATUS_CODE_ERROR",
        "STATUS_CODE_UNSET",
    }:
        raise ValueError(f"output line {index}: invalid status")
    if not isinstance(status.get("message", ""), str):
        raise ValueError(f"output line {index}: status.message must be a string")
    resource = row["resource"]
    scope = row["scope"]
    if not isinstance(resource, dict) or not isinstance(resource.get("attributes"), dict):
        raise ValueError(f"output line {index}: invalid resource")
    if not isinstance(scope, dict) or not isinstance(scope.get("name"), str):
        raise ValueError(f"output line {index}: invalid scope")
    if not isinstance(scope.get("version", ""), str):
        raise ValueError(f"output line {index}: scope.version must be a string")
    if halo_time(row["start_time"]) > halo_time(row["end_time"]):
        raise ValueError(f"output line {index}: start_time is later than end_time")


def validate_trace_graph(rows: list[Json]) -> None:
    """Validate identity, timing, parent references, and one root per trace."""
    by_trace: dict[str, dict[str, Json]] = {}
    for index, row in enumerate(rows, 1):
        validate_span(row, index)
        spans = by_trace.setdefault(row["trace_id"], {})
        if row["span_id"] in spans:
            raise ValueError(
                f"output line {index}: duplicate span_id {row['span_id']!r} "
                f"in trace {row['trace_id']!r}"
            )
        spans[row["span_id"]] = row

    allowed_kinds = {"AGENT", "LLM", "TOOL"}
    for trace_id, spans in by_trace.items():
        roots = [row for row in spans.values() if not row.get("parent_span_id")]
        if len(roots) != 1:
            raise ValueError(
                f"trace {trace_id!r}: expected exactly one root span, got {len(roots)}"
            )
        for row in spans.values():
            parent_id = row.get("parent_span_id")
            if parent_id and parent_id not in spans:
                raise ValueError(
                    f"trace {trace_id!r}: span {row['span_id']!r} references "
                    f"missing parent {parent_id!r}"
                )
            observation_kind = row["attributes"].get("inference.observation_kind")
            if observation_kind not in allowed_kinds:
                raise ValueError(
                    f"trace {trace_id!r}: invalid inference.observation_kind "
                    f"{observation_kind!r}"
                )
            if parent_id:
                parent = spans[parent_id]
                if (
                    parent["start_time"] > row["start_time"]
                    or parent["end_time"] < row["end_time"]
                ):
                    raise ValueError(
                        f"trace {trace_id!r}: parent {parent_id!r} does not enclose "
                        f"child {row['span_id']!r}"
                    )

            visited: set[str] = set()
            current = row
            while current.get("parent_span_id"):
                current_id = current["span_id"]
                if current_id in visited:
                    raise ValueError(
                        f"trace {trace_id!r}: parent cycle includes span {current_id!r}"
                    )
                visited.add(current_id)
                next_parent_id = current["parent_span_id"]
                if next_parent_id not in spans:
                    raise ValueError(
                        f"trace {trace_id!r}: span {current_id!r} references "
                        f"missing parent {next_parent_id!r}"
                    )
                current = spans[next_parent_id]
