#!/usr/bin/env python3
"""Build a portable Workspace-Bench Judge prompt from an external Runner log.

This command never calls an LLM. It combines task metadata, a JSONL execution
log, and optional Runner output files into a small bundle that can be uploaded
to another model for manual LLM-as-a-Judge evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType


SYSTEM_PROMPT = "你是一个严格的任务评测员。"

_TEXT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".html",
    ".htm",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".sql",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SECRET_KEY_NAMES = {
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "refreshtoken",
    "secret",
    "sessiontoken",
    "setcookie",
    "xapikey",
}
_SECRET_TEXT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"), "Basic [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-[REDACTED]"),
    (
        re.compile(
            r"""(?ix)
            (?P<key>["']?(?:api[_-]?key|authorization|password|secret|
            access[_-]?token|refresh[_-]?token|session[_-]?token)["']?)
            \s*[:=]\s*
            (?P<quote>["']?)
            (?P<value>[^"',\s}]+)
            (?P=quote)
            """
        ),
        r"\g<key>: [REDACTED]",
    ),
)
_READ_TOOL_TOKENS = (
    "cat",
    "get_file",
    "parse_pdf",
    "read",
    "read_file",
)
_EXECUTE_TOOL_TOKENS = (
    "bash",
    "command",
    "execute",
    "powershell",
    "shell",
    "terminal",
)
_TOOL_OUTPUT_KEYS = {
    "body",
    "content",
    "output",
    "output.value",
    "response",
    "result",
    "stderr",
    "stdout",
    "tooloutput",
}
_LOG_FORMAT_CHOICES = ("auto", "generic", "halo", "xiaoyi", "event-stream")
_NORMALIZED_LOG_SCHEMA = "workspace-bench.runner-event.v1"


class JudgePromptError(RuntimeError):
    """Raised when the prompt bundle cannot be generated safely."""


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument sequence. Uses `sys.argv` when omitted.

    Returns:
        Parsed command-line namespace.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Generate a Workspace-Bench Judge prompt from task metadata, an "
            "external Runner JSONL log, and optional output files. No API call is made."
        )
    )
    task_source = parser.add_mutually_exclusive_group(required=True)
    task_source.add_argument(
        "--task-id",
        help="Workspace-Bench task ID, resolved below --task-root.",
    )
    task_source.add_argument(
        "--metadata",
        type=Path,
        help="Path to a copied Workspace-Bench metadata.json file.",
    )
    parser.add_argument(
        "--task-root",
        type=Path,
        help=(
            "Root containing <task-id>/metadata.json. Defaults to WB_TASK_ROOT "
            "or this repository's Workspace-Bench/evaluation/tasks_lite."
        ),
    )
    parser.add_argument(
        "--log-jsonl",
        type=Path,
        required=True,
        help="JSONL log produced by the external Runner Agent.",
    )
    parser.add_argument(
        "--log-format",
        choices=_LOG_FORMAT_CHOICES,
        default="auto",
        help=(
            "Source JSONL format. 'auto' detects HALO spans and XiaoYi-style "
            "event+payload streams before falling back to a generic adapter."
        ),
    )
    parser.add_argument(
        "--runner-output",
        type=Path,
        action="append",
        default=[],
        help=(
            "Runner output file or directory. Repeat for multiple paths. "
            "Actual outputs are important for result-based rubrics."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="New or empty directory for the generated Judge bundle.",
    )
    parser.add_argument(
        "--eval-src",
        type=Path,
        help=(
            "Optional Workspace-Bench evaluation/src directory. When available, "
            "its rich Office/PDF excerpt reader is reused."
        ),
    )
    parser.add_argument(
        "--max-trace-items",
        type=int,
        default=30,
        help="Maximum JSONL events embedded in the prompt.",
    )
    parser.add_argument(
        "--max-str-len",
        type=int,
        default=12000,
        help="Maximum length of each string embedded in trace evidence.",
    )
    parser.add_argument(
        "--max-output-files",
        type=int,
        default=50,
        help="Maximum number of Runner outputs embedded in the prompt.",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=80_000,
        help="Maximum bytes read from a text output for an excerpt.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite bundle files when --out-dir is not empty.",
    )
    return parser.parse_args(argv)


def _find_repo_root(start: Path) -> Path | None:
    """Find the better-office repository root from a descendant path."""
    for candidate in (start, *start.parents):
        if (candidate / "Workspace-Bench").is_dir() and (candidate / "deepagents").is_dir():
            return candidate
    return None


def _default_task_root() -> Path | None:
    """Resolve the default Workspace-Bench task root when available."""
    configured = os.environ.get("WB_TASK_ROOT")
    if configured:
        return Path(configured).expanduser()
    repo_root = _find_repo_root(Path(__file__).resolve())
    if repo_root is None:
        return None
    return repo_root / "Workspace-Bench" / "evaluation" / "tasks_lite"


def _resolve_metadata_path(args: argparse.Namespace) -> Path:
    """Resolve and validate the requested task metadata path."""
    if args.metadata is not None:
        metadata_path = args.metadata.expanduser().resolve()
    else:
        task_id = str(args.task_id)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
            msg = f"Invalid task ID: {task_id!r}"
            raise JudgePromptError(msg)
        task_root = (
            args.task_root.expanduser()
            if args.task_root is not None
            else _default_task_root()
        )
        if task_root is None:
            msg = "Cannot locate task root; pass --task-root or --metadata."
            raise JudgePromptError(msg)
        metadata_path = (task_root / task_id / "metadata.json").resolve()
    if not metadata_path.is_file():
        msg = f"metadata.json not found: {metadata_path}"
        raise JudgePromptError(msg)
    return metadata_path


def _load_json_object(path: Path) -> dict[str, object]:
    """Load a UTF-8 JSON object.

    Args:
        path: JSON file to read.

    Returns:
        Parsed top-level object.

    Raises:
        JudgePromptError: If the file is invalid or is not an object.
    """
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        msg = f"Cannot read valid JSON from {path}: {exc}"
        raise JudgePromptError(msg) from exc
    if not isinstance(value, dict):
        msg = f"Expected a JSON object in {path}"
        raise JudgePromptError(msg)
    return value


def _load_jsonl(path: Path) -> tuple[list[dict[str, object]], list[str]]:
    """Load heterogeneous JSONL events without discarding malformed lines.

    Args:
        path: JSONL file produced by an external Runner.

    Returns:
        Parsed events and human-readable warnings.
    """
    if not path.is_file():
        msg = f"Runner JSONL log not found: {path}"
        raise JudgePromptError(msg)
    events: list[dict[str, object]] = []
    warnings: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        msg = f"Cannot read Runner JSONL log {path}: {exc}"
        raise JudgePromptError(msg) from exc

    for line_number, raw_line in enumerate(lines, start=1):
        text = raw_line.strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(f"Line {line_number} is not valid JSON: {exc.msg}")
            events.append(
                {
                    "_jsonlLine": line_number,
                    "parseError": exc.msg,
                    "raw": text,
                }
            )
            continue

        if isinstance(value, dict):
            event = dict(value)
            event.setdefault("_jsonlLine", line_number)
            events.append(event)
        elif isinstance(value, list):
            for item_index, item in enumerate(value):
                events.append(
                    {
                        "_jsonlLine": line_number,
                        "_listItem": item_index,
                        "event": item,
                    }
                )
        else:
            events.append({"_jsonlLine": line_number, "value": value})

    if not events:
        warnings.append("The JSONL log contains no non-empty events.")
    return events, warnings


def _deep_parse_json_strings(value: object, *, depth: int = 0) -> object:
    """Turn nested JSON strings into structured values without executing input.

    Args:
        value: Arbitrary JSON-compatible value.
        depth: Current recursion depth.

    Returns:
        A value with object- or array-looking JSON strings decoded.
    """
    if depth >= 6:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            return value
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        return _deep_parse_json_strings(parsed, depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): _deep_parse_json_strings(child, depth=depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _deep_parse_json_strings(child, depth=depth + 1)
            for child in value
        ]
    return value


def _source_event(event: dict[str, object]) -> tuple[dict[str, object], int | None]:
    """Unwrap list items created by `_load_jsonl` while retaining their line.

    Args:
        event: Loaded JSONL event.

    Returns:
        The source event and its original JSONL line number.
    """
    line = event.get("_jsonlLine")
    line_number = line if isinstance(line, int) else None
    nested = event.get("event")
    if isinstance(nested, dict) and "_listItem" in event:
        return dict(nested), line_number
    return event, line_number


def _first_value(mapping: dict[str, object], names: Sequence[str]) -> object | None:
    """Return the first non-empty value under a list of candidate keys.

    Args:
        mapping: Source event or payload.
        names: Candidate field names in priority order.

    Returns:
        The first present value, or `None`.
    """
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _detect_log_format(events: Sequence[dict[str, object]]) -> str:
    """Detect a supported Runner JSONL layout from representative events.

    Args:
        events: Parsed source events.

    Returns:
        `halo`, `event-stream`, or `generic`.
    """
    halo_matches = 0
    event_stream_matches = 0
    for loaded_event in events[:50]:
        event, _ = _source_event(loaded_event)
        attributes = event.get("attributes")
        if (
            isinstance(attributes, dict)
            and isinstance(event.get("trace_id"), str)
            and isinstance(event.get("span_id"), str)
        ):
            halo_matches += 1
        if isinstance(event.get("event"), str) and isinstance(event.get("payload"), dict):
            event_stream_matches += 1
    if halo_matches:
        return "halo"
    if event_stream_matches:
        return "event-stream"
    return "generic"


def _normalization_base(
    *,
    sequence: int,
    line_number: int | None,
    source_format: str,
    event_type: str,
    timestamp: object | None,
    raw_event: dict[str, object],
) -> dict[str, object]:
    """Build fields shared by every normalized event.

    Args:
        sequence: Stable one-based event sequence.
        line_number: Original JSONL line number when known.
        source_format: Detected or requested input format.
        event_type: Canonical event category.
        timestamp: Source timestamp when present.
        raw_event: Original source event.

    Returns:
        A normalized event retaining its source evidence.
    """
    normalized: dict[str, object] = {
        "schemaVersion": 1,
        "schema": _NORMALIZED_LOG_SCHEMA,
        "sequence": sequence,
        "eventType": event_type,
        "sourceFormat": source_format,
    }
    if line_number is not None:
        normalized["sourceLine"] = line_number
    if timestamp not in (None, ""):
        normalized["timestamp"] = timestamp
    normalized["rawEvent"] = raw_event
    return normalized


def _tool_result_is_error(payload: dict[str, object]) -> bool:
    """Detect common direct and nested tool failure signals.

    Args:
        payload: Tool result payload.

    Returns:
        `True` when the payload contains a recognized failure signal.
    """
    details_value = payload.get("details")
    details = details_value if isinstance(details_value, dict) else {}
    raw_value = details.get("raw")
    raw = raw_value if isinstance(raw_value, dict) else {}
    data_value = raw.get("data")
    data = data_value if isinstance(data_value, dict) else {}
    return bool(
        payload.get("is_error")
        or payload.get("success") is False
        or payload.get("ok") is False
        or payload.get("error")
        or details.get("ok") is False
        or details.get("error")
        or raw.get("ok") is False
        or raw.get("error")
        or data.get("success") is False
        or data.get("error")
    )


def _normalize_event_stream(
    event: dict[str, object],
    *,
    sequence: int,
    line_number: int | None,
) -> dict[str, object]:
    """Normalize a XiaoYi-style `event` + `payload` record.

    Args:
        event: Source event.
        sequence: Stable one-based event sequence.
        line_number: Original JSONL line number.

    Returns:
        Canonical Runner event.
    """
    parsed = _deep_parse_json_strings(event)
    source = parsed if isinstance(parsed, dict) else event
    if source.get("parseError"):
        normalized = _normalization_base(
            sequence=sequence,
            line_number=line_number,
            source_format="event-stream",
            event_type="parse_error",
            timestamp=None,
            raw_event=source,
        )
        normalized["error"] = source.get("parseError")
        return normalized
    event_name = str(source.get("event") or "event").strip().casefold()
    payload_value = source.get("payload")
    payload = payload_value if isinstance(payload_value, dict) else {}
    normalized = _normalization_base(
        sequence=sequence,
        line_number=line_number,
        source_format="event-stream",
        event_type=event_name,
        timestamp=_first_value(source, ("timestamp", "time", "created_at", "createdAt")),
        raw_event=source,
    )
    role = _first_value(source, ("agent_role", "role"))
    if isinstance(role, str):
        normalized["role"] = role

    if event_name == "model_input":
        normalized["content"] = {
            "messages": payload.get("messages", []),
            "systemPrompt": payload.get("system_prompt"),
            "tools": payload.get("tools", []),
        }
    elif event_name == "model_output":
        normalized["content"] = payload.get("assistant", payload)
    elif event_name == "tool_call":
        normalized["toolName"] = str(payload.get("tool_name") or "unknown")
        call_id = payload.get("tool_call_id")
        if call_id not in (None, ""):
            normalized["toolCallId"] = str(call_id)
        normalized["toolInput"] = payload.get("args", {})
    elif event_name == "tool_result":
        tool_name = payload.get("tool_name")
        if tool_name not in (None, ""):
            normalized["toolName"] = str(tool_name)
        call_id = payload.get("tool_call_id")
        if call_id not in (None, ""):
            normalized["toolCallId"] = str(call_id)
        normalized["toolOutput"] = payload
        normalized["toolIsError"] = _tool_result_is_error(payload)
    else:
        normalized["content"] = payload
    return normalized


def _normalize_halo_span(
    event: dict[str, object],
    *,
    sequence: int,
    line_number: int | None,
) -> dict[str, object]:
    """Normalize one HALO/OpenInference span into a canonical event.

    Args:
        event: HALO span.
        sequence: Stable one-based event sequence.
        line_number: Original JSONL line number.

    Returns:
        Canonical Runner event.
    """
    parsed = _deep_parse_json_strings(event)
    source = parsed if isinstance(parsed, dict) else event
    if source.get("parseError"):
        normalized = _normalization_base(
            sequence=sequence,
            line_number=line_number,
            source_format="halo",
            event_type="parse_error",
            timestamp=None,
            raw_event=source,
        )
        normalized["error"] = source.get("parseError")
        return normalized
    attributes_value = source.get("attributes")
    attributes = attributes_value if isinstance(attributes_value, dict) else {}
    observation = str(
        attributes.get("inference.observation_kind")
        or attributes.get("openinference.span.kind")
        or ""
    ).strip().casefold()
    event_type = {
        "agent": "agent_span",
        "llm": "model_interaction",
        "tool": "tool_span",
    }.get(observation, "span")
    normalized = _normalization_base(
        sequence=sequence,
        line_number=line_number,
        source_format="halo",
        event_type=event_type,
        timestamp=_first_value(source, ("start_time", "timestamp", "time")),
        raw_event=source,
    )
    for source_key, target_key in (
        ("trace_id", "traceId"),
        ("span_id", "spanId"),
        ("parent_span_id", "parentSpanId"),
    ):
        value = source.get(source_key)
        if value not in (None, ""):
            normalized[target_key] = value
    status = source.get("status")
    if status not in (None, ""):
        normalized["status"] = status

    if event_type == "tool_span":
        normalized["toolName"] = str(
            attributes.get("tool.name") or source.get("name") or "unknown"
        )
        call_id = attributes.get("tool.call.id")
        if call_id not in (None, ""):
            normalized["toolCallId"] = str(call_id)
        normalized["toolInput"] = attributes.get("input.value")
        normalized["toolOutput"] = attributes.get("output.value")
        status_code = ""
        if isinstance(status, dict):
            status_code = str(status.get("code") or "")
        is_error_value = attributes.get("tool.is_error")
        normalized["toolIsError"] = (
            is_error_value is True
            or (
                isinstance(is_error_value, str)
                and is_error_value.strip().casefold() in {"1", "true", "yes"}
            )
            or "error" in status_code.casefold()
        )
    elif event_type == "model_interaction":
        normalized["model"] = _first_value(
            attributes,
            ("inference.llm.model_name", "llm.model_name"),
        )
        normalized["content"] = {
            "inputMessages": attributes.get("llm.input_messages"),
            "outputMessages": attributes.get("llm.output_messages"),
            "systemPrompt": attributes.get("llm.system_prompt"),
            "tools": attributes.get("llm.tools"),
        }
        normalized["usage"] = {
            "inputTokens": attributes.get("inference.llm.input_tokens"),
            "outputTokens": attributes.get("inference.llm.output_tokens"),
            "totalTokens": attributes.get("llm.token_count.total"),
        }
    else:
        normalized["content"] = {
            "name": source.get("name"),
            "input": attributes.get("input.value"),
            "output": attributes.get("output.value"),
        }
    return normalized


def _generic_event_type(event: dict[str, object]) -> str:
    """Infer a canonical category for an otherwise unknown JSON event.

    Args:
        event: Source event.

    Returns:
        Best-effort canonical event type.
    """
    if event.get("parseError"):
        return "parse_error"
    role = _event_role(event)
    tool = _tool_name(event)
    if role in {"tool", "toolresult", "tool_result", "bashexecution"}:
        return "tool_result"
    if tool and any(key in event for key in ("result", "output", "response", "stderr")):
        return "tool_result"
    if tool and any(key in event for key in ("arguments", "args", "input", "parameters")):
        return "tool_call"
    if role in {"assistant", "user", "system"}:
        return "message"
    if any(key in event for key in ("error", "exception", "traceback")):
        return "error"
    candidate = _first_value(event, ("event", "type", "kind"))
    if isinstance(candidate, str) and candidate.strip():
        return re.sub(r"[^a-z0-9]+", "_", candidate.casefold()).strip("_") or "event"
    return "event"


def _normalize_generic_event(
    event: dict[str, object],
    *,
    sequence: int,
    line_number: int | None,
) -> dict[str, object]:
    """Normalize an unknown JSONL event without discarding its raw structure.

    Args:
        event: Source event.
        sequence: Stable one-based event sequence.
        line_number: Original JSONL line number.

    Returns:
        Canonical Runner event.
    """
    parsed = _deep_parse_json_strings(event)
    source = parsed if isinstance(parsed, dict) else event
    event_type = _generic_event_type(source)
    normalized = _normalization_base(
        sequence=sequence,
        line_number=line_number,
        source_format="generic",
        event_type=event_type,
        timestamp=_first_value(
            source,
            ("timestamp", "time", "created_at", "createdAt", "start_time"),
        ),
        raw_event=source,
    )
    role = _event_role(source)
    if role:
        normalized["role"] = role
    tool = _tool_name(source)
    if tool:
        normalized["toolName"] = tool
    call_id = _first_value(source, ("tool_call_id", "toolCallId", "call_id", "callId"))
    if call_id not in (None, ""):
        normalized["toolCallId"] = str(call_id)

    content = _first_value(source, ("content", "text", "message"))
    if content not in (None, ""):
        normalized["content"] = content
    tool_input = _first_value(source, ("arguments", "args", "input", "parameters"))
    if event_type in {"tool_call", "tool_result"} and tool_input not in (None, ""):
        normalized["toolInput"] = tool_input
    tool_output = _first_value(source, ("result", "output", "response", "stdout", "stderr"))
    if event_type == "tool_result" and tool_output not in (None, ""):
        normalized["toolOutput"] = tool_output
    error = _first_value(source, ("error", "exception", "traceback", "stderr"))
    if error not in (None, ""):
        normalized["error"] = error
    status = source.get("status")
    if status not in (None, ""):
        normalized["status"] = status
    return normalized


def _normalize_events(
    events: Sequence[dict[str, object]],
    *,
    requested_format: str,
) -> tuple[list[dict[str, object]], str, list[str]]:
    """Convert heterogeneous Runner events to one stable Judge-facing schema.

    Args:
        events: Parsed source JSONL events.
        requested_format: CLI selection from `_LOG_FORMAT_CHOICES`.

    Returns:
        Normalized events, detected format, and normalization warnings.

    Raises:
        JudgePromptError: If the requested format is unsupported.
    """
    if requested_format not in _LOG_FORMAT_CHOICES:
        msg = f"Unsupported log format: {requested_format!r}"
        raise JudgePromptError(msg)
    detected_format = _detect_log_format(events)
    selected_format = detected_format if requested_format == "auto" else requested_format
    if selected_format == "xiaoyi":
        selected_format = "event-stream"
    warnings: list[str] = []
    if requested_format != "auto" and selected_format != detected_format:
        warnings.append(
            f"Requested log format {requested_format!r} differs from auto-detected "
            f"format {detected_format!r}; the requested adapter was used."
        )

    normalized: list[dict[str, object]] = []
    for sequence, loaded_event in enumerate(events, start=1):
        source, line_number = _source_event(loaded_event)
        if selected_format == "halo":
            item = _normalize_halo_span(
                source,
                sequence=sequence,
                line_number=line_number,
            )
        elif selected_format == "event-stream":
            item = _normalize_event_stream(
                source,
                sequence=sequence,
                line_number=line_number,
            )
        else:
            item = _normalize_generic_event(
                source,
                sequence=sequence,
                line_number=line_number,
            )
        normalized.append(item)
    return normalized, detected_format, warnings


def _sanitize_text(text: str) -> str:
    """Remove common credential forms from free-form text."""
    sanitized = text
    for pattern, replacement in _SECRET_TEXT_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def _is_secret_key(key: str) -> bool:
    """Identify credential fields without hiding harmless token-count metrics."""
    parts = re.split(r"[./]", key.casefold())
    normalized_parts = [
        re.sub(r"[^a-z0-9]", "", part)
        for part in parts
    ]
    sensitive_suffixes = (
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "password",
        "refreshtoken",
        "sessiontoken",
    )
    return any(
        part in _SECRET_KEY_NAMES
        or any(part.endswith(suffix) for suffix in sensitive_suffixes)
        for part in normalized_parts
    )


def _sanitize_value(value: object, *, key: str | None = None) -> object:
    """Recursively sanitize secrets while retaining evidence structure."""
    if key is not None and _is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        return {
            str(child_key): _sanitize_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _tool_name(event: dict[str, object]) -> str:
    """Best-effort extraction of a tool name from common JSONL formats."""
    candidates: list[object] = [
        event.get("tool"),
        event.get("tool_name"),
        event.get("toolName"),
        event.get("name"),
    ]
    attributes = event.get("attributes")
    if isinstance(attributes, dict):
        candidates.extend(
            [
                attributes.get("tool.name"),
                attributes.get("function.name"),
                attributes.get("openinference.tool.name"),
            ]
        )
    message = event.get("message")
    if isinstance(message, dict):
        candidates.extend(
            [
                message.get("name"),
                message.get("tool"),
                message.get("toolName"),
                message.get("tool_name"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return ""


def _event_role(event: dict[str, object]) -> str:
    """Extract a role from either the event or a nested session message."""
    role = event.get("role")
    if isinstance(role, str):
        return role.strip().casefold()
    message = event.get("message")
    if isinstance(message, dict):
        nested_role = message.get("role")
        if isinstance(nested_role, str):
            return nested_role.strip().casefold()
    return ""


def _redact_output_fields(value: object) -> object:
    """Redact source and command output fields but preserve tool identity."""
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in _TOOL_OUTPUT_KEYS:
                redacted[key] = "[tool output redacted from Judge view]"
            else:
                redacted[key] = _redact_output_fields(child)
        return redacted
    if isinstance(value, list):
        return [_redact_output_fields(item) for item in value]
    return value


def _sanitize_event(event: dict[str, object]) -> dict[str, object]:
    """Sanitize one event and hide source-reading or shell output payloads."""
    sanitized = _sanitize_value(event)
    if not isinstance(sanitized, dict):
        return {"event": sanitized}
    tool = _tool_name(event)
    role = _event_role(event)
    should_redact_tool_output = (
        any(token in tool for token in (*_READ_TOOL_TOKENS, *_EXECUTE_TOOL_TOKENS))
        or role in {"tool", "toolresult", "tool_result", "bashexecution"}
    )
    if should_redact_tool_output:
        redacted = _redact_output_fields(sanitized)
        if isinstance(redacted, dict):
            return redacted
    return sanitized


def _truncate_value(
    value: object,
    *,
    max_str_len: int,
    depth: int = 0,
    max_depth: int = 4,
    max_list_items: int = 20,
) -> object:
    """Bound nested trace evidence before embedding it in a prompt."""
    if isinstance(value, str):
        if len(value) <= max_str_len:
            return value
        return value[:max_str_len] + "...[truncated]"
    if depth >= max_depth and isinstance(value, (dict, list)):
        return "[max depth reached]"
    if isinstance(value, dict):
        return {
            str(key): _truncate_value(
                child,
                max_str_len=max_str_len,
                depth=depth + 1,
                max_depth=max_depth,
                max_list_items=max_list_items,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        items = [
            _truncate_value(
                item,
                max_str_len=max_str_len,
                depth=depth + 1,
                max_depth=max_depth,
                max_list_items=max_list_items,
            )
            for item in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            items.append(f"...[truncated {len(value) - max_list_items} more items]")
        return items
    return value


def _select_trace_events(
    events: list[dict[str, object]],
    *,
    max_items: int,
    max_str_len: int,
) -> list[object]:
    """Keep both the beginning and end of a long execution trace."""
    if max_items < 1:
        msg = "--max-trace-items must be at least 1"
        raise JudgePromptError(msg)
    if len(events) <= max_items:
        selected: list[object] = list(events)
    else:
        head_count = (max_items + 1) // 2
        tail_count = max_items - head_count
        selected = list(events[:head_count])
        selected.append(
            {"note": f"...[truncated {len(events) - max_items} middle events]"}
        )
        if tail_count:
            selected.extend(events[-tail_count:])
    return [
        _truncate_value(item, max_str_len=max_str_len)
        for item in selected
    ]


def _normalize_filename_key(name: str) -> str:
    """Normalize expected output names using the native Judge's weak match."""
    value = str(name or "")
    for noise in (
        " ",
        "　",
        "\t",
        "\n",
        "’",
        "'",
        "“",
        "”",
        '"',
        "（",
        "）",
        "(",
        ")",
    ):
        value = value.replace(noise, "")
    return value.casefold()


def _safe_directory_outputs(
    directory: Path,
    *,
    expected_names: Sequence[str],
) -> tuple[list[Path], int]:
    """Collect only a dedicated output tree and exact expected-name matches."""
    candidates: list[Path] = []
    ignored = 0
    expected_keys = {
        _normalize_filename_key(Path(name).name)
        for name in expected_names
        if name.strip()
    }
    dedicated_output = (
        directory
        if directory.name.casefold() in {"output", "outputs"}
        else directory / "output"
    )
    dedicated_files: set[Path] = set()
    if dedicated_output.is_dir():
        dedicated_files = {
            path.resolve()
            for path in dedicated_output.rglob("*")
            if path.is_file()
        }
        candidates.extend(sorted(dedicated_files))

    if expected_keys and directory.is_dir():
        judge_artifacts = directory / "judge_artifacts"
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in dedicated_files or judge_artifacts in path.parents:
                continue
            if _normalize_filename_key(path.name) in expected_keys:
                candidates.append(resolved)
            else:
                ignored += 1
    elif dedicated_output != directory:
        ignored = sum(
            1
            for path in directory.rglob("*")
            if path.is_file() and path.resolve() not in dedicated_files
        )
    return candidates, ignored


def _expand_output_paths(
    paths: Sequence[Path],
    *,
    metadata: dict[str, object],
) -> tuple[list[Path], list[str]]:
    """Expand Runner paths without recursively exposing input/source files."""
    files: list[Path] = []
    warnings: list[str] = []
    seen: set[Path] = set()
    expected_names = _expected_output_names(metadata)
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates, ignored = _safe_directory_outputs(
                path,
                expected_names=expected_names,
            )
            if ignored:
                warnings.append(
                    f"Ignored {ignored} non-output file(s) below {path.name!r}. "
                    "Directories are restricted to output/ and expected filenames; "
                    "pass an individual file explicitly to include it."
                )
        else:
            warnings.append(f"Runner output path does not exist: {path.name!r}")
            continue
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                files.append(candidate)
    return files, warnings


def _sha256_file(path: Path) -> str:
    """Calculate a file digest without loading the entire file into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        msg = f"Could not hash {path}: {exc}"
        raise JudgePromptError(msg) from exc
    return digest.hexdigest()


def _copy_outputs_to_bundle(
    files: Sequence[Path],
    *,
    out_dir: Path,
) -> tuple[list[Path], list[dict[str, object]]]:
    """Copy selected outputs into numbered folders for portable upload."""
    if not files:
        return [], []
    output_root = out_dir / "runner_outputs"
    output_root.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    entries: list[dict[str, object]] = []
    for index, source in enumerate(files, start=1):
        destination_dir = output_root / f"{index:03d}"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            msg = f"Could not copy Runner output {source}: {exc}"
            raise JudgePromptError(msg) from exc
        copied.append(destination)
        entries.append(
            {
                "sourceName": source.name,
                "bundlePath": destination.relative_to(out_dir).as_posix(),
                "sizeBytes": destination.stat().st_size,
                "sha256": _sha256_file(destination),
            }
        )
    return copied, entries


def _default_eval_src() -> Path | None:
    """Resolve Workspace-Bench evaluation source when this is a monorepo checkout."""
    configured = os.environ.get("WB_EVAL_SRC")
    if configured:
        return Path(configured).expanduser()
    repo_root = _find_repo_root(Path(__file__).resolve())
    if repo_root is None:
        return None
    return repo_root / "Workspace-Bench" / "evaluation" / "src"


def _load_agent_eval(eval_src: Path | None) -> tuple[ModuleType | None, str | None]:
    """Load the native rich excerpt reader without requiring it."""
    if eval_src is None:
        return None, "Workspace-Bench evaluation/src was not found; using text-only excerpts."
    module_path = eval_src.expanduser().resolve() / "agent_eval.py"
    if not module_path.is_file():
        return (
            None,
            "Native agent_eval.py was not found; using text-only excerpts.",
        )
    try:
        spec = importlib.util.spec_from_file_location(
            "_workspace_bench_agent_eval_for_prompt",
            module_path,
        )
        if spec is None or spec.loader is None:
            return None, "Could not create an import spec for agent_eval.py."
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, None
    except (ImportError, OSError, RuntimeError) as exc:
        return None, f"Could not load native rich excerpt reader: {exc}"


def _decode_text_excerpt(path: Path, *, max_bytes: int) -> tuple[str | None, str | None]:
    """Read a safe excerpt from a likely text output."""
    mime, _ = mimetypes.guess_type(path.name)
    is_likely_text = path.suffix.lower() in _TEXT_EXTENSIONS or (
        isinstance(mime, str) and mime.startswith("text/")
    )
    if not is_likely_text:
        return None, "Binary or unsupported file; attach the original file to the Judge model."
    try:
        data = path.read_bytes()[:max_bytes]
    except OSError as exc:
        return None, f"Could not read output: {exc}"
    if b"\x00" in data:
        return None, "Binary content detected; attach the original file to the Judge model."
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            text = data.decode(encoding)
            return text, None
        except UnicodeDecodeError:
            continue
    return None, "Could not decode text output; attach the original file."


def _native_excerpt(
    module: ModuleType,
    path: Path,
) -> tuple[str | None, str | None, bool, str | None]:
    """Call the native Workspace-Bench rich excerpt reader when available."""
    reader = getattr(module, "_read_rich_excerpt", None)
    mime_reader = getattr(module, "_guess_mime", None)
    if not callable(reader):
        msg = "Native agent_eval does not expose _read_rich_excerpt."
        raise JudgePromptError(msg)
    value = reader(str(path))
    if not isinstance(value, tuple) or len(value) != 3:
        msg = "Native _read_rich_excerpt returned an unexpected value."
        raise JudgePromptError(msg)
    excerpt, image_data_url, note = value
    mime = mime_reader(str(path)) if callable(mime_reader) else mimetypes.guess_type(path.name)[0]
    return (
        excerpt if isinstance(excerpt, str) else None,
        mime if isinstance(mime, str) else None,
        isinstance(image_data_url, str) and image_data_url.startswith("data:image/"),
        note if isinstance(note, str) else None,
    )


def _common_output_root(files: Sequence[Path]) -> Path | None:
    """Find a display root shared by all supplied output files."""
    if not files:
        return None
    try:
        common = Path(os.path.commonpath([str(path.parent) for path in files]))
    except ValueError:
        return None
    return common


def _expected_output_names(metadata: dict[str, object]) -> list[str]:
    """Extract expected output names from Workspace-Bench metadata."""
    values: list[str] = []
    output_files = metadata.get("output_files")
    if isinstance(output_files, list):
        values.extend(
            str(item).strip()
            for item in output_files
            if isinstance(item, str) and item.strip()
        )
    output_file = metadata.get("output_file")
    if isinstance(output_file, str) and output_file.strip():
        values.append(output_file.strip())
    return sorted(set(values))


def _collect_output_evidence(
    files: Sequence[Path],
    *,
    metadata: dict[str, object],
    native_module: ModuleType | None,
    max_output_bytes: int,
) -> tuple[list[dict[str, object]], list[str], str | None]:
    """Build prompt-safe evidence records for external Runner outputs."""
    warnings: list[str] = []
    root = _common_output_root(files)
    expected = _expected_output_names(metadata)
    expected_basenames = {Path(name).name.casefold() for name in expected}
    ordered = sorted(
        files,
        key=lambda path: (
            0 if path.name.casefold() in expected_basenames else 1,
            path.name.casefold(),
        ),
    )
    records: list[dict[str, object]] = []
    for path in ordered:
        display_path = path.name
        if root is not None:
            try:
                display_path = path.relative_to(root).as_posix()
            except ValueError:
                display_path = path.name
        excerpt: str | None
        mime: str | None
        has_image = False
        note: str | None
        if native_module is not None:
            try:
                excerpt, mime, has_image, note = _native_excerpt(native_module, path)
            except (JudgePromptError, OSError, RuntimeError, ValueError) as exc:
                warnings.append(f"Native excerpt failed for {path.name}: {exc}")
                excerpt, note = _decode_text_excerpt(path, max_bytes=max_output_bytes)
                mime = mimetypes.guess_type(path.name)[0]
        else:
            excerpt, note = _decode_text_excerpt(path, max_bytes=max_output_bytes)
            mime = mimetypes.guess_type(path.name)[0]
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        records.append(
            {
                "path": display_path,
                "relToWorkDir": display_path,
                "sizeBytes": size,
                "excerpt": excerpt,
                "mime": mime,
                "hasImage": has_image,
                "note": note,
            }
        )

    provided_basenames = {path.name.casefold() for path in files}
    for expected_name in expected:
        if Path(expected_name).name.casefold() not in provided_basenames:
            warnings.append(f"Expected output was not supplied: {expected_name}")
    return records, warnings, root.name if root is not None else None


def _rubric_items(metadata: dict[str, object]) -> list[dict[str, object]]:
    """Convert metadata rubrics to the native Judge prompt structure."""
    raw_rubrics = metadata.get("rubrics")
    if not isinstance(raw_rubrics, list) or not raw_rubrics:
        msg = "metadata.json does not contain a non-empty rubrics list."
        raise JudgePromptError(msg)
    rubric_types = metadata.get("rubric_types")
    rubric_diffs = metadata.get("rubric_diffs")
    types = rubric_types if isinstance(rubric_types, list) else []
    diffs = rubric_diffs if isinstance(rubric_diffs, list) else []
    items: list[dict[str, object]] = []
    for index, rubric in enumerate(raw_rubrics):
        if not isinstance(rubric, str):
            continue
        rubric_type = types[index] if index < len(types) and isinstance(types[index], str) else None
        rubric_diff = diffs[index] if index < len(diffs) and isinstance(diffs[index], str) else None
        items.append(
            {
                "index": index,
                "rubric": rubric,
                "rubricType": rubric_type,
                "rubricDiff": rubric_diff,
            }
        )
    if not items:
        msg = "metadata.json contains no string rubrics."
        raise JudgePromptError(msg)
    return items


def _task_id(metadata: dict[str, object], metadata_path: Path) -> str:
    """Resolve a stable task identifier for the prompt."""
    for key in ("id", "absolute_id", "task_id"):
        value = metadata.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return metadata_path.parent.name


def _build_user_prompt(
    *,
    task_id: str,
    metadata: dict[str, object],
    output_records: Sequence[dict[str, object]],
    work_dir: str | None,
    trace_summary: dict[str, object],
    warnings: Sequence[str],
    max_output_files: int,
    max_str_len: int,
) -> str:
    """Build a portable form of the native Workspace-Bench grading prompt."""
    if max_output_files < 1:
        msg = "--max-output-files must be at least 1"
        raise JudgePromptError(msg)
    included_outputs: list[object] = []
    for record in output_records[:max_output_files]:
        item = dict(record)
        excerpt = item.get("excerpt")
        if isinstance(excerpt, str) and len(excerpt) > max_str_len:
            item["excerpt"] = excerpt[:max_str_len] + "...[truncated]"
        included_outputs.append(item)
    if len(output_records) > max_output_files:
        included_outputs.append(
            {"note": f"...[truncated {len(output_records) - max_output_files} more files]"}
        )

    payload: dict[str, object] = {
        "taskId": task_id,
        "task": metadata.get("task"),
        "steps": metadata.get("steps"),
        "expectedOutputs": _expected_output_names(metadata),
        "workDir": work_dir,
        "outputs": included_outputs,
        "traceSummary": trace_summary,
        "rubrics": _rubric_items(metadata),
        "evidenceWarnings": list(warnings),
    }
    instructions = (
        "请你作为严格评测员，基于给定 JSON 中的 task/outputs/traceSummary "
        "来判断每条 rubrics 是否满足。\n"
        "要求：\n"
        "1) 只能依据给定证据，不要凭空假设。\n"
        "2) **强制要求**：当实际输出文件名与 expectedOutputs 不符但内容功能等价时，仍必须检查该实际文件的内容来判断 rubric。文件名差异本身不应导致 rubric 失败。同时，若 excerpt 被截断，应结合 traceSummary 中的工具调用结果综合判断。\n"
        "3) 每条 rubric 输出 passed(true/false) + evidence"
        "(字符串，引用到具体文件/片段或工具调用) + confidence(0-1)。\n"
        '4) 输出必须是 JSON 对象，格式：{ "rubrics": '
        '[{"index":0,"passed":true,"confidence":0.8,"evidence":"..."}] }\n'
        "5) 如果证据不足，请 passed=false 且 evidence 写明缺失证据。\n"
        "6) Runner 在日志中的自述不能替代实际输出文件证据；两者冲突时，以实际输出为准。\n"
        "7) normalized_runner_log.jsonl 是统一格式的补充执行证据，"
        "sanitized_runner_log.jsonl 是保留原结构的脱敏审计日志。"
        "不要因为日志声称任务完成就直接判通过。\n"
        "8) 如果输出是二进制文件且 Prompt 中没有可用摘录，请检查随 Prompt 上传的实际输出附件；"
        "若没有附件，应将相关 rubric 判为证据不足。\n\n"
    )
    return instructions + json.dumps(payload, ensure_ascii=False, indent=2)


def _prepare_out_dir(path: Path, *, overwrite: bool) -> Path:
    """Create the bundle directory without silently replacing prior evidence."""
    resolved = path.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()) and not overwrite:
        msg = f"Output directory is not empty: {resolved}; pass --overwrite or use a new directory."
        raise JudgePromptError(msg)
    resolved.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for filename in (
            "judge_messages.json",
            "judge_prompt.jsonl",
            "judge_prompt.txt",
            "manifest.json",
            "normalized_runner_log.jsonl",
            "sanitized_runner_log.jsonl",
        ):
            artifact = resolved / filename
            if artifact.is_file():
                artifact.unlink()
        runner_outputs = resolved / "runner_outputs"
        if runner_outputs.is_dir():
            shutil.rmtree(runner_outputs)
    return resolved


def _write_json(path: Path, value: object) -> None:
    """Write deterministic UTF-8 JSON."""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_bundle(args: argparse.Namespace) -> dict[str, object]:
    """Generate all files needed for portable manual Judge evaluation.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Bundle manifest written to disk.
    """
    if args.max_str_len < 1 or args.max_output_bytes < 1:
        msg = "--max-str-len and --max-output-bytes must be positive."
        raise JudgePromptError(msg)

    metadata_path = _resolve_metadata_path(args)
    metadata = _load_json_object(metadata_path)
    log_path = args.log_jsonl.expanduser().resolve()
    raw_events, warnings = _load_jsonl(log_path)
    requested_log_format = getattr(args, "log_format", "auto")
    normalized_events, detected_log_format, normalization_warnings = _normalize_events(
        raw_events,
        requested_format=requested_log_format,
    )
    applied_log_format = (
        detected_log_format
        if requested_log_format == "auto"
        else "event-stream"
        if requested_log_format == "xiaoyi"
        else requested_log_format
    )
    warnings.extend(normalization_warnings)
    sanitized_events = [_sanitize_event(event) for event in raw_events]
    normalized_sanitized_events = [
        _sanitize_event(event)
        for event in normalized_events
    ]
    out_dir = _prepare_out_dir(args.out_dir, overwrite=args.overwrite)

    output_files, output_path_warnings = _expand_output_paths(
        args.runner_output,
        metadata=metadata,
    )
    warnings.extend(output_path_warnings)
    if not output_files:
        warnings.append(
            "No Runner output files were supplied. Result-based rubrics cannot "
            "be judged reliably from logs alone."
        )
    copied_output_files, copied_output_entries = _copy_outputs_to_bundle(
        output_files,
        out_dir=out_dir,
    )

    requested_eval_src = args.eval_src if args.eval_src is not None else _default_eval_src()
    native_module, native_warning = _load_agent_eval(requested_eval_src)
    if native_warning:
        warnings.append(native_warning)
    output_records, output_warnings, work_dir = _collect_output_evidence(
        copied_output_files,
        metadata=metadata,
        native_module=native_module,
        max_output_bytes=args.max_output_bytes,
    )
    warnings.extend(output_warnings)

    selected_trace = _select_trace_events(
        normalized_sanitized_events,
        max_items=args.max_trace_items,
        max_str_len=args.max_str_len,
    )
    trace_summary: dict[str, object] = {
        "format": "jsonl",
        "schema": _NORMALIZED_LOG_SCHEMA,
        "sourceFormat": applied_log_format,
        "detectedSourceFormat": detected_log_format,
        "sourceFile": log_path.name,
        "eventCount": len(normalized_sanitized_events),
        "includedEventCount": min(
            len(normalized_sanitized_events),
            args.max_trace_items,
        ),
        "executionTrace": selected_trace,
        "fullNormalizedTraceAttachment": "normalized_runner_log.jsonl",
        "fullSanitizedTraceAttachment": "sanitized_runner_log.jsonl",
        "sanitizedSourceTrace": "sanitized_runner_log.jsonl",
    }

    resolved_task_id = _task_id(metadata, metadata_path)
    user_prompt = _build_user_prompt(
        task_id=resolved_task_id,
        metadata=metadata,
        output_records=output_records,
        work_dir=work_dir,
        trace_summary=trace_summary,
        warnings=warnings,
        max_output_files=args.max_output_files,
        max_str_len=args.max_str_len,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    sanitized_log_path = out_dir / "sanitized_runner_log.jsonl"
    sanitized_log_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False) + "\n"
            for event in sanitized_events
        ),
        encoding="utf-8",
    )
    normalized_log_path = out_dir / "normalized_runner_log.jsonl"
    normalized_log_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False) + "\n"
            for event in normalized_sanitized_events
        ),
        encoding="utf-8",
    )
    prompt_path = out_dir / "judge_prompt.txt"
    prompt_path.write_text(
        f"=== SYSTEM ===\n{SYSTEM_PROMPT}\n\n=== USER ===\n{user_prompt}\n",
        encoding="utf-8",
    )
    _write_json(out_dir / "judge_messages.json", messages)
    prompt_record: dict[str, object] = {
        "schemaVersion": 1,
        "taskId": resolved_task_id,
        "messages": messages,
        "attachments": [
            "normalized_runner_log.jsonl",
            "sanitized_runner_log.jsonl",
            *[
                str(entry["bundlePath"])
                for entry in copied_output_entries
                if isinstance(entry.get("bundlePath"), str)
            ],
        ],
    }
    prompt_jsonl_path = out_dir / "judge_prompt.jsonl"
    prompt_jsonl_path.write_text(
        json.dumps(prompt_record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    bundle_artifact_names = (
        "judge_prompt.txt",
        "judge_messages.json",
        "judge_prompt.jsonl",
        "normalized_runner_log.jsonl",
        "sanitized_runner_log.jsonl",
    )

    manifest: dict[str, object] = {
        "version": 1,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "taskId": resolved_task_id,
        "inputs": {
            "metadata": metadata_path.name,
            "runnerLog": log_path.name,
            "runnerOutputs": [path.name for path in output_files],
        },
        "bundle": {
            "judgePrompt": "judge_prompt.txt",
            "judgeMessages": "judge_messages.json",
            "judgePromptJsonl": "judge_prompt.jsonl",
            "normalizedRunnerLog": "normalized_runner_log.jsonl",
            "sanitizedRunnerLog": "sanitized_runner_log.jsonl",
            "runnerOutputs": "runner_outputs/",
        },
        "logNormalization": {
            "schema": _NORMALIZED_LOG_SCHEMA,
            "requestedFormat": requested_log_format,
            "detectedFormat": detected_log_format,
            "appliedFormat": applied_log_format,
            "normalizedEvents": len(normalized_sanitized_events),
            "rawSourcePreservedInNormalizedEvents": True,
        },
        "runnerOutputCopies": copied_output_entries,
        "artifactSha256": {
            name: _sha256_file(out_dir / name)
            for name in bundle_artifact_names
        },
        "counts": {
            "rubrics": len(_rubric_items(metadata)),
            "logEvents": len(raw_events),
            "normalizedLogEvents": len(normalized_sanitized_events),
            "promptTraceEvents": min(
                len(normalized_sanitized_events),
                args.max_trace_items,
            ),
            "runnerOutputFiles": len(output_files),
            "promptOutputFiles": min(len(output_records), args.max_output_files),
        },
        "warnings": warnings,
        "manualJudgeInstructions": [
            "Upload judge_prompt.txt.",
            "Upload normalized_runner_log.jsonl when the model supports file attachments.",
            "sanitized_runner_log.jsonl preserves the sanitized source layout for auditing.",
            "Upload every file below runner_outputs/; logs alone are not "
            "sufficient for result rubrics.",
            "Ask the model to return only the JSON object requested by judge_prompt.txt.",
            "Compute score as passed rubric count divided by total rubric count.",
        ],
        "redaction": {
            "credentialFields": True,
            "commonCredentialStrings": True,
            "sourceReadAndShellOutputs": True,
            "originalLogUnchanged": True,
            "normalizedLogSanitized": True,
        },
    }
    _write_json(out_dir / "manifest.json", manifest)
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """Run the prompt bundle generator.

    Args:
        argv: Optional command-line arguments. Uses `sys.argv` when omitted.

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    try:
        manifest = generate_bundle(args)
    except JudgePromptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir.expanduser().resolve()
    print(f"Judge bundle created: {out_dir}")
    print(f"Task: {manifest['taskId']}")
    print(f"Prompt: {out_dir / 'judge_prompt.txt'}")
    print(f"Normalized log: {out_dir / 'normalized_runner_log.jsonl'}")
    print(f"Sanitized log: {out_dir / 'sanitized_runner_log.jsonl'}")
    warnings = manifest.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
