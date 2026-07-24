"""Build canonical AGENT, LLM, and TOOL span records."""

from __future__ import annotations

import uuid

from .content import (
    attribute_value,
    cap_attribute,
    halo_time,
    source_context_value,
)
from .models import ConversionOptions, Json
from .status import event_status


def base_attrs(project_id: str, kind: str) -> Json:
    return {
        "inference.export.schema_version": 1,
        "inference.project_id": project_id,
        "inference.observation_kind": kind,
        "openinference.span.kind": kind,
    }


def span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    name: str,
    kind: str,
    start_time: str,
    end_time: str,
    status_code: str,
    status_message: str,
    attrs: Json,
    options: ConversionOptions,
) -> Json:
    attrs = {
        key: cap_attribute(value, options.max_attribute_chars)
        for key, value in attrs.items()
    }
    status_message = cap_attribute(status_message, options.max_attribute_chars)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "trace_state": "",
        "name": name,
        "kind": kind,
        "start_time": start_time,
        "end_time": end_time,
        "status": {"code": status_code, "message": status_message},
        "resource": {"attributes": {"service.name": "converted-agent"}},
        "scope": {"name": "event-stream-to-halo-full-clean", "version": "1.0.0"},
        "attributes": attrs,
    }


def trace_id_from(rows: list[Json], fallback: str) -> str:
    for row in rows:
        payload = row["payload"]
        if payload.get("run_id"):
            return str(payload["run_id"])
        if row.get("run_id"):
            return str(row["run_id"])
    for row in rows:
        if row.get("session_id"):
            return str(row["session_id"])
    return fallback


def llm_span(
    row: Json,
    pending_model: Json | None,
    trace_id: str,
    agent_span_id: str,
    project_id: str,
    options: ConversionOptions,
) -> Json:
    payload = row["payload"]
    input_payload = pending_model["payload"] if pending_model else {}
    assistant = payload.get("assistant") if isinstance(payload.get("assistant"), dict) else {}
    usage = assistant.get("usage") if isinstance(assistant.get("usage"), dict) else {}
    model = assistant.get("model") or "model"
    provider = assistant.get("provider") or assistant.get("api")
    attrs = {
        **base_attrs(project_id, "LLM"),
        "llm.input_messages": attribute_value(input_payload.get("messages", []), options),
        "llm.output_messages": attribute_value([assistant] if assistant else payload, options),
        "llm.tools": attribute_value(input_payload.get("tools", []), options),
        "llm.system_prompt": attribute_value(input_payload.get("system_prompt", ""), options),
        "source.model_output.context": source_context_value(
            row,
            (
                {"assistant": "llm.output_messages"}
                if assistant
                else {"*": "llm.output_messages"}
            ),
            options,
        ),
    }
    if pending_model:
        attrs["source.model_input.context"] = source_context_value(
            pending_model["row"],
            {
                "messages": "llm.input_messages",
                "tools": "llm.tools",
                "system_prompt": "llm.system_prompt",
            },
            options,
        )
    if model:
        attrs["inference.llm.model_name"] = str(model)
        attrs["llm.model_name"] = str(model)
    if provider:
        attrs["inference.llm.provider"] = str(provider)
    if isinstance(usage.get("input"), int):
        attrs["inference.llm.input_tokens"] = usage["input"]
        attrs["llm.token_count.prompt"] = usage["input"]
    if isinstance(usage.get("output"), int):
        attrs["inference.llm.output_tokens"] = usage["output"]
        attrs["llm.token_count.completion"] = usage["output"]
    if isinstance(usage.get("total_tokens"), int):
        attrs["llm.token_count.total"] = usage["total_tokens"]

    error_message, failed = event_status(payload)
    start_time = (
        halo_time(pending_model["row"].get("timestamp"))
        if pending_model
        else halo_time(row.get("timestamp"))
    )
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=agent_span_id,
        name=f"response.{model}",
        kind="SPAN_KIND_CLIENT",
        start_time=start_time,
        end_time=halo_time(row.get("timestamp")),
        status_code="STATUS_CODE_ERROR" if failed else "STATUS_CODE_OK",
        status_message=error_message,
        attrs=attrs,
        options=options,
    )


def unfinished_llm_span(
    pending_model: Json,
    trace_id: str,
    agent_span_id: str,
    project_id: str,
    options: ConversionOptions,
) -> Json:
    payload = pending_model["payload"]
    timestamp = halo_time(pending_model["row"].get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=agent_span_id,
        name="response.model.unfinished",
        kind="SPAN_KIND_CLIENT",
        start_time=timestamp,
        end_time=timestamp,
        status_code="STATUS_CODE_ERROR",
        status_message="model_input has no matching model_output",
        attrs={
            **base_attrs(project_id, "LLM"),
            "llm.input_messages": attribute_value(payload.get("messages", []), options),
            "llm.output_messages": "",
            "llm.tools": attribute_value(payload.get("tools", []), options),
            "llm.system_prompt": attribute_value(payload.get("system_prompt", ""), options),
            "source.model_input.context": source_context_value(
                pending_model["row"],
                {
                    "messages": "llm.input_messages",
                    "tools": "llm.tools",
                    "system_prompt": "llm.system_prompt",
                },
                options,
            ),
        },
        options=options,
    )


def tool_span(
    row: Json,
    call: Json | None,
    trace_id: str,
    parent_span_id: str,
    project_id: str,
    call_id: str,
    options: ConversionOptions,
) -> Json:
    payload = row["payload"]
    call_payload = call["payload"] if call else {}
    tool_name = payload.get("tool_name") or call_payload.get("tool_name") or "tool"
    error_message, failed = event_status(payload)
    start_time = (
        halo_time(call["row"].get("timestamp"))
        if call
        else halo_time(row.get("timestamp"))
    )
    attrs = {
        **base_attrs(project_id, "TOOL"),
        "tool.name": str(tool_name),
        "tool.call_id": call_id,
        "tool.call.id": call_id,
        "input.value": attribute_value(call_payload.get("args", {}), options),
        "output.value": attribute_value(payload, options),
        "tool.is_error": failed,
        "source.tool_result.context": source_context_value(
            row,
            {"*": "output.value"},
            options,
        ),
    }
    if call:
        attrs["source.tool_call.context"] = source_context_value(
            call["row"],
            {
                "args": "input.value",
                "tool_name": "tool.name",
                "tool_call_id": "tool.call_id",
            },
            options,
        )
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=parent_span_id,
        name=f"function.{tool_name}",
        kind="SPAN_KIND_INTERNAL",
        start_time=start_time,
        end_time=halo_time(row.get("timestamp")),
        status_code="STATUS_CODE_ERROR" if failed else "STATUS_CODE_OK",
        status_message=error_message,
        attrs=attrs,
        options=options,
    )


def unmatched_tool_span(
    call_id: str,
    call: Json,
    trace_id: str,
    parent_span_id: str,
    project_id: str,
    options: ConversionOptions,
) -> Json:
    payload = call["payload"]
    tool_name = payload.get("tool_name") or "tool"
    timestamp = halo_time(call["row"].get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=parent_span_id,
        name=f"function.{tool_name}",
        kind="SPAN_KIND_INTERNAL",
        start_time=timestamp,
        end_time=timestamp,
        status_code="STATUS_CODE_ERROR",
        status_message="tool_call has no matching tool_result",
        attrs={
            **base_attrs(project_id, "TOOL"),
            "tool.name": str(tool_name),
            "tool.call_id": call_id,
            "tool.call.id": call_id,
            "input.value": attribute_value(payload.get("args", {}), options),
            "output.value": "",
            "tool.is_error": True,
            "source.tool_call.context": source_context_value(
                call["row"],
                {
                    "args": "input.value",
                    "tool_name": "tool.name",
                    "tool_call_id": "tool.call_id",
                },
                options,
            ),
        },
        options=options,
    )
