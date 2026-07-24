"""Pair events, group runs/sessions, and orchestrate conversion."""

from __future__ import annotations

import uuid
import warnings

from .builders import (
    base_attrs,
    llm_span,
    span,
    tool_span,
    trace_id_from,
    unfinished_llm_span,
    unmatched_tool_span,
)
from .content import halo_time, source_attribute_value
from .models import ConversionOptions, Json
from .status import correlation_id, tool_call_id, tool_ids_from_model_output
from .validation import validate_input, validate_trace_graph


def _pop_correlated(pending: list[Json], value: str | None) -> Json | None:
    if not pending:
        return None
    if value is None:
        return pending.pop(0)
    for index, item in enumerate(pending):
        if item.get("correlation_id") == value:
            return pending.pop(index)
    return None


def _pop_tool_call(pending: list[Json], value: str | None) -> Json | None:
    if not pending:
        return None
    if value is None:
        for index, item in enumerate(pending):
            if item.get("call_id") is None:
                return pending.pop(index)
        return pending.pop(0)
    for index, item in enumerate(pending):
        if item.get("call_id") == value:
            return pending.pop(index)
    return None


def _split_runs(rows: list[Json]) -> list[list[Json]]:
    """Split runs without confusing subagent metadata with top-level boundaries."""
    has_explicit_runs = any(
        row["event"] == "agent_start"
        or row["payload"].get("run_id") not in (None, "")
        or row.get("run_id") not in (None, "")
        for row in rows
    )

    if not has_explicit_runs:
        # Legacy/subagent files can interleave lifecycle rows from one session
        # with model/tool rows from another. Group by session to preserve each
        # session's chronology and avoid tiny traces on every switch.
        grouped: dict[str, list[Json]] = {}
        fallback_key = "__missing_session__"
        for row in rows:
            session_value = row.get("session_id")
            key = (
                str(session_value)
                if session_value not in (None, "")
                else fallback_key
            )
            grouped.setdefault(key, []).append(row)
        return list(grouped.values())

    # Explicit agent runs stay intact even when subagent_completed rows carry a
    # child session_id. Only agent/run boundaries split them.
    groups: list[list[Json]] = []
    current: list[Json] = []
    current_run_id: str | None = None
    closed = False

    for row in rows:
        payload = row["payload"]
        run_value = payload.get("run_id") or row.get("run_id")
        run_id = str(run_value) if run_value not in (None, "") else None
        boundary = bool(
            current
            and (
                closed
                or row["event"] == "agent_start"
                or (run_id and current_run_id and run_id != current_run_id)
            )
        )
        if boundary:
            groups.append(current)
            current = []
            current_run_id = None
            closed = False

        current.append(row)
        if run_id:
            current_run_id = run_id
        if row["event"] == "agent_end":
            closed = True

    if current:
        groups.append(current)
    return groups


def _convert_run(
    rows: list[Json],
    project_id: str,
    default_trace_id: str,
    options: ConversionOptions,
    trace_id_override: str | None = None,
) -> list[Json]:
    trace_id = trace_id_override or trace_id_from(rows, default_trace_id)
    agent_span_id = str(uuid.uuid4())
    agent_role = str(rows[0].get("agent_role") or "main")
    child_spans: list[Json] = []
    pending_models: list[Json] = []
    pending_tools: list[Json] = []
    tool_parents: dict[str, str] = {}
    last_llm_span_id: str | None = None
    source_events: list[Json] = []
    agent_attrs = {
        **base_attrs(project_id, "AGENT"),
        "agent.name": agent_role,
        "inference.agent_name": agent_role,
        "session.id": str(rows[0].get("session_id") or ""),
    }

    for row in rows:
        event = row["event"]
        payload = row["payload"]
        if event not in {"model_input", "model_output", "tool_call", "tool_result"}:
            source_events.append(row)
        if event == "model_input":
            pending_models.append(
                {
                    "row": row,
                    "payload": payload,
                    "correlation_id": correlation_id(payload),
                }
            )
            last_llm_span_id = None
        elif event == "model_output":
            value = correlation_id(payload)
            pending_model = _pop_correlated(pending_models, value)
            if value and pending_model is None:
                warnings.warn(
                    f"model_output correlation id {value!r} has no matching model_input",
                    stacklevel=2,
                )
            llm = llm_span(
                row,
                pending_model,
                trace_id,
                agent_span_id,
                project_id,
                options,
            )
            child_spans.append(llm)
            last_llm_span_id = llm["span_id"]
            for current_tool_id in tool_ids_from_model_output(payload):
                tool_parents[current_tool_id] = llm["span_id"]
        elif event == "tool_call":
            call_id = tool_call_id(payload)
            pending_tools.append(
                {
                    "row": row,
                    "payload": payload,
                    "call_id": call_id,
                    "parent_span_id": (
                        tool_parents.get(call_id) if call_id else None
                    )
                    or last_llm_span_id
                    or agent_span_id,
                }
            )
        elif event == "tool_result":
            result_call_id = tool_call_id(payload)
            call = _pop_tool_call(pending_tools, result_call_id)
            effective_call_id = (
                result_call_id
                or (call.get("call_id") if call else None)
                or f"generated-{uuid.uuid4()}"
            )
            parent_span_id = (
                (call.get("parent_span_id") if call else None)
                or tool_parents.get(effective_call_id)
                or last_llm_span_id
                or agent_span_id
            )
            child_spans.append(
                tool_span(
                    row,
                    call,
                    trace_id,
                    parent_span_id,
                    project_id,
                    effective_call_id,
                    options,
                )
            )

    for pending_model in pending_models:
        child_spans.append(
            unfinished_llm_span(
                pending_model,
                trace_id,
                agent_span_id,
                project_id,
                options,
            )
        )
    for call in pending_tools:
        call_id = call.get("call_id") or f"generated-{uuid.uuid4()}"
        child_spans.append(
            unmatched_tool_span(
                call_id,
                call,
                trace_id,
                call.get("parent_span_id") or agent_span_id,
                project_id,
                options,
            )
        )

    if source_events:
        agent_attrs["source.events"] = source_attribute_value(source_events, options)
    agent_attrs["source.event_count"] = len(rows)
    _enclose_tool_children(child_spans)

    has_error = any(
        item["status"]["code"] == "STATUS_CODE_ERROR" for item in child_spans
    )
    event_times = [halo_time(row.get("timestamp")) for row in rows]
    start_time = min([*event_times, *(item["start_time"] for item in child_spans)])
    end_time = max([*event_times, *(item["end_time"] for item in child_spans)])
    root = span(
        trace_id=trace_id,
        span_id=agent_span_id,
        parent_span_id="",
        name=f"agent.{agent_role}",
        kind="SPAN_KIND_INTERNAL",
        start_time=start_time,
        end_time=end_time,
        status_code="STATUS_CODE_ERROR" if has_error else "STATUS_CODE_OK",
        status_message="one or more child spans failed" if has_error else "",
        attrs=agent_attrs,
        options=options,
    )
    return [root, *child_spans]


def _enclose_tool_children(child_spans: list[Json]) -> None:
    """Extend LLM intervals through their Tool descendants."""
    spans_by_id = {item["span_id"]: item for item in child_spans}
    for item in child_spans:
        if item["attributes"].get("inference.observation_kind") != "TOOL":
            continue
        parent = spans_by_id.get(item["parent_span_id"])
        if parent and parent["attributes"].get("inference.observation_kind") == "LLM":
            parent["start_time"] = min(parent["start_time"], item["start_time"])
            parent["end_time"] = max(parent["end_time"], item["end_time"])


def convert_events(
    rows: list[Json],
    project_id: str,
    default_trace_id: str,
    *,
    force_trace_id: bool = False,
    strict_events: bool = False,
    options: ConversionOptions | None = None,
) -> list[Json]:
    validate_input(rows, require_chronological=False)
    options = options or ConversionOptions()
    known_events = {
        "agent_start",
        "agent_end",
        "model_input",
        "model_output",
        "tool_call",
        "tool_result",
        "session_lifecycle",
        "subagent_completed",
    }
    unknown_events = sorted({row["event"] for row in rows} - known_events)
    if unknown_events:
        message = f"unsupported event types ignored: {', '.join(unknown_events)}"
        if strict_events:
            raise ValueError(message)
        warnings.warn(message, stacklevel=2)

    groups = _split_runs(rows)
    if force_trace_id and len(groups) != 1:
        raise ValueError("--trace-id cannot be used when the input contains multiple runs")

    converted: list[Json] = []
    for index, group in enumerate(groups):
        validate_input(group, require_chronological=True)
        fallback = default_trace_id if index == 0 else str(uuid.uuid4())
        converted.extend(
            _convert_run(
                group,
                project_id,
                fallback,
                options,
                trace_id_override=default_trace_id if force_trace_id else None,
            )
        )
    validate_trace_graph(converted)
    return converted
