#!/usr/bin/env python3
"""Prepare XiaoYi logs for Agent Judge without calling a model API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from standalone_judge.batch import prepare_batch
from xiaoyi_loop.runtime_paths import resolve_runtime_paths
from xiaoyi_loop.settings import ConfigError, load_local_settings
from xiaoyi_loop.task_locator import TaskLocationError, resolve_task_specs
from xiaoyi_loop.task_validation import TaskPreflightError, validate_task_specs
from xiaoyi_loop.workspace_runtime import resolve_workspace_config


_SCRIPT_ROOT = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_ROOT.parent
def _runtime_root() -> Path:
    configured = os.environ.get("XIAOYI_LOOP_HOME", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else _SKILL_ROOT
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="标准化小艺日志并生成 Agent Judge 输入；不会调用模型 API。"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="本机配置文件；默认发现 <workspace>/.xiaoyi-loop/local.toml。",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Agent 当前工作目录；默认使用启动命令的目录。",
    )
    parser.add_argument(
        "--task-dir",
        type=Path,
        action="append",
        default=[],
        help="用户明确提供的 Task 目录或 Task 根目录；可重复。",
    )
    parser.add_argument("--logs-dir", type=Path, help="覆盖配置中的日志目录。")
    parser.add_argument("--tasks-root", type=Path, help="覆盖配置中的 Task 根目录。")
    parser.add_argument("--run-dir", type=Path, help="覆盖配置中的 Judge 运行目录。")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--task-id",
        action="append",
        dest="task_ids",
        help="只 prepare 此 Task ID；可重复。",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="prepare 当前 logs_dir 下全部包含标准 Trace 的任务。",
    )
    parser.add_argument(
        "--log-format",
        choices=("auto", "generic", "halo", "xiaoyi", "event-stream"),
        default="auto",
    )
    return parser


def _is_judgeable_log_dir(path: Path, task_id: str) -> bool:
    """Return whether a canonical task directory contains a usable pulled log."""
    return path.is_dir() and (path / f"task{task_id}.jsonl").is_file()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime_root = _runtime_root()
    workspace = args.workspace.expanduser().resolve()
    config_path = resolve_workspace_config(workspace, explicit=args.config)
    try:
        settings = load_local_settings(
            project_root=runtime_root,
            config_path=config_path,
            discover_default_config=False,
        )
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    runtime_paths = resolve_runtime_paths(
        settings,
        workspace=workspace,
        logs_dir=args.logs_dir,
        run_dir=args.run_dir,
    )
    logs_dir = runtime_paths.logs_dir
    configured_root = (
        args.tasks_root.expanduser()
        if args.tasks_root is not None
        else settings.tasks_root
    )
    if configured_root is not None and not configured_root.is_absolute():
        configured_root = (workspace / configured_root).resolve()
    elif configured_root is not None:
        configured_root = configured_root.resolve()
    run_dir = runtime_paths.run_dir
    explicit_locations = tuple(
        path.expanduser().resolve()
        if path.is_absolute()
        else (workspace / path).resolve()
        for path in args.task_dir
    )
    try:
        located = resolve_task_specs(
            args.task_ids or (),
            workspace=workspace,
            explicit_locations=explicit_locations,
            configured_root=configured_root,
            allow_many_without_selectors=args.all,
        )
    except (TaskLocationError, ValueError) as exc:
        print(f"Task 定位失败：{exc}", file=sys.stderr)
        return 2

    specs = list(located.specs)
    if args.all:
        log_ids = {
            match.group(1)
            for path in logs_dir.iterdir()
            if path.is_dir()
            and (match := re.fullmatch(r"task[_-]?(\d+)", path.name, re.IGNORECASE))
            and _is_judgeable_log_dir(path, match.group(1))
        } if logs_dir.is_dir() else set()
        specs_by_id = {str(spec.task_id): spec for spec in specs}
        missing_metadata = sorted(log_ids - specs_by_id.keys(), key=int)
        if missing_metadata:
            print(
                "Task 定位失败：以下标准 Trace 找不到对应 metadata.json："
                + ", ".join(missing_metadata),
                file=sys.stderr,
            )
            return 2
        specs = [
            specs_by_id[task_id]
            for task_id in sorted(log_ids, key=int)
            if task_id in specs_by_id
        ]
        if not specs:
            print("prepare 失败：logs_dir 下没有标准 task<ID>/task<ID>.jsonl Trace。", file=sys.stderr)
            return 1

    try:
        preflight = validate_task_specs(specs)
    except TaskPreflightError as exc:
        print(f"Task 预检失败：\n{exc}", file=sys.stderr)
        return 2
    for result in preflight:
        for warning in result.warnings:
            print(f"[preflight] task{result.task_id} 警告：{warning}", file=sys.stderr)

    try:
        report = prepare_batch(
            logs_dir=logs_dir,
            prepared_dir=run_dir,
            log_format=args.log_format,
            overwrite=True,
            task_ids=[str(spec.task_id) for spec in specs],
            metadata_paths=[spec.metadata_path for spec in specs],
            preserve_task_files=("judge_result.json",),
        )
    except (OSError, ValueError) as exc:
        print(f"prepare 失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(report.get("summary", report), ensure_ascii=False, indent=2))
    summary = report.get("summary")
    failed = summary.get("failed", 1) if isinstance(summary, dict) else 1
    return 1 if isinstance(failed, int) and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
