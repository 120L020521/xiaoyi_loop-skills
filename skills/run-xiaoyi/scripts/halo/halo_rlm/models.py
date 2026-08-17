"""Dataclasses shared across the HALO RLM engine.

Pure standard library. These model the parsed span metadata, per-trace
summaries, query filters, and search match records returned by TraceStore.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass
class TraceFilters:
    """Filters accepted by overview / query / count.

    All fields optional. ``regex_pattern`` is the only scan-heavy filter: it is
    applied lazily by re-reading raw JSONL lines from disk.
    """

    has_errors: Optional[bool] = None
    model_names: Optional[list[str]] = None
    service_names: Optional[list[str]] = None
    agent_names: Optional[list[str]] = None
    project_id: Optional[str] = None
    start_time_gte: Optional[str] = None
    end_time_lte: Optional[str] = None
    regex_pattern: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "TraceFilters":
        if not d:
            return cls()
        allowed = {
            "has_errors",
            "model_names",
            "service_names",
            "agent_names",
            "project_id",
            "start_time_gte",
            "end_time_lte",
            "regex_pattern",
        }
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(
                "unknown trace filter field(s): " + ", ".join(sorted(unknown))
            )
        kwargs = {k: v for k, v in d.items() if k in allowed and v is not None}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Span metadata (kept in memory at index time; attributes are NOT kept here)
# ---------------------------------------------------------------------------


@dataclass
class SpanMeta:
    """Lightweight per-span metadata parsed at index time."""

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    kind: Optional[str]
    start_time: Optional[str]
    end_time: Optional[str]
    status_code: Optional[str]
    status_message: Optional[str]
    model_name: Optional[str]
    service_name: Optional[str]
    project_id: Optional[str]
    agent_names: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    span_index: int = 0  # index of this span within its trace (file order)
    line_offset: int = 0  # byte offset of the raw JSONL line in the file
    line_length: int = 0  # byte length of the raw JSONL line (incl. newline)

    @property
    def is_error(self) -> bool:
        return self.status_code == "STATUS_CODE_ERROR"


# ---------------------------------------------------------------------------
# Trace summary
# ---------------------------------------------------------------------------


@dataclass
class TraceSummary:
    trace_id: str
    span_count: int
    start_time: Optional[str]
    end_time: Optional[str]
    has_errors: bool
    service_names: list[str]
    model_names: list[str]
    agent_names: list[str]
    total_input_tokens: int
    total_output_tokens: int
    raw_jsonl_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Search match record
# ---------------------------------------------------------------------------


@dataclass
class SpanMatchRecord:
    trace_id: str
    span_id: str
    span_index: int
    span_name: str
    kind: Optional[str]
    status_code: Optional[str]
    parent_span_id: Optional[str]
    raw_jsonl_bytes: int
    match_text: str
    matched_context: str
    match_start_char: int
    match_end_char: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Tool call / result envelopes (used by engine + tools)
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single tool call requested by the LLM."""

    id: str
    name: str
    arguments_json: str  # raw JSON string of arguments

    def arguments(self) -> dict[str, Any]:
        import json

        try:
            parsed = json.loads(self.arguments_json or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments_json or "{}"},
        }


@dataclass
class ChatResult:
    """Normalized result of one chat.completions call."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
