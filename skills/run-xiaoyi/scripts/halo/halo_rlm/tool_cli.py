"""One-shot tool CLI for agent-driven mode (no LLM API required).

The host agent (Kimi, Claude Code, any code agent with a shell tool) acts as
the root RLM itself and invokes individual trace tools as subprocesses:

    python3 -m halo_rlm.tool_cli TRACES.jsonl --list
    python3 -m halo_rlm.tool_cli TRACES.jsonl get_dataset_overview
    python3 -m halo_rlm.tool_cli TRACES.jsonl view_trace --trace-id t-1
    python3 -m halo_rlm.tool_cli TRACES.jsonl render_trace --trace-id t-1 --budget 8000

Results are printed to stdout as JSON (render_trace prints plain text).
Usage errors print an {"error": ...} JSON object and exit 2; tool-level
errors return {"error": ...} with exit 0, mirroring the engine's contract
so the calling agent can parse the payload the same way in both modes.

Not available here (the host agent replaces them): call_subagent (spawn
subagents via the host's own mechanism), get_context_item (the host owns
its context), synthesize_traces (the host IS the synthesis model — use
render_trace to pull bounded trace text, then summarize it yourself).
run_code is also omitted because this package does not provide HALOAgent's
Deno/Pyodide security sandbox.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .tools import ToolRegistry
from .trace_store import TraceStore

# Tools exposed in agent-driven mode. synthesize_traces / get_context_item /
# call_subagent are deliberately excluded: the host agent fulfills those roles.
AGENT_DRIVEN_TOOLS = [
    "get_dataset_overview",
    "query_traces",
    "count_traces",
    "view_trace",
    "view_spans",
    "search_trace",
    "search_span",
    "render_trace",  # extra: bounded plain-text rendering for host-side synthesis
]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="halo-rlm-tool",
        description="Execute one HALO trace tool and print its result (agent-driven mode).",
    )
    p.add_argument("trace_path", help="Path to the OTLP-shaped JSONL trace file")
    p.add_argument("tool", nargs="?", help=f"Tool name, one of: {', '.join(AGENT_DRIVEN_TOOLS)}")
    p.add_argument(
        "--args",
        default=None,
        help="Advanced: all tool arguments as one JSON object. Do not combine with named flags.",
    )
    p.add_argument("--trace-id", default=None, help="Trace id for trace-specific tools")
    p.add_argument(
        "--span-id",
        action="append",
        default=None,
        help="Span id; repeat for view_spans",
    )
    p.add_argument("--regex-pattern", default=None, help="Regex for search_trace/search_span")
    p.add_argument("--budget", type=int, default=None, help="Byte budget for render_trace")
    p.add_argument("--max-matches", type=int, default=None, help="Maximum search matches")
    p.add_argument(
        "--context-buffer-chars",
        type=int,
        default=None,
        help="Search context characters around each match",
    )
    p.add_argument("--list", action="store_true", help="List available tools and their JSON schemas")
    return p


def _load_args(raw: str) -> dict[str, Any]:
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as f:
            raw = f.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--args must be a JSON object")
    return value


def _named_tool_args(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "trace_id": args.trace_id,
        "regex_pattern": args.regex_pattern,
        "budget": args.budget,
        "max_matches": args.max_matches,
        "context_buffer_chars": args.context_buffer_chars,
    }
    result = {key: value for key, value in values.items() if value is not None}
    if args.span_id:
        if args.tool == "view_spans":
            result["span_ids"] = args.span_id
        elif len(args.span_id) == 1:
            result["span_id"] = args.span_id[0]
        else:
            raise ValueError("repeat --span-id only with view_spans")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    store = TraceStore(args.trace_path)
    registry = ToolRegistry(
        store=store,
        llm_client=None,
        synthesis_model="",
        context=None,
        depth=0,
        maximum_depth=0,  # call_subagent structurally unavailable here
    )

    if args.list or not args.tool:
        schemas = [
            s for s in registry.schemas() if s.get("function", {}).get("name") in AGENT_DRIVEN_TOOLS
        ]
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "render_trace",
                    "description": "Render a trace as bounded plain text (agent-driven mode extra). "
                    "Use it to pull trace content for host-side synthesis.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trace_id": {"type": "string"},
                            "budget": {"type": "integer", "default": 8000},
                        },
                        "required": ["trace_id"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        print(json.dumps({"tools": schemas}, ensure_ascii=False, indent=2))
        return 0 if args.list else 2

    if args.tool not in AGENT_DRIVEN_TOOLS:
        print(
            json.dumps(
                {
                    "error": f"unknown or unavailable tool in agent-driven mode: {args.tool}",
                    "available": AGENT_DRIVEN_TOOLS,
                },
                ensure_ascii=False,
            )
        )
        return 2

    try:
        named_args = _named_tool_args(args)
        if args.args is not None and named_args:
            raise ValueError("do not combine --args with named tool flags")
        tool_args = _load_args(args.args) if args.args is not None else named_args
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"invalid tool arguments: {e}"}, ensure_ascii=False))
        return 2

    if args.tool == "render_trace":
        print(store.render_trace(tool_args["trace_id"], budget=int(tool_args.get("budget", 8000))))
        return 0

    print(registry.execute(args.tool, tool_args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
