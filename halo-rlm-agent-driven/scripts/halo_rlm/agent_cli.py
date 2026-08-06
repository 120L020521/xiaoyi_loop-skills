"""Local helpers for the no-API-key, host-agent HALO workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .better_harness import (
    BETTER_HARNESS_COMPONENTS,
    DEFAULT_EDITABLE_SURFACES,
    build_halo_prompt,
)
from .report_contract import normalize_json_report


def _load_object(path: str | None, label: str) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _build_prompt(args: argparse.Namespace) -> dict[str, Any]:
    output = Path(args.output).resolve()
    prompt = build_halo_prompt(
        task=_load_object(args.task_json, "--task-json"),
        judge_result=_load_object(args.judge_result, "--judge-result"),
        surface_filenames=args.surface or list(DEFAULT_EDITABLE_SURFACES),
        additional_request=args.prompt,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(prompt.rstrip() + "\n", encoding="utf-8")
    return {"status": "ok", "prompt_path": str(output), "chars": len(prompt)}


def _validate_report(args: argparse.Namespace) -> dict[str, Any]:
    report_path = Path(args.report).resolve()
    surfaces = args.surface or list(DEFAULT_EDITABLE_SURFACES)
    normalized = normalize_json_report(
        report_path.read_text(encoding="utf-8"),
        allowed_components=BETTER_HARNESS_COMPONENTS,
        allowed_targets=surfaces,
    )
    report_path.write_text(normalized + "\n", encoding="utf-8")
    return {"status": "ok", "report_path": str(report_path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo-rlm-agent",
        description="Local prompt/report helpers for host-agent diagnosis; no LLM API is used.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt = subparsers.add_parser("build-prompt", help="Write halo_prompt.txt locally")
    prompt.add_argument("--output", required=True, help="Destination halo_prompt.txt")
    prompt.add_argument("--task-json", default=None, help="Optional task JSON object")
    prompt.add_argument("--judge-result", default=None, help="Optional Judge JSON object")
    prompt.add_argument("--surface", action="append", default=None)
    prompt.add_argument("-p", "--prompt", default=None, help="Additional diagnostic request")
    prompt.set_defaults(handler=_build_prompt)

    validate = subparsers.add_parser(
        "validate-report", help="Validate and normalize a host-agent halo_report.json"
    )
    validate.add_argument("report", help="Path to halo_report.json")
    validate.add_argument("--surface", action="append", default=None)
    validate.set_defaults(handler=_validate_report)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = args.handler(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
