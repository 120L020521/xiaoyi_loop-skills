"""TraceStore: lazy-indexed OTel JSONL trace storage and querying.

Design notes (per spec):
- The JSONL file is never loaded wholesale into memory. At index time we read
  every line once, record (trace_id, byte offset, byte length), and parse only
  lightweight per-span metadata (ids/name/status/times/tokens/model/service/
  agent). Full attributes are parsed on demand by seek()-ing back to the raw
  line.
- Attribute truncation: discovery reads (view_trace / search results) cap each
  attribute at 4KB; surgical reads (view_spans) at 16KB. Long values keep
  their head and get a ``... [HALO truncated: original N chars]`` marker.
- view_trace / view_spans responses have a 150KB total byte budget; when the
  serialized response would exceed it, an ``oversized`` summary is returned
  instead of spans.
- OpenInference flat projection keys (``llm.input_messages.0.*``,
  ``llm.output_messages.0.*``, ``mcp.tools.0.*``) are dropped from views; a
  ``__halo_dropped_flat_projections`` attribute records what was dropped.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .models import SpanMatchRecord, SpanMeta, TraceFilters, TraceSummary

# --- Truncation / budget constants (semantics copied from the reference) ---
_DISCOVERY_ATTR_TRUNCATION_CHARS = 4096
_SURGICAL_ATTR_TRUNCATION_CHARS = 16384
_VIEW_RESPONSE_BYTES_BUDGET = 150_000
_INDEX_CACHE_SCHEMA_VERSION = 1

_TRUNCATION_MARKER = "... [HALO truncated: original {n} chars]"

# OpenInference flat projection keys to drop from attribute views.
_FLAT_PROJECTION_RE = re.compile(r"^(?:llm\.(?:input|output)_messages|mcp\.tools)\.\d+\.")

# Attribute keys that may carry token counts / model names.
_PROMPT_TOKEN_KEYS = (
    "inference.llm.input_tokens",
    "llm.token_count.prompt",
    "llm.token_count.input",
)
_COMPLETION_TOKEN_KEYS = (
    "inference.llm.output_tokens",
    "llm.token_count.completion",
    "llm.token_count.output",
)
_MODEL_KEYS = (
    "inference.llm.model_name",
    "llm.model_name",
    "gen_ai.request.model",
    "model_name",
)

_AGENT_ATTR_KEYS = (
    "inference.agent_name",
    "agent.id",
    "agent.name",
    "agent_id",
    "agent_name",
)


def _get(d: dict[str, Any], *paths: str, default: Any = None) -> Any:
    """Tolerant nested getter. Each path is a dotted string; the first path
    that resolves to a non-None value wins."""
    for path in paths:
        cur: Any = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def _truncate_text(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + _TRUNCATION_MARKER.format(n=len(s))


def _truncate_attr_value(value: Any, limit: int) -> Any:
    """Truncate a single attribute value to ``limit`` chars (head kept)."""
    if isinstance(value, str):
        return _truncate_text(value, limit)
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(value)
    return _truncate_text(serialized, limit)


def _process_attributes(attrs: Any, limit: int) -> dict[str, Any]:
    """Drop OpenInference flat projection keys and truncate long values."""
    if not isinstance(attrs, dict):
        return {}
    dropped: list[str] = []
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if _FLAT_PROJECTION_RE.match(key):
            dropped.append(key)
            continue
        out[key] = _truncate_attr_value(value, limit)
    if dropped:
        preview = ", ".join(dropped[:20])
        more = f" (+{len(dropped) - 20} more)" if len(dropped) > 20 else ""
        out["__halo_dropped_flat_projections"] = (
            f"dropped {len(dropped)} OpenInference flat projection keys: {preview}{more}"
        )
    return out


def _nano_to_iso(value: Any) -> Optional[str]:
    """Convert a unix-nanosecond timestamp (int or str) to ISO 8601 UTC."""
    try:
        ns = int(value)
    except (TypeError, ValueError):
        return None
    try:
        iso = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()
        return iso.replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _TraceIndex:
    """In-memory per-trace aggregate built at index time."""

    __slots__ = (
        "trace_id",
        "spans",
        "raw_jsonl_bytes",
        "has_errors",
        "otel_error_span_count",
        "service_names",
        "model_names",
        "agent_names",
        "project_id",
        "total_input_tokens",
        "total_output_tokens",
        "start_time",
        "end_time",
    )

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.spans: list[SpanMeta] = []
        self.raw_jsonl_bytes = 0
        self.has_errors = False
        self.otel_error_span_count = 0
        self.service_names: set[str] = set()
        self.model_names: set[str] = set()
        self.agent_names: set[str] = set()
        self.project_id: Optional[str] = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.start_time: Optional[str] = None
        self.end_time: Optional[str] = None

    def add(self, meta: SpanMeta) -> None:
        self.spans.append(meta)
        self.raw_jsonl_bytes += meta.line_length
        if meta.is_error:
            self.has_errors = True
            self.otel_error_span_count += 1
        if meta.service_name:
            self.service_names.add(meta.service_name)
        if meta.model_name:
            self.model_names.add(meta.model_name)
        if self.project_id is None and meta.project_id:
            self.project_id = meta.project_id
        self.agent_names.update(meta.agent_names)
        self.total_input_tokens += meta.input_tokens
        self.total_output_tokens += meta.output_tokens
        if meta.start_time and (self.start_time is None or meta.start_time < self.start_time):
            self.start_time = meta.start_time
        if meta.end_time and (self.end_time is None or meta.end_time > self.end_time):
            self.end_time = meta.end_time

    def summary(self) -> TraceSummary:
        return TraceSummary(
            trace_id=self.trace_id,
            span_count=len(self.spans),
            start_time=self.start_time,
            end_time=self.end_time,
            has_errors=self.has_errors,
            service_names=sorted(self.service_names),
            model_names=sorted(self.model_names),
            agent_names=sorted(self.agent_names),
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            raw_jsonl_bytes=self.raw_jsonl_bytes,
        )


class TraceStore:
    """Lazy index over an OTel-shaped JSONL trace file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.index_cache_path = f"{path}.halo-rlm-index.json"
        self._traces: dict[str, _TraceIndex] = {}
        self._trace_order: list[str] = []  # file order of first appearance
        self._total_spans = 0
        self._skipped_lines = 0
        if not self._load_index_cache():
            self._index()
            self._write_index_cache()

    def _source_fingerprint(self) -> tuple[int, int]:
        stat = os.stat(self.path)
        return stat.st_size, stat.st_mtime_ns

    def _load_index_cache(self) -> bool:
        try:
            with open(self.index_cache_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            source_size, source_mtime_ns = self._source_fingerprint()
            if (
                payload.get("schema_version") != _INDEX_CACHE_SCHEMA_VERSION
                or payload.get("source_size") != source_size
                or payload.get("source_mtime_ns") != source_mtime_ns
            ):
                return False
            spans = payload.get("spans")
            if not isinstance(spans, list) or not all(
                isinstance(raw, dict) for raw in spans
            ):
                return False
            metas = [SpanMeta(**raw) for raw in spans]
            for meta in metas:
                self._add_meta(meta)
            self._skipped_lines = int(payload.get("skipped_lines", 0))
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._traces.clear()
            self._trace_order.clear()
            self._total_spans = 0
            self._skipped_lines = 0
            return False

    def _write_index_cache(self) -> None:
        try:
            source_size, source_mtime_ns = self._source_fingerprint()
            payload = {
                "schema_version": _INDEX_CACHE_SCHEMA_VERSION,
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
                "skipped_lines": self._skipped_lines,
                "spans": [
                    asdict(meta)
                    for trace_id in self._trace_order
                    for meta in self._traces[trace_id].spans
                ],
            }
            tmp_path = (
                f"{self.index_cache_path}.tmp-{os.getpid()}-"
                f"{id(self)}"
            )
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp_path, self.index_cache_path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        except OSError:
            # Read-only trace directories remain supported; the next process
            # simply rebuilds the in-memory index.
            return

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _index(self) -> None:
        offset = 0
        with open(self.path, "rb") as f:
            for line in f:
                line_len = len(line)
                stripped = line.strip()
                if stripped:
                    try:
                        span = json.loads(stripped.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        span = None
                    if isinstance(span, dict):
                        meta = self._extract_meta(span, offset, line_len)
                        if meta is not None:
                            self._add_meta(meta)
                        else:
                            self._skipped_lines += 1
                    else:
                        self._skipped_lines += 1
                offset += line_len

    def _extract_meta(self, span: dict[str, Any], offset: int, line_len: int) -> Optional[SpanMeta]:
        trace_id = _get(span, "trace_id", "context.trace_id", "traceId", "context.traceId")
        if not trace_id:
            return None
        trace_id = str(trace_id)
        span_id = str(_get(span, "span_id", "spanId", "context.span_id", "context.spanId", default=""))
        parent_span_id = _get(
            span, "parent_span_id", "parentSpanId", "parent.span_id", "parent_id", default=None
        )
        if parent_span_id is not None:
            parent_span_id = str(parent_span_id)
        name = str(_get(span, "name", default=""))
        kind = _get(span, "kind", default=None)
        if isinstance(kind, int):
            kind = f"SPAN_KIND_{kind}"
        status_code = _get(span, "status.code", "status_code", "statusCode", default=None)
        status_message = _get(span, "status.message", "status_message", "statusMessage", default=None)
        if status_code is not None:
            status_code = str(status_code)
        if status_message is not None:
            status_message = str(status_message)

        start_time = _get(span, "start_time", "startTime", "start_time_iso", default=None)
        if start_time is None:
            start_time = _nano_to_iso(
                _get(span, "startTimeUnixNano", "start_time_unix_nano", default=None)
            )
        end_time = _get(span, "end_time", "endTime", "end_time_iso", default=None)
        if end_time is None:
            end_time = _nano_to_iso(
                _get(span, "endTimeUnixNano", "end_time_unix_nano", default=None)
            )
        if start_time is not None:
            start_time = str(start_time)
        if end_time is not None:
            end_time = str(end_time)

        attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
        resource_attrs = _get(span, "resource.attributes", default={})
        if not isinstance(resource_attrs, dict):
            resource_attrs = {}

        model_name = None
        for key in _MODEL_KEYS:
            if key in attrs and attrs[key] is not None:
                model_name = str(attrs[key])
                break

        service_name = resource_attrs.get("service.name") or attrs.get("service.name")
        if service_name is not None:
            service_name = str(service_name)

        project_id = attrs.get("inference.project_id")
        if project_id is not None:
            project_id = str(project_id)

        agent_names: list[str] = []
        for key in _AGENT_ATTR_KEYS:
            value = attrs.get(key)
            if isinstance(value, str) and value:
                agent_names.append(value)
        for key, value in attrs.items():
            if key.startswith("openinference.agent.") and isinstance(value, str) and value:
                agent_names.append(value)

        input_tokens = 0
        for key in _PROMPT_TOKEN_KEYS:
            if key in attrs:
                input_tokens = _to_int(attrs.get(key))
                break
        output_tokens = 0
        for key in _COMPLETION_TOKEN_KEYS:
            if key in attrs:
                output_tokens = _to_int(attrs.get(key))
                break

        return SpanMeta(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            kind=str(kind) if kind is not None else None,
            start_time=start_time,
            end_time=end_time,
            status_code=status_code,
            status_message=status_message,
            model_name=model_name,
            service_name=service_name,
            project_id=project_id,
            agent_names=agent_names,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            line_offset=offset,
            line_length=line_len,
        )

    def _add_meta(self, meta: SpanMeta) -> None:
        idx = self._traces.get(meta.trace_id)
        if idx is None:
            idx = _TraceIndex(meta.trace_id)
            self._traces[meta.trace_id] = idx
            self._trace_order.append(meta.trace_id)
        meta.span_index = len(idx.spans)
        idx.add(meta)
        self._total_spans += 1

    # ------------------------------------------------------------------
    # Raw line access (lazy, seek-based)
    # ------------------------------------------------------------------

    def _read_line(self, meta: SpanMeta) -> str:
        with open(self.path, "rb") as f:
            f.seek(meta.line_offset)
            raw = f.read(meta.line_length)
        return raw.decode("utf-8", "replace").rstrip("\n")

    def _read_span_json(self, meta: SpanMeta) -> dict[str, Any]:
        try:
            parsed = json.loads(self._read_line(meta))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _get_trace(self, trace_id: str) -> _TraceIndex:
        idx = self._traces.get(trace_id)
        if idx is None:
            raise KeyError(f"unknown trace_id: {trace_id!r}")
        return idx

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _iter_filtered(self, filters: Optional[TraceFilters]) -> Iterator[_TraceIndex]:
        filters = filters or TraceFilters()
        regex: Optional[re.Pattern[str]] = None
        if filters.regex_pattern:
            regex = re.compile(filters.regex_pattern)  # may raise re.error
        for trace_id in self._trace_order:
            idx = self._traces[trace_id]
            if filters.has_errors is not None and idx.has_errors != filters.has_errors:
                continue
            if filters.model_names and not (idx.model_names & set(filters.model_names)):
                continue
            if filters.service_names and not (idx.service_names & set(filters.service_names)):
                continue
            if filters.agent_names and not (idx.agent_names & set(filters.agent_names)):
                continue
            if filters.project_id is not None and idx.project_id != filters.project_id:
                continue
            if filters.start_time_gte is not None:
                if idx.start_time is None or idx.start_time < filters.start_time_gte:
                    continue
            if filters.end_time_lte is not None:
                if idx.end_time is None or idx.end_time > filters.end_time_lte:
                    continue
            if regex is not None and not self._trace_matches_regex(idx, regex):
                continue
            yield idx

    def _trace_matches_regex(self, idx: _TraceIndex, regex: re.Pattern[str]) -> bool:
        """Lazy raw-line scan: re-read each span line from disk."""
        for meta in idx.spans:
            try:
                if regex.search(self._read_line(meta)):
                    return True
            except OSError:
                continue
        return False

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_overview(self, filters: Optional[TraceFilters] = None) -> dict[str, Any]:
        total_traces = 0
        total_spans = 0
        error_trace_count = 0
        service_names: set[str] = set()
        model_names: set[str] = set()
        agent_names: set[str] = set()
        start_time: Optional[str] = None
        end_time: Optional[str] = None
        total_input_tokens = 0
        total_output_tokens = 0
        raw_jsonl_bytes = 0
        sample_trace_ids: list[str] = []

        for idx in self._iter_filtered(filters):
            total_traces += 1
            total_spans += len(idx.spans)
            if idx.has_errors:
                error_trace_count += 1
            service_names |= idx.service_names
            model_names |= idx.model_names
            agent_names |= idx.agent_names
            total_input_tokens += idx.total_input_tokens
            total_output_tokens += idx.total_output_tokens
            raw_jsonl_bytes += idx.raw_jsonl_bytes
            if idx.start_time and (start_time is None or idx.start_time < start_time):
                start_time = idx.start_time
            if idx.end_time and (end_time is None or idx.end_time > end_time):
                end_time = idx.end_time
            if len(sample_trace_ids) < 20:
                sample_trace_ids.append(idx.trace_id)

        return {
            "total_traces": total_traces,
            "total_spans": total_spans,
            "error_trace_count": error_trace_count,
            "service_names": sorted(service_names),
            "model_names": sorted(model_names),
            "agent_names": sorted(agent_names),
            "earliest_start_time": start_time or "",
            "latest_end_time": end_time or "",
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "raw_jsonl_bytes": raw_jsonl_bytes,
            "sample_trace_ids": sample_trace_ids,
        }

    def query_traces(
        self,
        filters: Optional[TraceFilters] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        matched = list(self._iter_filtered(filters))
        page = matched[offset : offset + limit]
        return {
            "traces": [idx.summary().to_dict() for idx in page],
            "total": len(matched),
        }

    def count_traces(self, filters: Optional[TraceFilters] = None) -> dict[str, Any]:
        return {"total": sum(1 for _ in self._iter_filtered(filters))}

    # ------------------------------------------------------------------
    # Views (truncated, budgeted)
    # ------------------------------------------------------------------

    def _view_span_dict(self, meta: SpanMeta, attr_limit: int) -> dict[str, Any]:
        span = self._read_span_json(meta)
        span["attributes"] = _process_attributes(span.get("attributes"), attr_limit)
        return span

    @staticmethod
    def _response_bytes(obj: Any) -> int:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _budget_check(
        self,
        idx: _TraceIndex,
        span_dicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return HALO's TraceView shape, using a nested oversized summary."""
        total_bytes = sum(self._response_bytes(span) for span in span_dicts)
        if total_bytes <= _VIEW_RESPONSE_BYTES_BUDGET:
            return {
                "trace_id": idx.trace_id,
                "spans": span_dicts,
                "oversized": None,
            }

        sizes = sorted(self._response_bytes(s) for s in span_dicts) or [0]
        median = sizes[len(sizes) // 2]
        top_names = Counter(m.name for m in idx.spans).most_common(10)
        oversized = {
            "trace_id": idx.trace_id,
            "span_count": len(span_dicts),
            "truncated_response_bytes": total_bytes,
            "response_bytes_budget": _VIEW_RESPONSE_BYTES_BUDGET,
            "span_response_bytes_min": sizes[0],
            "span_response_bytes_median": median,
            "span_response_bytes_max": sizes[-1],
            "top_span_names": [[name, count] for name, count in top_names],
            "error_span_count": idx.otel_error_span_count,
            "recommendation": (
                "Response exceeds the per-call byte budget; spans were not returned. "
                "Use search_trace(trace_id, regex_pattern) to locate relevant spans "
                "(bounded SpanMatchRecords), then view_spans(trace_id, "
                "span_ids=[...]) on a small surgical set. Do NOT retry the same "
                "view call."
            ),
        }
        return {
            "trace_id": idx.trace_id,
            "spans": [],
            "oversized": oversized,
        }

    def view_trace(self, trace_id: str) -> dict[str, Any]:
        idx = self._get_trace(trace_id)
        span_dicts = [
            self._view_span_dict(meta, _DISCOVERY_ATTR_TRUNCATION_CHARS) for meta in idx.spans
        ]
        return self._budget_check(idx, span_dicts)

    def view_spans(self, trace_id: str, span_ids: list[str]) -> dict[str, Any]:
        idx = self._get_trace(trace_id)
        if not isinstance(span_ids, list):
            raise ValueError("span_ids must be a list")
        if len(span_ids) > 200:
            raise ValueError(f"view_spans accepts at most 200 span ids (got {len(span_ids)})")
        wanted = {str(s) for s in span_ids}
        metas = [m for m in idx.spans if m.span_id in wanted]
        span_dicts = [
            self._view_span_dict(meta, _SURGICAL_ATTR_TRUNCATION_CHARS) for meta in metas
        ]
        return self._budget_check(idx, span_dicts)

    # ------------------------------------------------------------------
    # Search (regex over raw span lines, bounded results)
    # ------------------------------------------------------------------

    def _search_spans(
        self,
        trace_id: str,
        regex_pattern: str,
        context_buffer_chars: int = 100,
        max_matches: int = 50,
        span_id: Optional[str] = None,
    ) -> dict[str, Any]:
        idx = self._get_trace(trace_id)
        try:
            regex = re.compile(regex_pattern)
        except re.error as e:
            raise ValueError(f"invalid regex_pattern: {e}") from e
        context_buffer_chars = max(0, min(int(context_buffer_chars), 2_000))
        max_matches = max(1, min(int(max_matches), 500))

        metas = idx.spans
        if span_id is not None:
            metas = [m for m in idx.spans if m.span_id == span_id]
            if not metas:
                raise KeyError(f"unknown span_id {span_id!r} in trace {trace_id!r}")

        match_count = 0
        matches: list[SpanMatchRecord] = []
        for meta in metas:
            text = self._read_line(meta)
            for m in regex.finditer(text):
                match_count += 1
                if len(matches) < max_matches:
                    start, end = m.start(), m.end()
                    ctx_start = max(0, start - context_buffer_chars)
                    ctx_end = min(len(text), end + context_buffer_chars)
                    matches.append(
                        SpanMatchRecord(
                            trace_id=trace_id,
                            span_id=meta.span_id,
                            span_index=meta.span_index,
                            span_name=meta.name,
                            kind=meta.kind,
                            status_code=meta.status_code,
                            parent_span_id=meta.parent_span_id,
                            raw_jsonl_bytes=meta.line_length,
                            match_text=m.group(0),
                            matched_context=text[ctx_start:ctx_end],
                            match_start_char=start,
                            match_end_char=end,
                        )
                    )
        return {
            "trace_id": trace_id,
            "match_count": match_count,
            "returned_match_count": len(matches),
            "has_more": match_count > len(matches),
            "matches": [rec.to_dict() for rec in matches],
        }

    def search_trace(
        self,
        trace_id: str,
        regex_pattern: str,
        context_buffer_chars: int = 100,
        max_matches: int = 50,
    ) -> dict[str, Any]:
        return self._search_spans(
            trace_id, regex_pattern, context_buffer_chars, max_matches, span_id=None
        )

    def search_span(
        self,
        trace_id: str,
        span_id: str,
        regex_pattern: str,
        context_buffer_chars: int = 100,
        max_matches: int = 50,
    ) -> dict[str, Any]:
        return self._search_spans(
            trace_id, regex_pattern, context_buffer_chars, max_matches, span_id=span_id
        )

    # ------------------------------------------------------------------
    # Plain-text rendering (for synthesis)
    # ------------------------------------------------------------------

    def render_trace(self, trace_id: str, budget: int = 8000) -> str:
        idx = self._get_trace(trace_id)
        lines: list[str] = [
            f"trace_id: {idx.trace_id}",
            f"span_count: {len(idx.spans)}  has_errors: {idx.has_errors}  "
            f"raw_jsonl_bytes: {idx.raw_jsonl_bytes}",
            f"services: {sorted(idx.service_names)}  models: {sorted(idx.model_names)}  "
            f"agents: {sorted(idx.agent_names)}",
            f"tokens: input={idx.total_input_tokens} output={idx.total_output_tokens}  "
            f"start={idx.start_time}  end={idx.end_time}",
            "",
        ]
        for meta in idx.spans:
            err = ""
            if meta.is_error:
                err = f"  ERROR: {meta.status_message or ''}"
            lines.append(
                f"[{meta.span_index}] {meta.name} (span_id={meta.span_id}, "
                f"kind={meta.kind}, status={meta.status_code}){err}"
            )
            lines.append(
                f"    parent={meta.parent_span_id} start={meta.start_time} "
                f"end={meta.end_time} model={meta.model_name} "
                f"tokens(in={meta.input_tokens}, out={meta.output_tokens})"
            )
            if meta.agent_names:
                lines.append(f"    agents={meta.agent_names}")
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[:budget] + "\n... [truncated]"
        return text

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def trace_ids(self) -> list[str]:
        return list(self._trace_order)

    @property
    def total_spans(self) -> int:
        return self._total_spans

    @property
    def skipped_lines(self) -> int:
        return self._skipped_lines

    @property
    def file_size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
