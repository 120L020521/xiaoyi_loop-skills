"""Public API for the converter implementation."""

from .cli import main
from .content import halo_time
from .conversion import convert_events
from .io import (
    convert_file,
    default_output_path,
    iter_jsonl_files,
    output_path_for,
    read_jsonl,
)
from .models import ConversionOptions
from .validation import validate_input, validate_span, validate_trace_graph

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
