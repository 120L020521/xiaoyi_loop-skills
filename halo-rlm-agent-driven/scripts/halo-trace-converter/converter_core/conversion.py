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
from .status import (
    agent_status,
    correlation_id,
    tool_call_id,
)
from .validation import validate_input, validate_trace_graph


EXECUTION_EVENTS = {
    "agent_start",
    "agent_end",
    "session_started",
    "session_ended",
    "model_input",
    "model_output",
    "tool_call",
    "tool_result",
}

START_EVENTS = {"agent_start", "session_started"}
END_EVENTS = {"agent_end", "session_ended"}


def _row_run_id(row: Json) -> str | None:
    value = row["payload"].get("run_id") or row.get("run_id")
    return str(value) if value not in (None, "") else None


def _row_parent_session_id(row: Json) -> str | None:
    payload = row["payload"]
    value = row.get("parent_session_id") or payload.get("parent_session_id")
    return str(value) if value not in (None, "") else None


def _has_execution(rows: list[Json]) -> bool:
    return any(row["event"] in EXECUTION_EVENTS for row in rows)


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
    for row in reversed(rows):
        if row["event"] not in END_EVENTS:
            continue
        error_message, failed = agent_status(row["payload"])
        return failed, error_message
    return None


def _agent_instructions(rows: list[Json]) -> str:
    """Recover the system prompt for the native AGENT attribute when available."""
    for row in rows:
        payload = row["payload"]
        for key in ("agent_instructions", "instructions", "system_prompt"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def _agent_timeline_rows(rows: list[Json], identity_row: Json) -> list[Json]:
    """Keep foreign-session evidence out of the current AGENT timeline."""
    execution_rows = [
        row for row in rows if row["event"] in EXECUTION_EVENTS
    ]
    identity_session = identity_row.get("session_id")
    if identity_session not in (None, ""):
        session_rows = [
            row
            for row in execution_rows
            if row.get("session_id") == identity_session
        ]
        if session_rows:
            return session_rows
    return execution_rows or rows


def _group_by_session(rows: list[Json]) -> list[list[Json]]:
    """Partition interleaved main/child events without creating metadata roots."""
    sessions: dict[str, list[Json]] = {}
    unassigned: list[Json] = []
    for row in rows:
        session_value = row.get("session_id")
        if session_value in (None, ""):
            unassigned.append(row)
        else:
            sessions.setdefault(str(session_value), []).append(row)

    executable_ids = [
        session_id
        for session_id, group in sessions.items()
        if _has_execution(group)
    ]
    if not executable_ids:
        return [rows]
    executable_id_set = set(executable_ids)

    grouped = {session_id: sessions[session_id] for session_id in executable_ids}
    run_sessions: dict[str, set[str]] = {}
    for session_id, group in grouped.items():
        for row in group:
            run_id = _row_run_id(row)
            if run_id:
                run_sessions.setdefault(run_id, set()).add(session_id)

    # Auxiliary-only sessions and rows without session_id are evidence, not
    # independent executions. Attach each once to an explicitly referenced
    # executable session, or to the main/earliest session as a deterministic
    # fallback.
    auxiliary = [
        row
        for session_id, group in sessions.items()
        if session_id not in executable_id_set
        for row in group
    ]
    auxiliary.extend(unassigned)
    main_ids = [
        session_id
        for session_id, group in grouped.items()
        if any(str(row.get("agent_role") or "") == "main" for row in group)
    ]
    fallback_id = min(
        main_ids or list(executable_ids),
        key=lambda session_id: min(
            halo_time(row.get("timestamp")) for row in grouped[session_id]
        ),
    )
    for row in auxiliary:
        payload = row["payload"]
        references = (
            row.get("parent_session_id"),
            payload.get("parent_session_id"),
            payload.get("child_session_id"),
        )
        target = next(
            (
                str(value)
                for value in references
                if value not in (None, "") and str(value) in executable_id_set
            ),
            None,
        )
        run_id = _row_run_id(row)
        if target is None and run_id and len(run_sessions.get(run_id, set())) == 1:
            target = next(iter(run_sessions[run_id]))
        grouped[target or fallback_id].append(row)

    result = list(grouped.values())
    for group in result:
        group.sort(key=lambda row: halo_time(row.get("timestamp")))
    result.sort(key=lambda group: halo_time(group[0].get("timestamp")))
    return result


def _split_session_runs(rows: list[Json]) -> list[list[Json]]:
    """Split sequential runs inside one session while retaining auxiliary rows."""
    groups: list[list[Json]] = []
    current: list[Json] = []
    current_run_id: str | None = None
    has_execution = False
    closed = False

    for row in rows:
        event = row["event"]
        is_execution = event in EXECUTION_EVENTS
        run_id = _row_run_id(row)
        boundary = bool(
            has_execution
            and is_execution
            and (
                closed
                or event in START_EVENTS
                or (run_id and current_run_id and run_id != current_run_id)
            )
        )
        if boundary:
            groups.append(current)
            current = []
            current_run_id = None
            has_execution = False
            closed = False

        current.append(row)
        if run_id:
            current_run_id = run_id
        has_execution = has_execution or is_execution
        if event in END_EVENTS:
            closed = True

    if current and has_execution:
        groups.append(current)
    return groups


def _split_runs(rows: list[Json]) -> list[list[Json]]:
    """Split interleaved session streams, then split sequential runs."""
    return [
        run
        for session_rows in _group_by_session(rows)
        for run in _split_session_runs(session_rows)
    ]


def _convert_run(
    rows: list[Json],
    project_id: str,
    default_trace_id: str,
    options: ConversionOptions,
    trace_id_override: str | None = None,
    root_parent_span_id: str = "",
    agent_name_override: str | None = None,
) -> list[Json]:
    identity_row = next(
        (row for row in rows if row["event"] in EXECUTION_EVENTS),
        rows[0],
    )
    timeline_rows = _agent_timeline_rows(rows, identity_row)
    identity_first_rows = [
        *(row for row in rows if row["event"] in EXECUTION_EVENTS),
        *(row for row in rows if row["event"] not in EXECUTION_EVENTS),
    ]
    trace_id = trace_id_override or trace_id_from(
        identity_first_rows,
        default_trace_id,
    )
    agent_span_id = str(uuid.uuid4())
    agent_role = agent_name_override or str(identity_row.get("agent_role") or "main")
    child_spans: list[Json] = []
    pending_models: list[Json] = []
    pending_tools: list[Json] = []
    source_events: list[Json] = []
    agent_attrs = {
        **base_attrs(project_id, "AGENT"),
        "agent.name": agent_role,
        "agent.instructions": _agent_instructions(timeline_rows),
        "inference.agent_name": agent_role,
    }
    session_id = next(
        (
            str(row["session_id"])
            for row in timeline_rows
            if row.get("session_id") not in (None, "")
        ),
        None,
    )
    parent_session_id = next(
        (
            value
            for row in timeline_rows
            if (value := _row_parent_session_id(row)) is not None
        ),
        None,
    )
    run_id = next(
        (value for row in timeline_rows if (value := _row_run_id(row)) is not None),
        None,
    )
    if session_id:
        agent_attrs["session.id"] = session_id
    if parent_session_id:
        agent_attrs["session.parent_id"] = parent_session_id
    if run_id:
        agent_attrs["agent.run_id"] = run_id

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

    terminal_outcome = _agent_terminal_outcome(timeline_rows)
    has_agent_start = any(
        row["event"] in START_EVENTS
        for row in timeline_rows
    )
    has_agent_end = any(
        row["event"] in END_EVENTS
        for row in timeline_rows
    )
    if has_agent_start and not has_agent_end:
        has_error = True
        root_status_message = "agent/session end event is missing"
    elif terminal_outcome is not None:
        has_error, root_status_message = terminal_outcome
    else:
        # Native pi-halo-tracer keeps a normally closed root span OK even when
        # one of its child TOOL or LLM spans failed.
        has_error = False
        root_status_message = ""
    event_times = [
        halo_time(row.get("timestamp")) for row in timeline_rows
    ]
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
        "session_started",
        "session_ended",
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
    used_trace_ids: dict[str, int] = {}
    for index, group in enumerate(groups):
        validate_input(group, require_chronological=True)
        fallback = default_trace_id if index == 0 else str(uuid.uuid4())
        candidate_trace_id = (
            default_trace_id
            if force_trace_id
            else trace_id_from(group, fallback)
        )
        collision_index = used_trace_ids.get(candidate_trace_id, 0)
        used_trace_ids[candidate_trace_id] = collision_index + 1
        if collision_index:
            candidate_trace_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"halo-run:{candidate_trace_id}:{collision_index + 1}",
                )
            )
        converted.extend(
            _convert_run(
                group,
                project_id,
                fallback,
                options,
                trace_id_override=candidate_trace_id,
            )
        )
    validate_trace_graph(converted)
    return converted
