"""Resolve native HALO statuses and extract event correlation identifiers."""

from __future__ import annotations

import json
from typing import Any

from .content import jsonish, normalized_key
from .models import Json


FAILURE_STATES = {
    "cancelled",
    "canceled",
    "content_filter",
    "error",
    "failed",
    "failure",
    "status_code_error",
    "timed_out",
    "timeout",
}

SUCCESS_STATES = {
    "complete",
    "completed",
    "ok",
    "stop",
    "success",
    "succeeded",
}


def _message_from_error(value: Any) -> str:
    if isinstance(value, dict):
        for key in (
            "message",
            "error_message",
            "errorMessage",
            "errMsg",
            "detail",
            "reason",
        ):
            if value.get(key):
                return str(value[key])
        return jsonish(value) if value else ""
    if isinstance(value, list):
        messages = [_message_from_error(item) for item in value]
        return "; ".join(message for message in messages if message)
    return str(value) if value not in (None, "", False) else ""


def _boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true"}:
            return True
        if lowered in {"0", "false"}:
            return False
    return None


def _number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _known_wrapper(payload: Json) -> tuple[Json, Json, Json, Json | None] | None:
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    backend = normalized_key(details.get("backend"))
    has_wrapper_shape = "ok" in details and isinstance(details.get("raw"), dict)
    if backend not in {"cli", "os_api"} and not has_wrapper_shape:
        return None
    raw = details.get("raw")
    if not isinstance(raw, dict):
        raw = {}
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    output = _json_value(data.get("output"))
    return details, raw, data, output if isinstance(output, dict) else None


def _content_error_message(payload: Json) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        decoded = _json_value(text)
        if isinstance(decoded, dict):
            for key in ("errMsg", "errorMessage", "error_message", "error", "message"):
                message = _message_from_error(decoded.get(key))
                if message:
                    return message
            continue
        return text.strip()
    return ""


def _process_error_message(output: Json | None) -> str:
    if not output:
        return ""
    stdout = _json_value(output.get("stdout"))
    if isinstance(stdout, dict):
        for key in ("error", "errors", "errorMessage", "error_message", "message"):
            message = _message_from_error(stdout.get(key))
            if message:
                return message
    stderr = output.get("stderr")
    return str(stderr).strip() if stderr not in (None, "") else ""


def _tool_error_message(
    payload: Json,
    wrapper: tuple[Json, Json, Json, Json | None] | None = None,
) -> str:
    if wrapper:
        details, raw, data, output = wrapper
        candidates = [
            _process_error_message(output),
            _message_from_error(data.get("error")),
            _message_from_error(raw.get("error")),
            _message_from_error(details.get("error")),
            _content_error_message(payload),
        ]
    else:
        candidates = [
            _message_from_error(payload.get("error")),
            _message_from_error(payload.get("error_message")),
            _message_from_error(payload.get("errorMessage")),
            _message_from_error(payload.get("exception")),
            _content_error_message(payload),
        ]
    return next((message for message in candidates if message), "tool execution failed")


def _wrapper_status(payload: Json) -> tuple[str, bool] | None:
    wrapper = _known_wrapper(payload)
    if not wrapper:
        return None
    details, _raw, _data, output = wrapper
    wrapper_ok = _boolean_value(details.get("ok"))
    if wrapper_ok is None:
        return None
    if wrapper_ok:
        return "", False

    # A known wrapper occasionally reports ok=false solely because a successful
    # process wrote progress text to stderr. Trust the process result when both
    # its exit code and its JSON stdout explicitly report success.
    if output and _number_value(output.get("exitCode")) == 0:
        stdout = _json_value(output.get("stdout"))
        if isinstance(stdout, dict) and any(
            _boolean_value(stdout.get(key)) is True for key in ("ok", "success")
        ):
            return "", False
    return _tool_error_message(payload, wrapper), True


def _subagent_status(payload: Json) -> tuple[str, bool] | None:
    tool_name = normalized_key(payload.get("tool_name"))
    if tool_name not in {"call_subagent", "run_subagent"}:
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    if any(
        _boolean_value(details.get(key)) is True
        for key in ("timed_out", "max_turns_exceeded")
    ):
        return "subagent did not complete normally", True
    value = details.get("terminal_status")
    if value in (None, "") and isinstance(details.get("subagent"), dict):
        value = details["subagent"].get("terminalStatus")
    status = normalized_key(value) if value not in (None, "") else ""
    if status in FAILURE_STATES:
        message = (
            _message_from_error(details.get("stopped_reason"))
            or _content_error_message(payload)
            or f"terminal_status={value}"
        )
        return message, True
    if status in SUCCESS_STATES:
        return "", False
    return None


def _top_level_status(payload: Json) -> tuple[str, bool] | None:
    explicit_success = False
    for key in ("success", "ok"):
        boolean = _boolean_value(payload.get(key))
        if boolean is False:
            return f"{key}=false", True
        if boolean is True:
            explicit_success = True

    for key in ("failed", "cancelled", "canceled", "timeout", "timed_out"):
        if _boolean_value(payload.get(key)) is True:
            return f"{key}=true", True

    explicit_zero_exit = False
    for key in ("exitCode", "exit_code", "returncode", "return_code"):
        numeric = _number_value(payload.get(key))
        if numeric is not None and numeric != 0:
            return f"{key}={payload[key]}", True
        if numeric == 0:
            explicit_zero_exit = True

    explicit_success_status = False
    for key in ("http_status", "http_status_code", "status_code"):
        numeric = _number_value(payload.get(key))
        if numeric is not None and numeric >= 400:
            return f"{key}={payload[key]}", True
        if numeric is not None:
            explicit_success_status = True

    for key in ("status", "state", "terminal_status"):
        value = payload.get(key)
        status = normalized_key(value) if value not in (None, "") else ""
        if status in FAILURE_STATES:
            return f"{key}={value}", True
        if status in SUCCESS_STATES:
            explicit_success_status = True

    if explicit_success:
        return "", False

    for key in ("error", "error_message", "errorMessage", "exception"):
        message = _message_from_error(payload.get(key))
        if message:
            return message, True
    if explicit_zero_exit or explicit_success_status:
        return "", False
    return None


def tool_status(payload: Json) -> tuple[str, bool]:
    """Resolve a TOOL result without scanning arbitrary nested business data."""
    explicit_error = _boolean_value(payload.get("is_error"))
    if explicit_error is True:
        return _tool_error_message(payload), True

    top_level = _top_level_status(payload)
    if top_level is not None and top_level[1]:
        return top_level

    for resolver in (_wrapper_status, _subagent_status):
        resolved = resolver(payload)
        if resolved is not None:
            return resolved
    if top_level is not None:
        return top_level

    # This mirrors Pi Agent: a tool that returned normally is successful unless
    # its producer exposes a recognized operation-level failure contract.
    return "", False


def llm_status(payload: Json) -> tuple[str, bool]:
    """Mirror the native tracer's assistant.errorMessage LLM status rule."""
    assistant = payload.get("assistant")
    if isinstance(assistant, dict):
        for key in ("errorMessage", "error_message"):
            message = _message_from_error(assistant.get(key))
            if message:
                return message, True
        for key in ("status", "state", "stopReason", "stop_reason"):
            value = assistant.get(key)
            if value not in (None, "") and normalized_key(value) in FAILURE_STATES:
                return f"{key}={value}", True

    resolved = _top_level_status(payload)
    return resolved if resolved is not None else ("", False)


def agent_status(payload: Json) -> tuple[str, bool]:
    """Resolve an explicit agent_end result; a normal end is successful."""
    resolved = _top_level_status(payload)
    return resolved if resolved is not None else ("", False)


def correlation_id(payload: Json) -> str | None:
    candidates: list[Any] = [payload]
    for key in ("assistant", "request", "response", "metadata"):
        if isinstance(payload.get(key), dict):
            candidates.append(payload[key])
    for candidate in candidates:
        for key in ("request_id", "model_call_id", "call_id", "message_id"):
            if candidate.get(key) not in (None, ""):
                return str(candidate[key])
    return None


def tool_call_id(payload: Json) -> str | None:
    value = payload.get("tool_call_id")
    return str(value) if value not in (None, "") else None


def tool_ids_from_model_output(payload: Json) -> set[str]:
    containers: list[Any] = [
        payload.get("tool_calls_decided"),
        payload.get("tool_calls"),
    ]
    assistant = payload.get("assistant")
    if isinstance(assistant, dict):
        containers.extend((assistant.get("tool_calls"), assistant.get("content")))

    found: set[str] = set()
    for container in containers:
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if (
                item.get("id") not in (None, "")
                and (
                    item_type in {"toolcall", "tool_call", "tool_use"}
                    or "name" in item
                    or "function" in item
                )
            ):
                found.add(str(item["id"]))
    return found
