"""Command-line interface for the converter."""

from __future__ import annotations

import argparse
from pathlib import Path

from .io import (
    EmptyJsonlError,
    convert_directory_files,
    convert_file,
    default_output_path,
    iter_jsonl_files,
)
from .models import ConversionOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert event+payload JSONL logs into HALO-compatible span JSONL. "
            "INPUT may be one file or a directory."
        )
    )
    parser.add_argument("input", type=Path, help="Input event-stream JSONL file or directory")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=(
            "Output traces.jsonl file or output directory. Defaults to "
            "<input>.halo.jsonl for files, or <input-dir>-halo-traces for directories."
        ),
    )
    parser.add_argument("--project-id", default="converted trace")
    parser.add_argument(
        "--trace-id",
        default=None,
        help="Force all rows into this trace_id. Only allowed for single-file input.",
    )
    parser.add_argument("--skip-bad-lines", action="store_true")
    parser.add_argument(
        "--strict-events",
        action="store_true",
        help="Fail instead of warning when an unsupported event type is present.",
    )
    parser.add_argument(
        "--max-attribute-chars",
        type=int,
        default=0,
        help="Maximum characters per string attribute; 0 keeps complete content (default: 0).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_attribute_chars < 0:
        raise ValueError("--max-attribute-chars must be zero or greater")
    options = ConversionOptions(max_attribute_chars=args.max_attribute_chars)

    input_path = args.input
    output_path = args.output or default_output_path(input_path)
    if input_path.is_file():
        final_output = (
            output_path / input_path.name
            if output_path.exists() and output_path.is_dir()
            else output_path
        )
        converted, skipped = convert_file(
            input_path,
            final_output,
            project_id=args.project_id,
            trace_id=args.trace_id,
            skip_bad_lines=args.skip_bad_lines,
            strict_events=args.strict_events,
            options=options,
        )
        print(f"files=1 converted={converted} skipped={skipped} output={final_output}")
        return 0

    if not input_path.is_dir():
        raise FileNotFoundError(f"input path not found: {input_path}")
    if args.trace_id:
        raise ValueError("--trace-id can only be used with a single input file")

    files = iter_jsonl_files(input_path, output_path)
    if not files:
        print(f"files=0 converted=0 skipped=0 output_dir={output_path}")
        return 0

    total_converted, total_skipped, skipped_files, merged_files = (
        convert_directory_files(
            files,
            input_path,
            output_path,
            project_id=args.project_id,
            skip_bad_lines=args.skip_bad_lines,
            strict_events=args.strict_events,
            options=options,
        )
    )

    print(
        f"files={len(files)} converted={total_converted} "
        f"skipped={total_skipped} skipped_files={skipped_files} "
        f"merged_files={merged_files} "
        f"output_dir={output_path}"
    )
    return 0
