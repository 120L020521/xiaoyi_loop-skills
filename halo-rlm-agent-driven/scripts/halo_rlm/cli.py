"""Command line entry point: ``python -m halo_rlm.cli``.

Usage:
    python -m halo_rlm.cli TRACES.jsonl [-p "Diagnose errors you find and suggest fixes"] \
      [--model gpt-4o-mini] [--synthesis-model X] [--compaction-model X] \
      [--max-depth 2] [--max-turns 20] [--max-parallel 4] \
      [--base-url ...] [--api-key ...] [--dataset-context "..."] \
      [--judge-result judge_result.json] [--task-json task.json] \
      [--trace-summary trace_summary.json] \
      [--surface FILE] [--artifacts-dir DIR] \
      [--mock-demo] [-o report.md]

The final JSON report goes to stdout (or -o FILE); progress logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

from .better_harness import (
    BETTER_HARNESS_COMPONENTS,
    DEFAULT_EDITABLE_SURFACES,
    build_halo_prompt,
)
from .engine import EngineConfig, run_engine, scripted_mock_for_demo
from .llm_client import LLMError
from .report_contract import normalize_json_report
from .trace_store import TraceStore


def _load_json_file(path: Optional[str], label: str) -> Optional[Any]:
    if not path:
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{label} is not valid JSON: {path}: {e}") from e


def _compact_json(value: Any, limit: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated by halo-rlm CLI: original {len(text)} chars]"


def _build_dataset_context(
    *,
    user_context: Optional[str],
    task_json: Optional[Any],
    judge_result: Optional[Any],
    trace_summary: Optional[Any],
) -> Optional[str]:
    sections: list[str] = []
    if user_context:
        sections.append(user_context)
    if task_json is not None:
        sections.append("task.json:\n" + _compact_json(task_json))
    if judge_result is not None:
        sections.append(
            "judge_result.json:\n"
            + _compact_json(judge_result)
            + "\n\nTreat this as outcome context, not as trace data."
        )
    if trace_summary is not None:
        sections.append(
            "trace_summary.json:\n"
            + _compact_json(trace_summary)
            + "\n\nThis is only a summary. Verify important claims against traces.jsonl spans."
        )
    return "\n\n---\n\n".join(sections) if sections else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo-rlm",
        description=(
            "HALO RLM engine: recursively analyze an OTel-shaped JSONL trace "
            "dataset with an LLM agent (root + subagents)."
        ),
    )
    parser.add_argument("traces", help="Path to the OTel JSONL trace file.")
    parser.add_argument(
        "-p",
        "--prompt",
        default=None,
        help=(
            "Optional additional diagnostic request. A complete mode-specific "
            "prompt is generated automatically."
        ),
    )
    parser.add_argument("--model", default="gpt-4o-mini", help="Main model (root + subagents).")
    parser.add_argument("--synthesis-model", default=None, help="Model for synthesize_traces (default: --model).")
    parser.add_argument("--compaction-model", default=None, help="Model for context compaction (default: --model).")
    parser.add_argument("--max-depth", type=int, default=2, help="maximum_depth (default: 2).")
    parser.add_argument("--max-turns", type=int, default=20, help="maximum_turns per agent (default: 20).")
    parser.add_argument("--max-parallel", type=int, default=4, help="maximum parallel subagents per depth (default: 4).")
    parser.add_argument("--keep-last-messages", type=int, default=12)
    parser.add_argument("--keep-last-turns", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL (default: env OPENAI_BASE_URL).")
    parser.add_argument("--api-key", default=None, help="API key (default: env OPENAI_API_KEY).")
    parser.add_argument("--dataset-context", default=None, help="Caller-supplied description of what the dataset encodes.")
    parser.add_argument("--task-json", default=None, help="Optional task.json context for harness diagnostics.")
    parser.add_argument("--judge-result", default=None, help="Optional judge_result.json context. Used as failure target, not trace data.")
    parser.add_argument("--trace-summary", default=None, help="Optional trace_summary.json context. Summary only; traces.jsonl remains authoritative.")
    parser.add_argument(
        "--surface",
        action="append",
        default=None,
        help="Editable harness surface filename. May be repeated.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help=(
            "Write halo_prompt.txt and halo_report.json to this directory, "
            "using the unified Better Harness artifact convention."
        ),
    )
    parser.add_argument(
        "--mock-demo",
        action="store_true",
        help="Run with a scripted mock LLM (no API key needed) to demo the recursive loop.",
    )
    parser.add_argument(
        "--enable-unsafe-run-code",
        action="store_true",
        help=(
            "Expose host Python subprocess execution. Disabled by default "
            "because it is not HALOAgent's Deno/Pyodide security sandbox."
        ),
    )
    parser.add_argument("-o", "--output", default=None, help="Write the final report to this file instead of stdout.")
    return parser


def _make_event_logger(verbose: bool = True):
    def log(event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "engine_start":
            print(f"[halo] engine start: {event.get('trace_path')}", file=sys.stderr)
        elif etype == "agent_start":
            print(
                f"[halo] agent start: {event.get('agent_id')} "
                f"(depth={event.get('depth')}, parent={event.get('parent_agent_id')})",
                file=sys.stderr,
            )
        elif etype == "turn":
            print(
                f"[halo] {event.get('agent_id')} turn {event.get('turn')}",
                file=sys.stderr,
            )
        elif etype == "tool_call":
            print(
                f"[halo] {event.get('agent_id')} tool_call: {event.get('tool')} "
                f"{event.get('arguments_preview', '')}",
                file=sys.stderr,
            )
        elif etype == "tool_result":
            print(
                f"[halo] {event.get('agent_id')} tool_result: {event.get('tool')} "
                f"({event.get('result_bytes')} bytes)",
                file=sys.stderr,
            )
        elif etype == "agent_end":
            print(
                f"[halo] agent end: {event.get('agent_id')} "
                f"(turns={event.get('turns_used')}, tool_calls={event.get('tool_calls_made')})",
                file=sys.stderr,
            )
        elif etype == "max_turns_reached":
            print(
                f"[halo] WARNING: {event.get('agent_id')} reached maximum_turns",
                file=sys.stderr,
            )
        elif etype == "engine_end":
            print(f"[halo] engine end: report_chars={event.get('report_chars')}", file=sys.stderr)
        else:
            print(f"[halo] {json.dumps(event, ensure_ascii=False)[:300]}", file=sys.stderr)

    return log


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    mock_script = None
    if args.mock_demo:
        # Peek at real trace ids so the scripted demo references real data.
        try:
            store = TraceStore(args.traces)
            sample_ids = store.trace_ids[:2]
        except Exception as e:  # noqa: BLE001
            print(f"[halo] error: cannot read trace file: {e}", file=sys.stderr)
            return 2
        mock_script = scripted_mock_for_demo(sample_ids)
        print("[halo] mock-demo mode: using scripted mock LLM (no API key needed)", file=sys.stderr)

    try:
        task_json = _load_json_file(args.task_json, "task-json")
        judge_result = _load_json_file(args.judge_result, "judge-result")
        trace_summary = _load_json_file(args.trace_summary, "trace-summary")
        if task_json is not None and not isinstance(task_json, dict):
            raise ValueError("--task-json must contain a JSON object")
        if judge_result is not None and not isinstance(judge_result, dict):
            raise ValueError("--judge-result must contain a JSON object")
        surfaces = args.surface or list(DEFAULT_EDITABLE_SURFACES)
        prompt = build_halo_prompt(
            task=task_json or {},
            judge_result=judge_result or {},
            surface_filenames=surfaces,
            additional_request=args.prompt,
        )
        dataset_context = _build_dataset_context(
            user_context=args.dataset_context,
            task_json=None,
            judge_result=None,
            trace_summary=trace_summary,
        )
    except (OSError, ValueError) as e:
        print(f"[halo] error: {e}", file=sys.stderr)
        return 2

    prompt_dir = args.artifacts_dir
    if prompt_dir is None and args.output:
        prompt_dir = os.path.dirname(os.path.abspath(args.output))
    if prompt_dir:
        os.makedirs(prompt_dir, exist_ok=True)
        prompt_path = os.path.join(prompt_dir, "halo_prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
            if not prompt.endswith("\n"):
                f.write("\n")
        print(f"[halo] prompt written to {prompt_path}", file=sys.stderr)

    config = EngineConfig(
        model=args.model,
        synthesis_model=args.synthesis_model,
        compaction_model=args.compaction_model,
        maximum_depth=args.max_depth,
        maximum_turns=args.max_turns,
        maximum_parallel_subagents=args.max_parallel,
        keep_last_messages=args.keep_last_messages,
        keep_last_turns=args.keep_last_turns,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        dataset_context=dataset_context,
        enable_unsafe_run_code=args.enable_unsafe_run_code,
        api_key=args.api_key,
        base_url=args.base_url,
        mock_script=mock_script,
    )

    try:
        report = run_engine(
            args.traces,
            prompt,
            config=config,
            on_event=_make_event_logger(),
        )
        report = normalize_json_report(
            report,
            allowed_components=BETTER_HARNESS_COMPONENTS,
            allowed_targets=surfaces,
        )
    except FileNotFoundError:
        print(f"[halo] error: trace file not found: {args.traces}", file=sys.stderr)
        return 2
    except LLMError as e:
        print(f"[halo] LLM error: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[halo] error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
            if not report.endswith("\n"):
                f.write("\n")
        print(f"[halo] report written to {args.output}", file=sys.stderr)
    else:
        print(report)

    if args.artifacts_dir:
        report_path = os.path.join(args.artifacts_dir, "halo_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
            if not report.endswith("\n"):
                f.write("\n")
        print(f"[halo] report written to {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
