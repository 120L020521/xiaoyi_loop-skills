"""Convert event+payload JSONL logs into full-detail HALO traces.

This variant preserves the original event payload content as much as possible,
but recursively parses JSON-looking strings so nested tool results are less
escaped. Use it when you want full information without the worst "\\\\\\" noise.

Input may be either one JSONL file or a directory. When a directory is provided,
all ``*.jsonl`` files under it are converted into the output directory while
preserving relative paths.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


Json = dict[str, Any]


def jsonish(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def halo_time(value: Any) -> str:
    if value in (None, ""):
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
    text = str(value).strip()
    if text.isdigit():
        return halo_time(int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def maybe_parse_json_string(value: str) -> Any:
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def deep_parse_json_strings(value: Any, *, max_depth: int = 6) -> Any:
    """Recursively parse JSON strings while preserving the represented content."""
    if max_depth <= 0:
        return value
    if isinstance(value, str):
        parsed = maybe_parse_json_string(value)
        if parsed is value:
            return value
        return deep_parse_json_strings(parsed, max_depth=max_depth - 1)
    if isinstance(value, list):
        return [deep_parse_json_strings(item, max_depth=max_depth) for item in value]
    if isinstance(value, dict):
        return {
            key: deep_parse_json_strings(item, max_depth=max_depth)
            for key, item in value.items()
        }
    return value


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
) -> Json:
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
    for row in rows:
        if row.get("session_id"):
            return str(row["session_id"])
    return fallback


def tool_status(payload: Json) -> tuple[str, bool]:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    raw = details.get("raw") if isinstance(details.get("raw"), dict) else {}
    error = details.get("error")
    error_message = ""
    if isinstance(error, dict):
        error_message = str(error.get("message") or "")
    elif error:
        error_message = str(error)
    raw_data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    if not error_message and isinstance(raw_data, dict):
        error_message = str(raw_data.get("error") or "")
    failed = bool(
        payload.get("is_error")
        or details.get("ok") is False
        or raw.get("ok") is False
        or (isinstance(raw_data, dict) and raw_data.get("success") is False)
        or error_message
    )
    return error_message, failed


def validate_input(rows: list[Json]) -> None:
    if not rows:
        raise ValueError("input JSONL is empty")
    for index, row in enumerate(rows, 1):
        if not isinstance(row.get("event"), str):
            raise ValueError(f"line {index}: missing string field 'event'")
        if not isinstance(row.get("payload"), dict):
            raise ValueError(f"line {index}: missing object field 'payload'")


def validate_span(row: Json, index: int) -> None:
    for key in ("trace_id", "span_id", "name", "kind", "start_time", "end_time", "status", "resource", "scope", "attributes"):
        if key not in row:
            raise ValueError(f"output line {index}: missing {key}")
    attrs = row["attributes"]
    for key in ("inference.export.schema_version", "inference.project_id", "inference.observation_kind"):
        if key not in attrs:
            raise ValueError(f"output line {index}: missing attributes.{key}")


def llm_span(row: Json, pending_model: Json | None, trace_id: str, agent_span_id: str, project_id: str) -> Json:
    payload = row["payload"]
    input_payload = pending_model["payload"] if pending_model else {}
    assistant = payload.get("assistant") if isinstance(payload.get("assistant"), dict) else {}
    usage = assistant.get("usage") if isinstance(assistant.get("usage"), dict) else {}
    model = assistant.get("model") or "model"
    provider = assistant.get("provider") or assistant.get("api")
    attrs = {
        **base_attrs(project_id, "LLM"),
        "llm.input_messages": jsonish(deep_parse_json_strings(input_payload.get("messages", []))),
        "llm.output_messages": jsonish(deep_parse_json_strings([assistant] if assistant else payload)),
        "llm.tools": jsonish(deep_parse_json_strings(input_payload.get("tools", []))),
        "llm.system_prompt": str(input_payload.get("system_prompt") or ""),
    }
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

    start_time = halo_time(pending_model["row"].get("timestamp")) if pending_model else halo_time(row.get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=agent_span_id,
        name=f"response.{model}",
        kind="SPAN_KIND_CLIENT",
        start_time=start_time,
        end_time=halo_time(row.get("timestamp")),
        status_code="STATUS_CODE_OK",
        status_message="",
        attrs=attrs,
    )


def unfinished_llm_span(pending_model: Json, trace_id: str, agent_span_id: str, project_id: str) -> Json:
    payload = pending_model["payload"]
    ts = halo_time(pending_model["row"].get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=str(uuid.uuid4()),
        parent_span_id=agent_span_id,
        name="response.model.unfinished",
        kind="SPAN_KIND_CLIENT",
        start_time=ts,
        end_time=ts,
        status_code="STATUS_CODE_ERROR",
        status_message="model_input has no matching model_output",
        attrs={
            **base_attrs(project_id, "LLM"),
            "llm.input_messages": jsonish(deep_parse_json_strings(payload.get("messages", []))),
            "llm.output_messages": "",
            "llm.tools": jsonish(deep_parse_json_strings(payload.get("tools", []))),
            "llm.system_prompt": str(payload.get("system_prompt") or ""),
        },
    )


def tool_span(row: Json, pending_tools: dict[str, Json], trace_id: str, agent_span_id: str, project_id: str) -> Json:
    payload = row["payload"]
    call_id = str(payload.get("tool_call_id") or uuid.uuid4())
    call = pending_tools.pop(call_id, None)
    call_payload = call["payload"] if call else {}
    tool_name = payload.get("tool_name") or call_payload.get("tool_name") or "tool"
    error_message, failed = tool_status(payload)
    start_time = halo_time(call["row"].get("timestamp")) if call else halo_time(row.get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=call_id,
        parent_span_id=agent_span_id,
        name=f"function.{tool_name}",
        kind="SPAN_KIND_INTERNAL",
        start_time=start_time,
        end_time=halo_time(row.get("timestamp")),
        status_code="STATUS_CODE_ERROR" if failed else "STATUS_CODE_OK",
        status_message=error_message,
        attrs={
            **base_attrs(project_id, "TOOL"),
            "tool.name": str(tool_name),
            "tool.call.id": call_id,
            "input.value": jsonish(deep_parse_json_strings(call_payload.get("args", {}))),
            "output.value": jsonish(deep_parse_json_strings(payload)),
            "tool.is_error": failed,
        },
    )


def unmatched_tool_span(call_id: str, call: Json, trace_id: str, agent_span_id: str, project_id: str) -> Json:
    payload = call["payload"]
    tool_name = payload.get("tool_name") or "tool"
    ts = halo_time(call["row"].get("timestamp"))
    return span(
        trace_id=trace_id,
        span_id=call_id,
        parent_span_id=agent_span_id,
        name=f"function.{tool_name}",
        kind="SPAN_KIND_INTERNAL",
        start_time=ts,
        end_time=ts,
        status_code="STATUS_CODE_ERROR",
        status_message="tool_call has no matching tool_result",
        attrs={
            **base_attrs(project_id, "TOOL"),
            "tool.name": str(tool_name),
            "tool.call.id": call_id,
            "input.value": jsonish(deep_parse_json_strings(payload.get("args", {}))),
            "output.value": "",
            "tool.is_error": True,
        },
    )


def convert_events(rows: list[Json], project_id: str, default_trace_id: str) -> list[Json]:
    validate_input(rows)
    trace_id = trace_id_from(rows, default_trace_id)
    agent_span_id = str(uuid.uuid4())
    agent_role = str(rows[0].get("agent_role") or "main")
    child_spans: list[Json] = []
    pending_model: Json | None = None
    pending_tools: dict[str, Json] = {}
    agent_attrs = {
        **base_attrs(project_id, "AGENT"),
        "agent.name": agent_role,
        "session.id": str(rows[0].get("session_id") or ""),
    }

    for row in rows:
        event = row["event"]
        payload = row["payload"]
        if event == "model_input":
            pending_model = {"row": row, "payload": payload}
            agent_attrs.setdefault("input.value", jsonish(deep_parse_json_strings(payload.get("messages", []))))
        elif event == "model_output":
            child_spans.append(llm_span(row, pending_model, trace_id, agent_span_id, project_id))
            pending_model = None
        elif event == "tool_call":
            call_id = str(payload.get("tool_call_id") or uuid.uuid4())
            pending_tools[call_id] = {"row": row, "payload": payload}
        elif event == "tool_result":
            child_spans.append(tool_span(row, pending_tools, trace_id, agent_span_id, project_id))

    if pending_model:
        child_spans.append(unfinished_llm_span(pending_model, trace_id, agent_span_id, project_id))
    for call_id, call in pending_tools.items():
        child_spans.append(unmatched_tool_span(call_id, call, trace_id, agent_span_id, project_id))

    if child_spans:
        agent_attrs["output.value"] = str(child_spans[-1]["attributes"].get("output.value", ""))
    has_error = any(row["status"]["code"] == "STATUS_CODE_ERROR" for row in child_spans)
    root = span(
        trace_id=trace_id,
        span_id=agent_span_id,
        parent_span_id="",
        name=f"agent.{agent_role}",
        kind="SPAN_KIND_INTERNAL",
        start_time=halo_time(rows[0].get("timestamp")),
        end_time=halo_time(rows[-1].get("timestamp")),
        status_code="STATUS_CODE_ERROR" if has_error else "STATUS_CODE_OK",
        status_message="one or more child spans failed" if has_error else "",
        attrs=agent_attrs,
    )
    return [root, *child_spans]


def read_jsonl(path: Path, skip_bad_lines: bool) -> tuple[list[Json], int]:
    rows: list[Json] = []
    skipped = 0
    with path.open("r", encoding="utf-8-sig") as src:
        for line_no, line in enumerate(src, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise TypeError("expected JSON object")
                rows.append(row)
            except Exception as exc:  # noqa: BLE001
                if not skip_bad_lines:
                    raise
                skipped += 1
                print(f"[skip] line {line_no}: {exc}", file=sys.stderr)
    return rows, skipped


def convert_file(
    input_path: Path,
    output_path: Path,
    *,
    project_id: str,
    trace_id: str | None,
    skip_bad_lines: bool,
) -> tuple[int, int]:
    rows, skipped = read_jsonl(input_path, skip_bad_lines)
    spans = convert_events(rows, project_id, trace_id or str(uuid.uuid4()))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as dst:
        for index, row in enumerate(spans, 1):
            validate_span(row, index)
            dst.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return len(spans), skipped


def iter_jsonl_files(input_dir: Path, output_dir: Path) -> list[Path]:
    output_dir = output_dir.resolve()
    files: list[Path] = []
    for path in sorted(input_dir.rglob("*.jsonl")):
        resolved = path.resolve()
        try:
            resolved.relative_to(output_dir)
        except ValueError:
            files.append(path)
    return files


def output_path_for(input_path: Path, input_root: Path, output_root: Path) -> Path:
    return output_root / input_path.relative_to(input_root)


def default_output_path(input_path: Path) -> Path:
    if input_path.is_file():
        return input_path.with_name(f"{input_path.stem}.halo{input_path.suffix}")
    return input_path.with_name(f"{input_path.name}-halo-traces")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input event-stream JSONL file or directory")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=(
            "Output traces.jsonl file or output directory. Defaults to "
            "<input>.halo.jsonl for files, or <input-dir>-halo-traces for directories."
        ),
    )
    parser.add_argument("--project-id", default="converted trace")
    parser.add_argument(
        "--trace-id",
        default=None,
        help="Force all rows into this trace_id. Only allowed for single-file input.",
    )
    parser.add_argument("--skip-bad-lines", action="store_true")
    args = parser.parse_args()

    input_path = args.input
    output_path = args.output or default_output_path(input_path)
    if input_path.is_file():
        final_output = output_path / input_path.name if output_path.exists() and output_path.is_dir() else output_path
        converted, skipped = convert_file(
            input_path,
            final_output,
            project_id=args.project_id,
            trace_id=args.trace_id,
            skip_bad_lines=args.skip_bad_lines,
        )
        print(f"files=1 converted={converted} skipped={skipped} output={final_output}")
        return 0

    if not input_path.is_dir():
        raise FileNotFoundError(f"input path not found: {input_path}")
    if args.trace_id:
        raise ValueError("--trace-id can only be used with a single input file")

    files = iter_jsonl_files(input_path, output_path)
    if not files:
        print(f"files=0 converted=0 skipped=0 output_dir={output_path}")
        return 0

    total_converted = 0
    total_skipped = 0
    for src in files:
        dst = output_path_for(src, input_path, output_path)
        converted, skipped = convert_file(
            src,
            dst,
            project_id=args.project_id,
            trace_id=None,
            skip_bad_lines=args.skip_bad_lines,
        )
        total_converted += converted
        total_skipped += skipped
        print(f"[ok] {src} -> {dst} spans={converted} skipped={skipped}")

    print(
        f"files={len(files)} converted={total_converted} "
        f"skipped={total_skipped} output_dir={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
