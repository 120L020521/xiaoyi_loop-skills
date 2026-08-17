"""Map prepared HALO spans back to verbatim pre-conversion JSONL events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OUTCOME_EVENTS = {
    "agent_end",
    "model_output",
    "session_ended",
    "subagent_completed",
    "tool_result",
}


@dataclass(frozen=True)
class SourceEvidence:
    trace_id: str
    span_id: str
    span_index: int
    source_line_numbers: tuple[int, ...]
    candidates: tuple[str, ...]
    outcome_candidates: tuple[str, ...]


def _read_jsonl(path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    rows: list[tuple[int, str, dict[str, Any]]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            raw = raw_line.rstrip("\r\n")
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSONL contains invalid JSON at line {line_number}: {path}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"JSONL line {line_number} must be a JSON object: {path}"
                )
            rows.append((line_number, raw, value))
    return rows


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _json_array(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        decoded = value
    elif isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        return []
    return [item for item in decoded if isinstance(item, dict)]


def _same_context(row: dict[str, Any], context: dict[str, Any]) -> bool:
    for field in ("event", "timestamp", "session_id", "agent_role"):
        expected = context.get(field)
        if expected not in (None, "") and row.get(field) != expected:
            return False
    return True


def _matching_source_lines(
    rows: list[tuple[int, str, dict[str, Any]]],
    context: dict[str, Any],
    *,
    tool_call_id: str | None,
) -> list[int]:
    direct = context.get("_halo_source_line")
    if isinstance(direct, int) and direct >= 1:
        for line_number, _raw, row in rows:
            if line_number == direct and _same_context(row, context):
                return [line_number]

    matches: list[int] = []
    for line_number, _raw, row in rows:
        if not _same_context(row, context):
            continue
        event = str(context.get("event") or "")
        if tool_call_id and event in {"tool_call", "tool_result"}:
            payload = row.get("payload")
            if not isinstance(payload, dict) or str(payload.get("tool_call_id") or "") != tool_call_id:
                continue
        matches.append(line_number)
    return matches


def _contiguous_candidates(
    rows_by_line: dict[int, tuple[str, dict[str, Any]]],
    line_numbers: set[int],
) -> list[str]:
    if not line_numbers:
        return []
    candidates: list[str] = []
    ordered = sorted(line_numbers)
    run: list[int] = []
    for line_number in ordered:
        if run and line_number != run[-1] + 1:
            candidates.append("\n".join(rows_by_line[number][0] for number in run))
            run = []
        run.append(line_number)
        candidates.append(rows_by_line[line_number][0])
    if run:
        candidates.append("\n".join(rows_by_line[number][0] for number in run))
    return list(dict.fromkeys(candidates))


def build_source_evidence(
    source_path: Path,
    prepared_path: Path,
) -> dict[tuple[str, str], SourceEvidence]:
    """Build source-log candidates and trace-local indexes for every prepared span."""
    source_rows = _read_jsonl(source_path)
    prepared_rows = _read_jsonl(prepared_path)
    if not source_rows:
        raise ValueError(f"source trace contains no JSON objects: {source_path}")
    source_is_span = all(
        isinstance(row.get("trace_id"), str) and isinstance(row.get("span_id"), str)
        for _line, _raw, row in source_rows
    )
    rows_by_line = {
        line_number: (raw, row) for line_number, raw, row in source_rows
    }
    source_span_lines: dict[str, list[int]] = {}
    if source_is_span:
        for line_number, _raw, row in source_rows:
            source_span_lines.setdefault(str(row["span_id"]), []).append(line_number)

    trace_counts: dict[str, int] = {}
    mapped: dict[tuple[str, str], SourceEvidence] = {}
    for _prepared_line, _prepared_raw, span in prepared_rows:
        trace_id = span.get("trace_id")
        span_id = span.get("span_id")
        if not isinstance(trace_id, str) or not trace_id or not isinstance(span_id, str) or not span_id:
            continue
        span_index = trace_counts.get(trace_id, 0)
        trace_counts[trace_id] = span_index + 1
        line_numbers: set[int] = set()
        if source_is_span:
            line_numbers.update(source_span_lines.get(span_id, []))
        else:
            attributes = span.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            tool_call_id = attributes.get("tool.call_id")
            tool_call_id = str(tool_call_id) if tool_call_id not in (None, "") else None
            contexts: list[dict[str, Any]] = []
            for name, value in attributes.items():
                if str(name).startswith("source.") and str(name).endswith(".context"):
                    context = _json_object(value)
                    if context is not None:
                        contexts.append(context)
            contexts.extend(_json_array(attributes.get("source.events")))
            for context in contexts:
                line_numbers.update(
                    _matching_source_lines(
                        source_rows,
                        context,
                        tool_call_id=tool_call_id,
                    )
                )

        candidates = _contiguous_candidates(rows_by_line, line_numbers)
        outcome_lines: list[str] = []
        for line_number in sorted(line_numbers):
            raw, row = rows_by_line[line_number]
            if source_is_span or str(row.get("event") or "") in OUTCOME_EVENTS:
                outcome_lines.append(raw)
        mapped[(trace_id, span_id)] = SourceEvidence(
            trace_id=trace_id,
            span_id=span_id,
            span_index=span_index,
            source_line_numbers=tuple(sorted(line_numbers)),
            candidates=tuple(candidates),
            outcome_candidates=tuple(dict.fromkeys(outcome_lines)),
        )
    return mapped


def choose_source_excerpt(
    evidence: SourceEvidence,
    *,
    max_chars: int,
    pattern: str | None = None,
    context_buffer_chars: int = 800,
) -> str:
    """Choose one contiguous source window, preferring input+outcome context."""
    if not evidence.candidates:
        raise ValueError(
            f"no pre-conversion source events map to span: {evidence.span_id}"
        )
    outcome_candidates = [
        candidate
        for candidate in evidence.candidates
        if any(outcome in candidate for outcome in evidence.outcome_candidates)
    ]
    pool = outcome_candidates or list(evidence.candidates)
    selected = max(pool, key=len)
    if len(selected) <= max_chars:
        return selected

    match = None
    if pattern:
        match = re.search(pattern, selected, flags=re.IGNORECASE)
    if match is None:
        match = re.search(
            r"STATUS_CODE_ERROR|success\\?\"?\s*[:=]\s*false|"
            r"status\\?\"?\s*[:=]\s*\\?\"?(?:failed|error)|"
            r"err(?:or|msg|code)|exception|exitCode\\?\"?\s*[:=]",
            selected,
            flags=re.IGNORECASE,
        )
    anchor = match.start() if match is not None else len(selected)
    start = max(0, min(anchor - max(0, context_buffer_chars), len(selected) - max_chars))
    return selected[start : start + max_chars]
