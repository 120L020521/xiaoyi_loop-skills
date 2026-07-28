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
from .content import halo_time, normalized_key, source_attribute_value
from .models import ConversionOptions, Json
from .status import (
    correlation_id,
    event_status,
    tool_call_id,
)
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


def _agent_terminal_outcome(
    rows: list[Json],
) -> tuple[bool, str] | None:
    """Return an explicit agent outcome as ``(failed, message)``."""
    success_states = {
        "complete",
        "completed",
        "ok",
        "success",
        "succeeded",
    }
    for row in reversed(rows):
        if row["event"] != "agent_end":
            continue
        payload = row["payload"]
        value = payload.get("status")
        if value in (None, ""):
            value = payload.get("state")
        terminal_status = normalized_key(value) if value not in (None, "") else ""

        error_message, failed = event_status(payload)
        if failed:
            return True, error_message
        if terminal_status in success_states:
            return False, ""
        for key in ("success", "ok"):
            boolean = payload.get(key)
            if boolean is True or (
                isinstance(boolean, str) and boolean.strip().lower() == "true"
            ):
                return False, ""
        return None
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
    root_parent_span_id: str = "",
    agent_name_override: str | None = None,
) -> list[Json]:
    trace_id = trace_id_override or trace_id_from(rows, default_trace_id)
    agent_span_id = str(uuid.uuid4())
    agent_role = agent_name_override or str(rows[0].get("agent_role") or "main")
    child_spans: list[Json] = []
    pending_models: list[Json] = []
    pending_tools: list[Json] = []
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
        elif event == "tool_call":
            call_id = tool_call_id(payload)
            pending_tools.append(
                {
                    "row": row,
                    "payload": payload,
                    "call_id": call_id,
                    "parent_span_id": agent_span_id,
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

    child_error_count = sum(
        item["status"]["code"] == "STATUS_CODE_ERROR" for item in child_spans
    )
    terminal_outcome = _agent_terminal_outcome(rows)
    has_agent_start = any(row["event"] == "agent_start" for row in rows)
    has_agent_end = any(row["event"] == "agent_end" for row in rows)
    if has_agent_start and not has_agent_end:
        has_error = True
        root_status_message = "agent_end event is missing"
    elif terminal_outcome is None:
        has_error = child_error_count > 0
        root_status_message = "one or more child spans failed" if has_error else ""
    else:
        has_error, root_status_message = terminal_outcome
    event_times = [halo_time(row.get("timestamp")) for row in rows]
    start_time = min([*event_times, *(item["start_time"] for item in child_spans)])
    end_time = max([*event_times, *(item["end_time"] for item in child_spans)])
    root = span(
        trace_id=trace_id,
        span_id=agent_span_id,
        parent_span_id=root_parent_span_id,
        name=f"agent.{agent_role}",
        kind="SPAN_KIND_INTERNAL",
        start_time=start_time,
        end_time=end_time,
        status_code="STATUS_CODE_ERROR" if has_error else "STATUS_CODE_OK",
        status_message=root_status_message,
        attrs=agent_attrs,
        options=options,
    )
    return [root, *child_spans]


def _nested_text(value: object, keys: set[str]) -> str | None:
    """Return the first non-empty scalar under one of the normalized keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            if normalized_key(key) in keys and item not in (None, ""):
                return str(item)
        for item in value.values():
            found = _nested_text(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_text(item, keys)
            if found:
                return found
    return None


def subagent_references(rows: list[Json]) -> list[Json]:
    """Extract standard Tool-to-child-session links from source events.

    The returned metadata is used only to build parent/child span topology. It
    is not emitted as a custom HALO attribute.
    """
    references: dict[str, Json] = {}
    subagent_tool_ids: set[str] = set()
    subagent_tool_names = {"call_subagent", "run_subagent"}

    for row in rows:
        payload = row["payload"]
        call_id = tool_call_id(payload)
        event = row["event"]
        tool_name = normalized_key(payload.get("tool_name") or payload.get("name") or "")

        if event == "tool_call" and tool_name in subagent_tool_names and call_id:
            subagent_tool_ids.add(call_id)
            reference = references.setdefault(call_id, {"tool_call_id": call_id})
            profile = _nested_text(
                payload.get("args", {}),
                {"agent_profile", "subagent_profile_id"},
            )
            if profile:
                reference["agent_profile"] = profile
            continue

        is_completion = event == "subagent_completed"
        is_result = event == "tool_result" and (
            tool_name in subagent_tool_names or (call_id and call_id in subagent_tool_ids)
        )
        if not call_id or not (is_completion or is_result):
            continue

        reference = references.setdefault(call_id, {"tool_call_id": call_id})
        child_session_id = _nested_text(payload, {"child_session_id"})
        profile = _nested_text(
            payload,
            {"agent_profile", "subagent_profile_id"},
        )
        if child_session_id:
            reference["child_session_id"] = child_session_id
        if profile:
            reference["agent_profile"] = profile

    return [
        reference
        for reference in references.values()
        if reference.get("child_session_id")
    ]


def subagent_session_candidates(rows_by_source: dict[object, list[Json]]) -> dict[str, Json]:
    """Choose the richest detailed event stream for every subagent session."""
    candidates: dict[str, Json] = {}
    execution_events = {
        "agent_start",
        "agent_end",
        "model_input",
        "model_output",
        "tool_call",
        "tool_result",
    }

    for source, rows in rows_by_source.items():
        by_session: dict[str, list[Json]] = {}
        for row in rows:
            session_id = row.get("session_id")
            if session_id in (None, ""):
                continue
            by_session.setdefault(str(session_id), []).append(row)

        for session_id, session_rows in by_session.items():
            detailed_rows = [
                row
                for row in session_rows
                if row["event"] in execution_events
                and normalized_key(row.get("agent_role") or "") == "subagent"
            ]
            if not detailed_rows:
                continue
            score = sum(row["event"] in execution_events for row in session_rows)
            previous = candidates.get(session_id)
            if previous is None or score > previous["score"]:
                candidates[session_id] = {
                    "source": source,
                    "rows": session_rows,
                    "score": score,
                }
    return candidates


def attach_linked_subagents(
    spans: list[Json],
    source_rows: list[Json],
    session_candidates: dict[str, Json],
    project_id: str,
    *,
    options: ConversionOptions | None = None,
) -> tuple[list[Json], set[str]]:
    """Attach available child logs below their standard subagent Tool spans."""
    options = options or ConversionOptions()
    attached_sessions: set[str] = set()
    active_sessions: set[str] = set()

    def tool_span_by_call_id(call_id: str, trace_id: str | None = None) -> Json | None:
        for item in spans:
            if trace_id is not None and item["trace_id"] != trace_id:
                continue
            attrs = item["attributes"]
            if (
                attrs.get("inference.observation_kind") == "TOOL"
                and attrs.get("tool.call.id") == call_id
                and normalized_key(attrs.get("tool.name") or "")
                in {"call_subagent", "run_subagent"}
            ):
                return item
        return None

    def attach_from(rows: list[Json], trace_id: str | None = None) -> None:
        for reference in subagent_references(rows):
            session_id = str(reference["child_session_id"])
            if session_id in attached_sessions:
                continue
            candidate = session_candidates.get(session_id)
            parent_tool = tool_span_by_call_id(
                str(reference["tool_call_id"]),
                trace_id,
            )
            if candidate is None or parent_tool is None:
                continue
            if session_id in active_sessions:
                warnings.warn(
                    f"subagent session cycle ignored: {session_id}",
                    stacklevel=2,
                )
                continue

            active_sessions.add(session_id)
            child_rows = candidate["rows"]
            child_spans = _convert_run(
                child_rows,
                project_id,
                parent_tool["trace_id"],
                options,
                trace_id_override=parent_tool["trace_id"],
                root_parent_span_id=parent_tool["span_id"],
                agent_name_override=str(
                    reference.get("agent_profile")
                    or child_rows[0].get("agent_role")
                    or "subagent"
                ),
            )
            spans.extend(child_spans)
            attached_sessions.add(session_id)
            attach_from(child_rows, parent_tool["trace_id"])
            active_sessions.remove(session_id)

    attach_from(source_rows)
    if attached_sessions:
        _enclose_all_descendants(spans)
        validate_trace_graph(spans)
    return spans, attached_sessions


def _enclose_all_descendants(spans: list[Json]) -> None:
    """Extend every ancestor interval after cross-file child spans are attached."""
    spans_by_id = {item["span_id"]: item for item in spans}
    for _ in range(len(spans)):
        changed = False
        for item in spans:
            parent = spans_by_id.get(item["parent_span_id"])
            if parent is None:
                continue
            start_time = min(parent["start_time"], item["start_time"])
            end_time = max(parent["end_time"], item["end_time"])
            if start_time != parent["start_time"] or end_time != parent["end_time"]:
                parent["start_time"] = start_time
                parent["end_time"] = end_time
                changed = True
        if not changed:
            break


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
