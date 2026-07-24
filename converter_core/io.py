"""Read and write JSONL files and resolve input/output paths."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from .conversion import convert_events
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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as destination:
        for index, row in enumerate(spans, 1):
            validate_span(row, index)
            destination.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return len(spans), skipped


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
