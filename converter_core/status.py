"""Infer errors and extract event correlation identifiers."""

from __future__ import annotations

from typing import Any, Iterator

from .content import jsonish, normalized_key
from .models import Json


def _walk_json(value: Any, *, max_depth: int = 10) -> Iterator[Json]:
    if max_depth < 0:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item, max_depth=max_depth - 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item, max_depth=max_depth - 1)


def _message_from_error(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "error_message", "errMsg", "detail", "reason"):
            if value.get(key):
                return str(value[key])
        return jsonish(value) if value else ""
    return str(value) if value not in (None, "", False) else ""


def _boolean_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
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


def event_status(payload: Json) -> tuple[str, bool]:
    """Infer failure from common Tool and model result conventions."""
    error_messages: list[str] = []
    reasons: list[str] = []
    stderr_messages: list[str] = []
    explicit_success = False
    failure_states = {
        "cancelled",
        "canceled",
        "error",
        "failed",
        "failure",
        "timed_out",
        "timeout",
        "content_filter",
        "status_code_error",
    }

    for node in _walk_json(payload):
        for key, value in node.items():
            normalized = normalized_key(key)
            if normalized in {"success", "ok"}:
                boolean = _boolean_value(value)
                if boolean is False:
                    reasons.append(f"{key}=false")
                elif boolean is True:
                    explicit_success = True
            elif normalized in {
                "is_error",
                "failed",
                "cancelled",
                "canceled",
                "timeout",
                "timed_out",
            }:
                if _boolean_value(value) is True:
                    reasons.append(f"{key}=true")
            elif normalized in {"exitcode", "exit_code", "returncode", "return_code"}:
                numeric = _number_value(value)
                if numeric is not None and numeric != 0:
                    reasons.append(f"{key}={value}")
            elif normalized in {"http_status", "http_status_code"}:
                numeric = _number_value(value)
                if numeric is not None and numeric >= 400:
                    reasons.append(f"{key}={value}")
            elif normalized == "status_code":
                numeric = _number_value(value)
                if numeric is not None and numeric >= 400:
                    reasons.append(f"{key}={value}")
                elif isinstance(value, str) and normalized_key(value) in failure_states:
                    reasons.append(f"{key}={value}")
            elif normalized in {"status", "state", "finish_reason", "stop_reason"}:
                numeric = _number_value(value) if normalized == "status" else None
                if numeric is not None and numeric >= 400:
                    reasons.append(f"{key}={value}")
                elif isinstance(value, str) and normalized_key(value) in failure_states:
                    reasons.append(f"{key}={value}")
            elif normalized in {
                "error",
                "error_message",
                "errcode",
                "err_msg",
                "exception",
            }:
                message = _message_from_error(value)
                if message:
                    error_messages.append(message)
            elif normalized == "stderr" and value and str(value).strip():
                stderr_messages.append(str(value))

    failed = bool(error_messages or reasons)
    if not failed and stderr_messages and not explicit_success:
        failed = True
    if not failed:
        return "", False
    message = next(
        (item for item in [*error_messages, *stderr_messages, *reasons] if item),
        "operation failed",
    )
    return message, True


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
