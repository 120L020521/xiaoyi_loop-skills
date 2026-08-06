"""Command-line interface for the standalone Better Harness Judge."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from standalone_judge.batch import judge_batch, prepare_batch
from standalone_judge.config import (
    list_profiles,
    load_env_file,
    resolve_profile,
)
from standalone_judge.generate_judge_prompt import _LOG_FORMAT_CHOICES


def _package_dir() -> Path:
    """Return the installed or copied package directory."""
    return Path(__file__).resolve().parent


def _build_parser() -> argparse.ArgumentParser:
    """Create the top-level command parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Prepare external Runner logs once, then score them with one "
            "selectable Judge model."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        aliases=["prepare-batch"],
        help="Convert external logs and outputs into Judge task directories.",
    )
    prepare_source = prepare.add_mutually_exclusive_group(required=True)
    prepare_source.add_argument(
        "--cases",
        type=Path,
        help="JSONL case index with explicit log/output mappings.",
    )
    prepare_source.add_argument(
        "--logs-dir",
        type=Path,
        help=(
            "Auto-discover task<ID> directories containing one JSONL log "
            "and an optional outputs/ directory."
        ),
    )
    prepare.add_argument(
        "--task-root",
        type=Path,
        help=(
            "Root containing <task_id>/metadata.json. Required with "
            "--logs-dir."
        ),
    )
    prepare.add_argument(
        "--run-dir",
        type=Path,
        help="Run root; prepared data defaults to <run-dir>/prepared.",
    )
    prepare.add_argument(
        "--out-dir",
        type=Path,
        help="Explicit prepared-data directory.",
    )
    prepare.add_argument(
        "--log-format",
        choices=_LOG_FORMAT_CHOICES,
        default="auto",
    )
    prepare.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="Prepare only this task ID. Repeat to select several tasks.",
    )
    prepare.add_argument("--overwrite", action="store_true")

    judge = subparsers.add_parser(
        "judge",
        aliases=["judge-batch"],
        help="Score prepared tasks with one Judge profile.",
    )
    judge.add_argument(
        "--run-dir",
        type=Path,
        help=(
            "Run root containing prepared/. Defaults to JUDGE_RUN_DIR in "
            ".env."
        ),
    )
    judge.add_argument(
        "--prepared-dir",
        type=Path,
        help="Explicit prepared-data directory.",
    )
    judge.add_argument(
        "--results-dir",
        type=Path,
        help=(
            "Explicit result directory. Defaults to "
            "<run-dir>/results/<profile>."
        ),
    )
    judge.add_argument(
        "--profile",
        help="Profile name. Defaults to JUDGE_PROFILE in .env.",
    )
    judge.add_argument(
        "--profiles-file",
        type=Path,
        default=_package_dir() / "judge_profiles.toml",
    )
    judge.add_argument(
        "--env-file",
        type=Path,
        default=_package_dir() / ".env",
    )
    judge.add_argument(
        "--trace-mode",
        choices=("compact", "full"),
        help=(
            "compact uses the embedded compressed trace; full uses "
            "normalized_runner_log.jsonl. Defaults to JUDGE_TRACE_MODE."
        ),
    )
    judge.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="Judge only this task ID. Repeat to select several tasks.",
    )
    result_mode = judge.add_mutually_exclusive_group()
    result_mode.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip successful results only when Judge settings and the "
            "prepared-input fingerprint still match."
        ),
    )
    result_mode.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-evaluate all selected prepared tasks.",
    )

    profiles = subparsers.add_parser(
        "profiles",
        help="List model profiles and whether their API key is configured.",
    )
    profiles.add_argument(
        "--profiles-file",
        type=Path,
        default=_package_dir() / "judge_profiles.toml",
    )
    profiles.add_argument(
        "--env-file",
        type=Path,
        default=_package_dir() / ".env",
    )
    return parser


def _default_run_dir(argument: Path | None) -> Path:
    """Resolve the run root from CLI, `.env`, or the package default."""
    if argument is not None:
        return argument.expanduser().resolve()
    configured = os.environ.get("JUDGE_RUN_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    configured_home = os.environ.get("XIAOYI_LOOP_HOME", "").strip()
    if configured_home:
        return (Path(configured_home).expanduser() / "xiaoyi_judge").resolve()
    return (_package_dir().parents[1] / "xiaoyi_judge").resolve()


def _print_summary(report: dict[str, object]) -> None:
    """Print only a compact command result."""
    print(
        json.dumps(
            report.get("summary", report),
            ensure_ascii=False,
            indent=2,
        )
    )


def _failed_count(report: dict[str, object]) -> int:
    """Return the failure count from either batch report type."""
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return 1
    value = summary.get("failed", 0)
    return int(value) if isinstance(value, int) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone Judge CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    args = _build_parser().parse_args(argv)
    try:
        load_env_file(
            getattr(args, "env_file", _package_dir() / ".env")
        )
        if args.command in {"prepare", "prepare-batch"}:
            run_dir = _default_run_dir(args.run_dir)
            prepared_dir = (
                args.out_dir.expanduser().resolve()
                if args.out_dir is not None
                else run_dir / "prepared"
            )
            report = prepare_batch(
                cases_path=args.cases,
                logs_dir=args.logs_dir,
                task_root=args.task_root,
                prepared_dir=prepared_dir,
                log_format=args.log_format,
                overwrite=args.overwrite,
                task_ids=args.task_ids,
            )
            _print_summary(report)
            return 1 if _failed_count(report) else 0

        if args.command == "profiles":
            rows = list_profiles(profiles_path=args.profiles_file)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0

        run_dir = _default_run_dir(args.run_dir)
        profile_name = (
            args.profile or os.environ.get("JUDGE_PROFILE", "")
        ).strip()
        if not profile_name:
            raise ValueError(
                "No Judge profile selected. Set JUDGE_PROFILE in .env or "
                "pass --profile."
            )
        trace_mode = (
            args.trace_mode
            or os.environ.get("JUDGE_TRACE_MODE", "compact")
        ).strip().casefold()
        if trace_mode not in {"compact", "full"}:
            raise ValueError(
                "JUDGE_TRACE_MODE must be either 'compact' or 'full'"
            )
        profile = resolve_profile(
            name=profile_name,
            profiles_path=args.profiles_file,
        )
        prepared_dir = (
            args.prepared_dir.expanduser().resolve()
            if args.prepared_dir is not None
            else run_dir / "prepared"
        )
        results_dir = (
            args.results_dir.expanduser().resolve()
            if args.results_dir is not None
            else run_dir / "results" / profile.name
        )
        report = judge_batch(
            prepared_dir=prepared_dir,
            results_dir=results_dir,
            profile=profile,
            trace_mode=trace_mode,
            resume=args.resume,
            overwrite=args.overwrite,
            task_ids=args.task_ids,
        )
        _print_summary(report)
        return 1 if _failed_count(report) else 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
