#!/usr/bin/env python3
"""Run XiaoYi tasks only; Agent Judge is orchestrated by the Skill."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pipeline import main as pipeline_main
from xiaoyi_loop.runtime_paths import RuntimePaths, resolve_runtime_paths
from xiaoyi_loop.settings import ConfigError, load_local_settings
from xiaoyi_loop.task_locator import TaskLocationError, resolve_task_specs
from xiaoyi_loop.task_validation import TaskPreflightError, validate_task_specs
from xiaoyi_loop.workspace_runtime import resolve_workspace_config


_SCRIPT_ROOT = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_ROOT.parent


def _runtime_root() -> Path:
    configured = os.environ.get("XIAOYI_LOOP_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else _SKILL_ROOT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="批量推送 Task 给小艺并拉取日志；不会启动 Judge API。"
    )
    parser.add_argument(
        "tasks",
        nargs="*",
        help="可选的 Task ID、范围、任务目录或 metadata.json。",
    )
    parser.add_argument("--config", type=Path, help="本机配置文件。")
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
        help=(
            "用户明确提供的精确 Task 目录或数据集根目录；可重复。"
            "数据集根目录必须配合 Task ID selector。"
        ),
    )
    parser.add_argument("--tasks-root", help="Task metadata 根目录。")
    parser.add_argument("--hdc", help="hdc 命令或可执行文件路径。")
    parser.add_argument("--target", help="HDC connect key。")
    parser.add_argument("--user-id", help="小艺日志 user_id。")
    parser.add_argument("--date", dest="date_id", help="日志日期 ID。")
    parser.add_argument("--dynamic-date", action="store_true")
    parser.add_argument("--logs-dir", help="日志与 output 目录。")
    parser.add_argument("--state-file", help="Runner 状态文件。")
    parser.add_argument("--poll", type=float)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--settle", type=float)
    parser.add_argument("--restart-delay", type=float)
    parser.add_argument("--tail-lines", type=int)
    force_group = parser.add_mutually_exclusive_group()
    force_group.add_argument("--force-stop", action="store_true")
    force_group.add_argument("--no-force-stop", action="store_true")
    error_group = parser.add_mutually_exclusive_group()
    error_group.add_argument("--stop-on-error", action="store_true")
    error_group.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _append_value(arguments: list[str], flag: str, value: object | None) -> None:
    if value is not None:
        arguments.extend((flag, str(value)))


def _pipeline_arguments(
    args: argparse.Namespace,
    metadata_paths: Sequence[Path],
    *,
    runtime_paths: RuntimePaths,
) -> list[str]:
    arguments = [str(path) for path in metadata_paths]
    _append_value(arguments, "--config", args.config)
    arguments.append("--no-default-config")
    _append_value(arguments, "--hdc", args.hdc)
    _append_value(arguments, "--target", args.target)
    _append_value(arguments, "--user-id", args.user_id)
    _append_value(arguments, "--date", args.date_id)
    _append_value(arguments, "--logs-dir", runtime_paths.logs_dir)
    _append_value(arguments, "--state-file", runtime_paths.state_file)
    _append_value(arguments, "--poll", args.poll)
    _append_value(arguments, "--timeout", args.timeout)
    _append_value(arguments, "--settle", args.settle)
    _append_value(arguments, "--restart-delay", args.restart_delay)
    _append_value(arguments, "--tail-lines", args.tail_lines)
    for enabled, flag in (
        (args.dynamic_date, "--dynamic-date"),
        (args.force_stop, "--force-stop"),
        (args.no_force_stop, "--no-force-stop"),
        (args.stop_on_error, "--stop-on-error"),
        (args.continue_on_error, "--continue-on-error"),
        (args.verbose, "--verbose"),
    ):
        if enabled:
            arguments.append(flag)
    arguments.append("--skip-judge")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    args = _parser().parse_args(arguments)
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
    try:
        explicit_locations = tuple(
            path.expanduser().resolve()
            if path.is_absolute()
            else (workspace / path).resolve()
            for path in args.task_dir
        )
        configured_root = (
            Path(args.tasks_root).expanduser()
            if args.tasks_root is not None
            else settings.tasks_root
        )
        if configured_root is not None and not configured_root.is_absolute():
            configured_root = (workspace / configured_root).resolve()
        elif configured_root is not None:
            configured_root = configured_root.resolve()
        located = resolve_task_specs(
            args.tasks,
            workspace=workspace,
            explicit_locations=explicit_locations,
            configured_root=configured_root,
        )
    except (TaskLocationError, ValueError) as exc:
        print(f"Task 定位失败：{exc}", file=sys.stderr)
        return 2

    try:
        preflight = validate_task_specs(located.specs)
    except TaskPreflightError as exc:
        print(f"Task 预检失败：\n{exc}", file=sys.stderr)
        return 2

    print(f"[runner] Task 来源：{located.source}")
    for spec in located.specs:
        print(f"[runner] task{spec.task_id} metadata：{spec.metadata_path}")
    for result in preflight:
        for warning in result.warnings:
            print(f"[preflight] task{result.task_id} 警告：{warning}", file=sys.stderr)
    runtime_paths = resolve_runtime_paths(
        settings,
        workspace=workspace,
        logs_dir=args.logs_dir,
        state_file=args.state_file,
    )
    print(f"[runner] 工作目录：{workspace}")
    print(f"[runner] 本机配置：{config_path or '未创建（使用默认值）'}")
    print(f"[runner] 日志与 output：{runtime_paths.logs_dir}")
    print(f"[runner] 状态文件：{runtime_paths.state_file}")
    args.config = config_path
    return pipeline_main(
        _pipeline_arguments(
            args,
            [spec.metadata_path for spec in located.specs],
            runtime_paths=runtime_paths,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
