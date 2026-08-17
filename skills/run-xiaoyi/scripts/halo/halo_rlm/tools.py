"""Tool registry: JSON schemas + executors for the HALO RLM engine.

Tools are registered as OpenAI function tools. Leaf tools are available at
every depth; ``call_subagent`` is only registered when depth < maximum_depth.

Every executor returns a JSON string. Tool failures (bad regex, unknown
trace_id, ...) are returned as ``{"error": "..."}`` results — they never raise
into the agent loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Callable, Optional

from .models import TraceFilters
from .prompts import SYNTHESIS_SYSTEM_PROMPT
from .trace_store import TraceStore

_RUN_CODE_TIMEOUT_SECONDS = 30
_RUN_CODE_OUTPUT_TRUNCATION_CHARS = 10_000


def _filters_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "has_errors": {
                "type": "boolean",
                "description": (
                    "Strict OTel semantics: keep only traces where at least one "
                    "span has status.code == STATUS_CODE_ERROR (or, when false, "
                    "traces with no such span)."
                ),
            },
            "model_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keep traces that use any of these model names.",
            },
            "service_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keep traces that include any of these service names.",
            },
            "agent_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keep traces that include any of these agent names.",
            },
            "project_id": {
                "type": "string",
                "description": "Keep traces whose inference.project_id matches exactly.",
            },
            "start_time_gte": {
                "type": "string",
                "description": "ISO 8601 lower bound on trace start time.",
            },
            "end_time_lte": {
                "type": "string",
                "description": "ISO 8601 upper bound on trace end time.",
            },
            "regex_pattern": {
                "type": "string",
                "description": (
                    "Scan-heavy filter: a trace matches if any of its raw span "
                    "JSONL lines matches this regex. Prefer indexed filters first."
                ),
            },
        },
        "additionalProperties": False,
    }


def _fn(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _no_params() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class ToolRegistry:
    """Per-agent tool registry and executor."""

    def __init__(
        self,
        store: TraceStore,
        llm_client: Any,
        synthesis_model: str,
        context: Any,
        depth: int,
        maximum_depth: int,
        subagent_handler: Optional[Callable[[str], dict[str, Any]]] = None,
        enable_unsafe_run_code: bool = False,
    ) -> None:
        self.store = store
        self.llm_client = llm_client
        self.synthesis_model = synthesis_model
        self.context = context
        self.depth = depth
        self.maximum_depth = maximum_depth
        self.subagent_handler = subagent_handler
        self.enable_unsafe_run_code = enable_unsafe_run_code

    # ------------------------------------------------------------------
    # Schemas
    # ------------------------------------------------------------------

    def schemas(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            _fn(
                "get_dataset_overview",
                "Dataset-level aggregate: trace/span counts, error trace count, "
                "service/model/agent name lists, time bounds, token totals, "
                "raw_jsonl_bytes, and up to 20 sample trace ids. Call this FIRST "
                "without regex_pattern.",
                {
                    "type": "object",
                    "properties": {"filters": _filters_schema()},
                    "additionalProperties": False,
                },
            ),
            _fn(
                "query_traces",
                "Paginated per-trace summaries (TraceSummary) including "
                "raw_jsonl_bytes, has_errors, token totals and error span counts.",
                {
                    "type": "object",
                    "properties": {
                        "filters": _filters_schema(),
                        "limit": {
                            "type": "integer",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                        "offset": {"type": "integer", "default": 0, "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            ),
            _fn(
                "count_traces",
                "Count traces matching the filters.",
                {
                    "type": "object",
                    "properties": {"filters": _filters_schema()},
                    "additionalProperties": False,
                },
            ),
            _fn(
                "view_trace",
                "View all spans of one trace. Per-attribute payloads are "
                "head-capped at ~4KB. Responses over the ~150KB byte budget "
                "return an oversized summary instead of spans.",
                {
                    "type": "object",
                    "properties": {"trace_id": {"type": "string"}},
                    "required": ["trace_id"],
                    "additionalProperties": False,
                },
            ),
            _fn(
                "view_spans",
                "Surgically view up to 200 spans of one trace. Per-attribute "
                "payloads are head-capped at ~16KB (4x the discovery cap).",
                {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                        "span_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 200,
                        },
                    },
                    "required": ["trace_id", "span_ids"],
                    "additionalProperties": False,
                },
            ),
            _fn(
                "search_trace",
                "Regex search across the raw JSONL lines of one trace. Returns "
                "bounded SpanMatchRecords (span metadata + matched text + "
                "surrounding context). If has_more=true, refine the regex.",
                {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                        "regex_pattern": {"type": "string"},
                        "context_buffer_chars": {
                            "type": "integer",
                            "default": 100,
                            "minimum": 0,
                            "maximum": 2000,
                        },
                        "max_matches": {
                            "type": "integer",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    "required": ["trace_id", "regex_pattern"],
                    "additionalProperties": False,
                },
            ),
            _fn(
                "search_span",
                "Regex search within a single span of a trace. Use for surgical "
                "reads of one large span.",
                {
                    "type": "object",
                    "properties": {
                        "trace_id": {"type": "string"},
                        "span_id": {"type": "string"},
                        "regex_pattern": {"type": "string"},
                        "context_buffer_chars": {
                            "type": "integer",
                            "default": 100,
                            "minimum": 0,
                            "maximum": 2000,
                        },
                        "max_matches": {
                            "type": "integer",
                            "default": 50,
                            "minimum": 1,
                            "maximum": 500,
                        },
                    },
                    "required": ["trace_id", "span_id", "regex_pattern"],
                    "additionalProperties": False,
                },
            ),
            _fn(
                "synthesize_traces",
                "Render each trace (bounded plain text) and synthesize a short "
                "cross-trace summary with a dedicated synthesis model.",
                {
                    "type": "object",
                    "properties": {
                        "trace_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "focus": {"type": "string"},
                    },
                    "required": ["trace_ids"],
                    "additionalProperties": False,
                },
            ),
            _fn(
                "get_context_item",
                "Retrieve the full stored content (original text plus "
                "compaction summary) of one item from your own conversation "
                "context, by item id (as shown in 'Compacted ... (id: X)' markers).",
                {
                    "type": "object",
                    "properties": {"item_id": {"type": "string"}},
                    "required": ["item_id"],
                    "additionalProperties": False,
                },
            ),
        ]
        if self.enable_unsafe_run_code:
            tools.append(
                _fn(
                    "run_code",
                    "UNSAFE host Python execution with a 30s timeout. This is not "
                    "the Deno/Pyodide sandbox used by HALOAgent and must be enabled "
                    "explicitly.",
                    {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                        "additionalProperties": False,
                    },
                )
            )
        if self.depth < self.maximum_depth:
            tools.append(
                _fn(
                    "call_subagent",
                    "Delegate a well-defined multi-turn subtask to a subagent. "
                    "The subagent gets the same trace tools and returns a JSON "
                    "result {child_agent_id, answer, turns_used, tool_calls_made}.",
                    {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "Self-contained task description for the subagent.",
                            }
                        },
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                )
            )
        return tools

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one tool call; always returns a JSON string, never raises."""
        try:
            result = self._execute(name, arguments or {})
        except Exception as e:  # noqa: BLE001 - tools must not break the loop
            result = {"error": f"{type(e).__name__}: {e}"}
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({"error": "tool result not JSON-serializable"})

    def _execute(self, name: str, args: dict[str, Any]) -> Any:
        if name == "get_dataset_overview":
            return {
                "result": self.store.get_overview(
                    TraceFilters.from_dict(args.get("filters"))
                )
            }
        if name == "query_traces":
            return {
                "result": self.store.query_traces(
                    TraceFilters.from_dict(args.get("filters")),
                    limit=int(args.get("limit", 50)),
                    offset=int(args.get("offset", 0)),
                )
            }
        if name == "count_traces":
            return {
                "result": self.store.count_traces(
                    TraceFilters.from_dict(args.get("filters"))
                )
            }
        if name == "view_trace":
            return {
                "result": self.store.view_trace(
                    self._required_str(args, "trace_id")
                )
            }
        if name == "view_spans":
            span_ids = args.get("span_ids")
            if not isinstance(span_ids, list) or not all(
                isinstance(s, str) for s in span_ids
            ):
                raise ValueError("span_ids must be a list of strings")
            return {
                "result": self.store.view_spans(
                    self._required_str(args, "trace_id"), span_ids
                )
            }
        if name == "search_trace":
            return {
                "result": self.store.search_trace(
                    self._required_str(args, "trace_id"),
                    self._required_str(args, "regex_pattern"),
                    context_buffer_chars=int(args.get("context_buffer_chars", 100)),
                    max_matches=int(args.get("max_matches", 50)),
                )
            }
        if name == "search_span":
            return {
                "result": self.store.search_span(
                    self._required_str(args, "trace_id"),
                    self._required_str(args, "span_id"),
                    self._required_str(args, "regex_pattern"),
                    context_buffer_chars=int(args.get("context_buffer_chars", 100)),
                    max_matches=int(args.get("max_matches", 50)),
                )
            }
        if name == "synthesize_traces":
            return self._synthesize_traces(args)
        if name == "get_context_item":
            return self._get_context_item(self._required_str(args, "item_id"))
        if name == "run_code":
            if not self.enable_unsafe_run_code:
                raise ValueError(
                    "run_code is disabled because this implementation is not a "
                    "security sandbox"
                )
            return self._run_code(self._required_str(args, "code"))
        if name == "call_subagent":
            if self.depth >= self.maximum_depth or self.subagent_handler is None:
                # Defensive check; the tool is not even registered at max depth.
                raise ValueError(
                    f"call_subagent unavailable at depth={self.depth} "
                    f"(maximum_depth={self.maximum_depth})"
                )
            return self.subagent_handler(self._required_str(args, "input"))
        raise ValueError(f"unknown tool: {name}")

    @staticmethod
    def _required_str(args: dict[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"missing or invalid required argument: {key}")
        return value

    # ------------------------------------------------------------------
    # Composite tools
    # ------------------------------------------------------------------

    def _synthesize_traces(self, args: dict[str, Any]) -> dict[str, Any]:
        trace_ids = args.get("trace_ids")
        if not isinstance(trace_ids, list) or not trace_ids:
            raise ValueError("trace_ids must be a non-empty list of strings")
        focus = args.get("focus")
        parts: list[str] = [f"trace_ids: {trace_ids}"]
        if focus:
            parts.append(f"focus: {focus}")
        for tid in trace_ids:
            try:
                rendered = self.store.render_trace(str(tid), budget=8000)
            except KeyError:
                rendered = f"[unknown trace_id: {tid}]"
            parts.append(f"--- trace {tid} ---\n{rendered}")
        user_text = "\n\n".join(parts)
        result = self.llm_client.chat(
            messages=[
                {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            model=self.synthesis_model,
        )
        return {"summary": result.content or ""}

    def _get_context_item(self, item_id: str) -> dict[str, Any]:
        if self.context is None:
            raise ValueError("no agent context available")
        item = self.context.get_item(item_id)
        if item is None:
            raise KeyError(f"unknown context item_id: {item_id!r}")
        return item.to_dict()

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    @classmethod
    def _run_code(cls, code: str) -> dict[str, Any]:
        """Semi-sandboxed execution: a plain subprocess with a timeout and
        truncated output. NOT a security boundary."""
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                tmp_path = f.name
            try:
                proc = subprocess.run(
                    [sys.executable, tmp_path],
                    capture_output=True,
                    text=True,
                    timeout=_RUN_CODE_TIMEOUT_SECONDS,
                    cwd=tempfile.gettempdir(),
                )
                stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired as e:
                stdout = cls._coerce_text(e.stdout)
                stderr = cls._coerce_text(e.stderr) + (
                    f"\n[run_code: killed after {_RUN_CODE_TIMEOUT_SECONDS}s timeout]"
                )
                exit_code = -1
            return {
                "stdout": (stdout or "")[:_RUN_CODE_OUTPUT_TRUNCATION_CHARS],
                "stderr": (stderr or "")[:_RUN_CODE_OUTPUT_TRUNCATION_CHARS],
                "exit_code": exit_code,
            }
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
