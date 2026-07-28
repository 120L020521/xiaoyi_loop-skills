"""Read and write JSONL files and resolve input/output paths."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from .content import normalized_key
from .conversion import (
    attach_linked_subagents,
    convert_events,
    subagent_session_candidates,
)
from .models import ConversionOptions, Json
from .validation import validate_span


class EmptyJsonlError(ValueError):
    """Raised when a JSONL file contains no non-blank JSON objects."""


def read_jsonl(path: Path, skip_bad_lines: bool) -> tuple[list[Json], int]:
    rows: list[Json] = []
    skipped = 0
    with path.open("r", encoding="utf-8-sig") as source:
        for line_no, line in enumerate(source, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise TypeError("expected JSON object")
                rows.append(row)
            except Exception as exc:  # noqa: BLE001
                if not skip_bad_lines:
                    raise
                skipped += 1
                print(f"[skip] line {line_no}: {exc}", file=sys.stderr)
    return rows, skipped


def convert_file(
    input_path: Path,
    output_path: Path,
    *,
    project_id: str,
    trace_id: str | None,
    skip_bad_lines: bool,
    strict_events: bool = False,
    options: ConversionOptions | None = None,
) -> tuple[int, int]:
    rows, skipped = read_jsonl(input_path, skip_bad_lines)
    if not rows and skipped == 0:
        raise EmptyJsonlError("input JSONL is empty")

    spans = convert_events(
        rows,
        project_id,
        trace_id or str(uuid.uuid4()),
        force_trace_id=trace_id is not None,
        strict_events=strict_events,
        options=options,
    )

    write_spans(output_path, spans)
    return len(spans), skipped


def write_spans(output_path: Path, spans: list[Json]) -> None:
    """Validate and write one canonical HALO span per JSONL line."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as destination:
        for index, row in enumerate(spans, 1):
            validate_span(row, index)
            destination.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def convert_directory_files(
    files: list[Path],
    input_root: Path,
    output_root: Path,
    *,
    project_id: str,
    skip_bad_lines: bool,
    strict_events: bool = False,
    options: ConversionOptions | None = None,
) -> tuple[int, int, int, int]:
    """Convert a directory and merge separately logged subagent sessions.

    Main files retain their existing conversion. A detailed child session is
    attached only when a source ``run_subagent``/``call_subagent`` Tool event
    identifies it by ``child_session_id``. Dedicated child log outputs are
    suppressed after a successful merge to avoid duplicate standalone traces.
    """
    options = options or ConversionOptions()
    loaded: dict[Path, list[Json]] = {}
    skipped_by_file: dict[Path, int] = {}
    empty_files: set[Path] = set()

    for source in files:
        rows, skipped = read_jsonl(source, skip_bad_lines)
        skipped_by_file[source] = skipped
        if not rows and skipped == 0:
            empty_files.add(source)
            continue
        loaded[source] = rows

    candidates = subagent_session_candidates(loaded)
    outputs: dict[Path, list[Json]] = {}
    attached_by_source: dict[Path, set[str]] = {}
    consumed_sessions: set[str] = set()

    for source, rows in loaded.items():
        spans = convert_events(
            rows,
            project_id,
            str(uuid.uuid4()),
            strict_events=strict_events,
            options=options,
        )
        spans, attached = attach_linked_subagents(
            spans,
            rows,
            candidates,
            project_id,
            options=options,
        )
        outputs[source] = spans
        attached_by_source[source] = attached
        consumed_sessions.update(attached)

    execution_events = {
        "agent_start",
        "agent_end",
        "model_input",
        "model_output",
        "tool_call",
        "tool_result",
    }

    def is_dedicated_subagent_source(source: Path) -> bool:
        child_sessions = {
            session_id
            for session_id, candidate in candidates.items()
            if candidate["source"] == source and session_id in consumed_sessions
        }
        execution_rows = [
            row for row in loaded[source] if row["event"] in execution_events
        ]
        return bool(execution_rows) and all(
            normalized_key(row.get("agent_role") or "") == "subagent"
            or str(row.get("session_id") or "") in child_sessions
            for row in execution_rows
        )

    consumed_sources = {
        candidate["source"]
        for session_id, candidate in candidates.items()
        if session_id in consumed_sessions
        and is_dedicated_subagent_source(candidate["source"])
    }
    converted = 0
    skipped_total = sum(skipped_by_file.values())
    skipped_files = len(empty_files)
    merged_files = 0

    for source in files:
        destination = output_path_for(source, input_root, output_root)
        if source in empty_files:
            print(f"[skip] {source}: empty JSONL")
            continue
        if source in consumed_sources:
            if destination.exists():
                destination.unlink()
            merged_files += 1
            print(f"[merge] {source}: attached to parent trace; standalone output omitted")
            continue

        spans = outputs[source]
        write_spans(destination, spans)
        converted += len(spans)
        print(
            f"[ok] {source} -> {destination} "
            f"spans={len(spans)} skipped={skipped_by_file[source]} "
            f"subagents={len(attached_by_source[source])}"
        )

    return converted, skipped_total, skipped_files, merged_files


def iter_jsonl_files(input_dir: Path, output_dir: Path) -> list[Path]:
    output_dir = output_dir.resolve()
    files: list[Path] = []
    for path in sorted(input_dir.rglob("*.jsonl")):
        resolved = path.resolve()
        try:
            resolved.relative_to(output_dir)
        except ValueError:
            files.append(path)
    return files


def output_path_for(input_path: Path, input_root: Path, output_root: Path) -> Path:
    return output_root / input_path.relative_to(input_root)


def default_output_path(input_path: Path) -> Path:
    if input_path.is_file():
        return input_path.with_name(f"{input_path.stem}.halo{input_path.suffix}")
    return input_path.with_name(f"{input_path.name}-halo-traces")
