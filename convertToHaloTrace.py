"""Convert event+payload JSONL logs into HALO-compatible span JSONL.

This is the stable command-line and import entry point. The implementation
lives in the ``converter_core`` package.
"""

from __future__ import annotations

from converter_core import (
    ConversionOptions,
    convert_events,
    convert_file,
    default_output_path,
    halo_time,
    iter_jsonl_files,
    main,
    output_path_for,
    read_jsonl,
    validate_input,
    validate_span,
    validate_trace_graph,
)

__all__ = [
    "ConversionOptions",
    "convert_events",
    "convert_file",
    "default_output_path",
    "halo_time",
    "iter_jsonl_files",
    "main",
    "output_path_for",
    "read_jsonl",
    "validate_input",
    "validate_span",
    "validate_trace_graph",
]


if __name__ == "__main__":
    raise SystemExit(main())
