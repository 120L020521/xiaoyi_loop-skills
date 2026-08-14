"""Local, API-free trace primitives for the host-agent HALO workflow."""

from .trace_store import TraceStore
from .models import TraceFilters, TraceSummary, SpanMatchRecord

__all__ = [
    "TraceStore",
    "TraceFilters",
    "TraceSummary",
    "SpanMatchRecord",
]

__version__ = "0.1.0"
