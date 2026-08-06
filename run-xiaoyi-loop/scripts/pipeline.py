#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XiaoYi → Judge 串联流水线。

阶段一：调用 batch_runner 的下层函数，逐个向鸿蒙 PC 小艺推送 task、
        等待 stop_reason=stop、拉取轨迹日志与 output 到 xiaoyi_logs/task<ID>/。
        所有结果统一落盘到 task<ID>/；正常、超时或失败，只要收集到 Trace 就参与 judge。
阶段二：调用 standalone_judge.cli.main，prepare 把 xiaoyi_logs 转成 judge 输入，
        judge 用选定 profile 评分，结果写到 xiaoyi_judge/results/<profile>/。
流水线止于 judge_result.json，不生成 Excel。

示例：
  python -m xiaoyi_loop 117 127
  python -m xiaoyi_loop 1-388 --judge-profile glm52
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

# 真实实现随 Skill 分发。配置从 Skill 目录定位；标准入口会显式传入以 Agent 工作目录
# 解析的运行产物路径。项目根目录兼容入口通过 XIAOYI_LOOP_HOME 保留原有配置位置。
_SCRIPT_ROOT = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_ROOT.parent
_configured_home = os.environ.get("XIAOYI_LOOP_HOME", "").strip()
_PROJECT_ROOT = (
    Path(_configured_home).expanduser().resolve()
    if _configured_home
    else _SKILL_ROOT
)
if str(_SCRIPT_ROOT) in sys.path:
    sys.path.remove(str(_SCRIPT_ROOT))
sys.path.insert(0, str(_SCRIPT_ROOT))

# batch_runner 是顶层单文件模块，直接 import 其下层函数。
from batch_runner import (
    TaskTimeoutError,
    TaskSpec,
    collect_task_specs,
    force_stop,
    list_remote_logs,
    pull_log,
    snapshot,
    start_task,
    today_id,
    wait_for_task_done,
)
# standalone_judge.cli.main(argv) 接收 argv，干净调用。
from standalone_judge.cli import main as judge_cli_main
from standalone_judge.config import load_env_file
from xiaoyi_loop.doctor import run_doctor
from xiaoyi_loop.settings import (
    ConfigError,
    LocalSettings,
    apply_local_environment,
    default_config_path,
    load_local_settings,
)


def _load_root_env() -> None:
    """向后兼容旧版根目录 .env；新安装应使用 config/local.toml。"""
    env_path = _PROJECT_ROOT / ".env"
    if env_path.is_file():
        load_env_file(env_path, override=False)


def _build_parser(settings: LocalSettings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "XiaoYi → Judge 串联流水线：向鸿蒙 PC 小艺批量推送 task，"
            "拉取轨迹后用 standalone_judge 评分。"
        )
    )
    parser.add_argument(
        "tasks",
        nargs="*",
        help=(
            "runner 要执行的 metadata.json/任务目录，或配合 --tasks-root 的任务编号"
            "（1-10、1..10、1-10,13,20）；--skip-runner 时可省略。"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(settings.config_path or default_config_path(_PROJECT_ROOT)),
        help="本机配置文件，默认 config/local.toml；也可设置 XIAOYI_LOOP_CONFIG。",
    )
    parser.add_argument(
        "--no-default-config",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--doctor", action="store_true", help="只检查本机配置和依赖，不连接设备或调用模型。")
    parser.add_argument(
        "--tasks-root",
        default=str(settings.tasks_root) if settings.tasks_root else None,
        help="任务 metadata 根目录；通常写在 config/local.toml。",
    )
    parser.add_argument("--hdc", default=settings.hdc, help="hdc 命令名或可执行文件路径。")
    parser.add_argument("--target", default=settings.target, help="hdc 目标 connect key；留空使用默认目标。")
    parser.add_argument("--user-id", default=settings.user_id, help="taichu_data 下的 user_id；留空自动扫描。")
    parser.add_argument("--date", dest="date_id", help="日志日期 ID，例如 20260804；不填取当天。")
    parser.add_argument("--dynamic-date", action="store_true", help="轮询时动态刷新日期 ID。")
    parser.add_argument(
        "--logs-dir",
        default=str(settings.logs_dir),
        help="小艺轨迹与 output 输出目录，也是 judge prepare 的 --logs-dir。",
    )
    parser.add_argument(
        "--run-dir",
        default=str(settings.run_dir),
        help="judge run 根目录，默认由本机配置决定。",
    )
    parser.add_argument(
        "--state-file",
        default=str(settings.state_file),
        help="流水线状态文件，默认由本机配置决定。",
    )
    parser.add_argument("--poll", type=float, default=settings.poll_seconds, help="runner 轮询间隔秒。")
    parser.add_argument("--timeout", type=int, default=settings.timeout_seconds, help="runner 单任务等待秒。")
    parser.add_argument("--settle", type=float, default=settings.settle_seconds, help="runner 拉日志前等待秒。")
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=settings.restart_delay_seconds,
        help="runner 相邻任务间隔秒。",
    )
    parser.add_argument("--tail-lines", type=int, default=settings.tail_lines, help="runner 检查日志末尾事件数。")
    parser.add_argument("--min-task", type=int, default=1, help="最小任务编号，默认 1。")
    parser.add_argument("--max-task", type=int, default=388, help="最大任务编号，默认 388。")
    force_stop_group = parser.add_mutually_exclusive_group()
    force_stop_group.add_argument(
        "--force-stop",
        action="store_false",
        dest="no_force_stop",
        help="runner 任务完成后 force-stop（覆盖本机配置）。",
    )
    force_stop_group.add_argument(
        "--no-force-stop",
        action="store_true",
        dest="no_force_stop",
        help="runner 任务完成后不 force-stop。",
    )
    parser.set_defaults(no_force_stop=not settings.force_stop)
    error_group = parser.add_mutually_exclusive_group()
    error_group.add_argument(
        "--stop-on-error",
        action="store_true",
        dest="stop_on_error",
        help="runner 单任务失败即终止整个批次。",
    )
    error_group.add_argument(
        "--continue-on-error",
        action="store_false",
        dest="stop_on_error",
        help="runner 单任务失败后继续（覆盖本机配置）。",
    )
    parser.set_defaults(stop_on_error=settings.stop_on_error)
    parser.add_argument("--verbose", action="store_true", help="打印 hdc 命令与 judge 详细日志。")

    parser.add_argument("--judge-profile", default=settings.judge_profile, help="Judge profile 名称。")
    parser.add_argument(
        "--judge-trace-mode",
        default=settings.judge_trace_mode,
        choices=("compact", "full"),
        help="Judge trace 模式。",
    )
    parser.add_argument(
        "--profiles-file",
        default=str(settings.profiles_file),
        help="Judge profile TOML 文件。",
    )
    judge_result_group = parser.add_mutually_exclusive_group()
    judge_result_group.add_argument(
        "--judge-resume",
        action="store_true",
        dest="judge_skip_existing",
        help="跳过输入未变化且已有成功结果的 task。",
    )
    judge_result_group.add_argument(
        "--judge-overwrite",
        action="store_false",
        dest="judge_skip_existing",
        help="重新 Judge 选中范围内的 task，不复用已有结果。",
    )
    parser.set_defaults(judge_skip_existing=settings.judge_skip_existing)
    parser.add_argument(
        "--judge-task-id",
        action="append",
        dest="judge_task_ids",
        help=(
            "prepare 和 Judge 只处理该 Task ID；可重复指定。"
            "正常 runner 批次会自动选择本轮已收集标准 Trace 的任务；"
            "使用 --skip-runner 且未指定本参数时扫描全部标准 Trace。"
        ),
    )
    parser.add_argument("--skip-judge", action="store_true", help="只跑 runner 阶段，跳过 judge。")
    parser.add_argument("--skip-runner", action="store_true", help="只跑 judge 阶段，跳过 runner（用已有日志）。")
    return parser


def _resolve_run_dir(argument: str | None) -> Path:
    if argument is not None:
        return Path(argument).expanduser().resolve()
    configured = os.environ.get("JUDGE_RUN_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (_PROJECT_ROOT / "xiaoyi_judge").resolve()


def _clean_task_log_dir(logs_dir: Path, task_id: int) -> None:
    """覆盖语义：每个要跑的 task 先清空旧日志目录，避免脏数据进 judge。"""
    task_dir = logs_dir / f"task{task_id}"
    if task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)


def _record_state(
    state_path: Path,
    *,
    started: list[int],
    completed: list[int],
    timed_out: list[int],
    failed: list[int],
    stage: str,
    batch_started_at: str,
    current_task: int | None = None,
    current_task_started_at: str | None = None,
    current_task_deadline_at: str | None = None,
) -> None:
    state = {
        "stage": stage,
        "startedAt": batch_started_at,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "processId": os.getpid(),
        "runner": {
            "started": started,
            "completed": completed,
            "timedOut": timed_out,
            "failed": failed,
            "currentTask": current_task,
            "currentTaskStartedAt": current_task_started_at,
            "currentTaskDeadlineAt": current_task_deadline_at,
        },
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(state_path)


def run_runner_phase(args: argparse.Namespace, tasks: list[TaskSpec], logs_dir: Path) -> tuple[list[int], list[int]]:
    """逐任务推送小艺、等待并拉日志。返回 (可 Judge IDs, 失败 IDs)。"""
    state_path = Path(args.state_file).expanduser().resolve()
    task_ids = [task.task_id for task in tasks]
    batch_started_at = datetime.now().isoformat(timespec="seconds")
    date_id = args.date_id or today_id()
    completed: list[int] = []
    timed_out: list[int] = []
    failed: list[int] = []
    total = len(tasks)
    current_task: int | None = None
    current_task_started_at: str | None = None
    current_task_deadline_at: str | None = None
    def record(stage: str) -> None:
        _record_state(
            state_path,
            started=task_ids,
            completed=completed,
            timed_out=timed_out,
            failed=failed,
            stage=stage,
            batch_started_at=batch_started_at,
            current_task=current_task,
            current_task_started_at=current_task_started_at,
            current_task_deadline_at=current_task_deadline_at,
        )

    try:
        record("runner-starting")
        print(f"[runner] 任务队列：{task_ids}")
        print(f"[runner] 日志日期：{date_id}{'（动态刷新）' if args.dynamic_date else ''}")
        print(f"[runner] 日志输出：{logs_dir}")

        for index, task in enumerate(tasks, start=1):
            task_id = task.task_id
            current_task = task_id
            current_task_started_at = None
            current_task_deadline_at = None
            record("runner-preparing")
            print(f"\n[runner] [{index}/{total}] 启动 task{task_id}")
            _clean_task_log_dir(logs_dir, task_id)

            query_characters: int | None = None
            try:
                current_date_id = today_id() if args.dynamic_date else date_id
                before_logs = list_remote_logs(
                    target=args.target,
                    user_id=args.user_id,
                    date_id=current_date_id,
                    verbose=args.verbose,
                )
                before = snapshot(before_logs)

                started_time = datetime.now()
                current_task_started_at = started_time.isoformat(timespec="seconds")
                current_task_deadline_at = (
                    started_time + timedelta(seconds=args.timeout)
                ).isoformat(timespec="seconds")
                record("runner-pushing")
                query_characters = len(
                    start_task(task, target=args.target, verbose=args.verbose)
                )
                record("runner-waiting")
                done_log = wait_for_task_done(
                    task_id=task_id,
                    before=before,
                    target=args.target,
                    user_id=args.user_id,
                    initial_date_id=current_date_id,
                    dynamic_date=args.dynamic_date,
                    poll_seconds=args.poll,
                    timeout_seconds=args.timeout,
                    tail_lines=args.tail_lines,
                    verbose=args.verbose,
                )
                print(f"[runner] task{task_id} 完成，日志：{done_log.path}")

                record("runner-pulling")
                if args.settle > 0:
                    time.sleep(args.settle)
                pull_log(
                    done_log,
                    task=task,
                    query_characters=query_characters,
                    out_dir=logs_dir,
                    target=args.target,
                    verbose=args.verbose,
                )
                print(
                    f"[runner] task{task_id} 日志与 output 已落盘到 "
                    f"{logs_dir}/task{task_id}/"
                )

                if not args.no_force_stop:
                    record("runner-force-stopping")
                    force_stop(target=args.target, verbose=args.verbose)
                    print(f"[runner] task{task_id} 已 force-stop")
                completed.append(task_id)

                if index < total and args.restart_delay > 0:
                    current_task_started_at = None
                    current_task_deadline_at = None
                    record("runner-inter-task-delay")
                    print(
                        f"[runner] task{task_id} 等待 {args.restart_delay:g}s 后下一任务"
                    )
                    time.sleep(args.restart_delay)

            except TaskTimeoutError as exc:
                print(f"[runner] task{task_id} 达到超时上限：{exc}", file=sys.stderr)
                record("runner-pulling-timeout")
                collected = _handle_runner_timeout(
                    args,
                    task,
                    query_characters,
                    exc,
                    logs_dir,
                )
                if collected:
                    completed.append(task_id)
                    timed_out.append(task_id)
                else:
                    failed.append(task_id)
                if args.stop_on_error and not collected:
                    print("[runner] --stop-on-error 已触发，终止批次", file=sys.stderr)
                    break
                if index < total and args.restart_delay > 0:
                    current_task_started_at = None
                    current_task_deadline_at = None
                    record("runner-inter-task-delay")
                    print(
                        f"[runner] task{task_id} 超时处理后等待 "
                        f"{args.restart_delay:g}s 再继续"
                    )
                    time.sleep(args.restart_delay)

            except Exception as exc:
                print(f"[runner] task{task_id} 异常失败：{exc}", file=sys.stderr)
                failed.append(task_id)
                record("runner-handling-failure")
                collected = _handle_runner_failure(
                    args,
                    task,
                    query_characters,
                    exc,
                    logs_dir,
                )
                if collected:
                    completed.append(task_id)
                if args.stop_on_error:
                    break
                if index < total and args.restart_delay > 0:
                    current_task_started_at = None
                    current_task_deadline_at = None
                    record("runner-inter-task-delay")
                    time.sleep(args.restart_delay)

        current_task = None
        current_task_started_at = None
        current_task_deadline_at = None
        record("runner-done")
        normal_completed = len([
            task_id
            for task_id in completed
            if task_id not in timed_out and task_id not in failed
        ])
        print(
            f"\n[runner] 阶段结束：正常完成 {normal_completed}，"
            f"超时后已收集 {len(timed_out)}，失败 {len(failed)}"
        )
        if timed_out:
            print(f"[runner] 超时后已按正常目录落盘并进入 judge：{timed_out}")
        if failed:
            judged_failures = [task_id for task_id in failed if task_id in completed]
            unjudged_failures = [task_id for task_id in failed if task_id not in completed]
            if judged_failures:
                print(f"[runner] 失败但已收集 Trace、将进入 judge：{judged_failures}")
            if unjudged_failures:
                print(f"[runner] 失败且无可用 Trace、judge 将跳过：{unjudged_failures}")
        return completed, failed
    except BaseException:
        try:
            record("runner-interrupted")
        except OSError:
            pass
        raise


def _handle_runner_failure(
    args: argparse.Namespace,
    task: TaskSpec,
    query_characters: int | None,
    exc: Exception,
    logs_dir: Path,
) -> bool:
    """失败任务尽力 force-stop，并把可用证据写入标准 task<ID> 目录。"""
    if not args.no_force_stop:
        try:
            force_stop(target=args.target, verbose=args.verbose)
            print(f"[runner] task{task.task_id} 失败后已 force-stop")
        except Exception as stop_exc:
            print(f"[runner] task{task.task_id} 失败后 force-stop 也失败：{stop_exc}", file=sys.stderr)

    active_log = getattr(exc, "active_log", None)
    if active_log is not None and args.settle > 0:
        time.sleep(args.settle)
    if active_log is not None:
        try:
            pull_log(
                active_log,
                task=task,
                query_characters=query_characters,
                out_dir=logs_dir,
                target=args.target,
                status="failed",
                failure_reason=str(exc),
                verbose=args.verbose,
            )
            print(f"[runner] task{task.task_id} 失败日志已落盘到 {logs_dir}/task{task.task_id}/")
            return True
        except Exception as pull_exc:
            print(f"[runner] task{task.task_id} 失败日志拉取也失败：{pull_exc}", file=sys.stderr)
            task_name = f"task{task.task_id}"
            task_dir = logs_dir / task_name
            trace_path = task_dir / f"{task_name}.jsonl"
            if trace_path.is_file():
                failure_meta = {
                    "task_id": task.task_id,
                    "metadata_path": str(task.metadata_path),
                    "query_characters": query_characters,
                    "status": "failed",
                    "failure_reason": str(exc),
                    "artifact_error": str(pull_exc),
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
                (task_dir / f"{task_name}.meta.json").write_text(
                    json.dumps(failure_meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    f"[runner] task{task.task_id} 已保留可用 Trace，仍将进入 judge",
                    file=sys.stderr,
                )
                return True
            task_dir.mkdir(parents=True, exist_ok=True)
            failure_meta = {
                "task_id": task.task_id,
                "metadata_path": str(task.metadata_path),
                "query_characters": query_characters,
                "status": "failed",
                "failure_reason": str(exc),
                "artifact_error": str(pull_exc),
                "remote_log": active_log.path,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            (task_dir / f"{task_name}.meta.json").write_text(
                json.dumps(failure_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    else:
        task_name = f"task{task.task_id}"
        task_dir = logs_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        failure_meta = {
            "task_id": task.task_id,
            "metadata_path": str(task.metadata_path),
            "query_characters": query_characters,
            "status": type(exc).__name__,
            "failure_reason": str(exc),
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        (task_dir / f"{task_name}.meta.json").write_text(
            json.dumps(failure_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return False


def _handle_runner_timeout(
    args: argparse.Namespace,
    task: TaskSpec,
    query_characters: int | None,
    exc: TaskTimeoutError,
    logs_dir: Path,
) -> bool:
    """Pull timeout artifacts into task<ID>, then stop XiaoYi like a normal completion."""
    task_name = f"task{task.task_id}"
    task_dir = logs_dir / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    collected = False

    if exc.active_log is not None:
        if args.settle > 0:
            time.sleep(args.settle)
        try:
            pull_log(
                exc.active_log,
                task=task,
                query_characters=query_characters,
                out_dir=logs_dir,
                target=args.target,
                status="timeout",
                failure_reason=str(exc),
                verbose=args.verbose,
            )
            collected = True
            print(
                f"[runner] task{task.task_id} 超时日志与 output 已落盘到 "
                f"{task_dir}/"
            )
        except Exception as pull_exc:
            trace_path = task_dir / f"{task_name}.jsonl"
            collected = trace_path.is_file()
            failure_meta = {
                "task_id": task.task_id,
                "metadata_path": str(task.metadata_path),
                "query_characters": query_characters,
                "status": "timeout",
                "failure_reason": str(exc),
                "artifact_error": str(pull_exc),
                "remote_log": exc.active_log.path,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            (task_dir / f"{task_name}.meta.json").write_text(
                json.dumps(failure_meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(
                f"[runner] task{task.task_id} 超时日志拉取失败：{pull_exc}",
                file=sys.stderr,
            )
            if collected:
                print(
                    f"[runner] task{task.task_id} 已保留可用 Trace，仍将进入 judge",
                    file=sys.stderr,
                )
    else:
        failure_meta = {
            "task_id": task.task_id,
            "metadata_path": str(task.metadata_path),
            "query_characters": query_characters,
            "status": "timeout_no_session_log",
            "failure_reason": str(exc),
            "remote_log": None,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        (task_dir / f"{task_name}.meta.json").write_text(
            json.dumps(failure_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[runner] task{task.task_id} 超时前未发现 session 日志，无法进入 judge。",
            file=sys.stderr,
        )

    if not args.no_force_stop:
        try:
            force_stop(target=args.target, verbose=args.verbose)
            print(f"[runner] task{task.task_id} 超时处理后已 force-stop")
        except Exception as stop_exc:
            print(
                f"[runner] task{task.task_id} 超时处理后 force-stop 失败：{stop_exc}",
                file=sys.stderr,
            )
    return collected


def run_judge_phase(
    args: argparse.Namespace,
    logs_dir: Path,
    run_dir: Path,
    runner_completed: list[int],
) -> int:
    """prepare + judge 两步，止于 judge_result.json。返回 judge cli 退出码。"""
    requested_task_ids = list(getattr(args, "judge_task_ids", None) or [])
    skip_runner = getattr(args, "skip_runner", False)
    if skip_runner:
        selected_task_ids = list(requested_task_ids)
    elif requested_task_ids:
        completed = {str(task_id) for task_id in runner_completed}
        selected_task_ids = [
            task_id
            for task_id in requested_task_ids
            if task_id.removeprefix("task_").removeprefix("task") in completed
        ]
    else:
        selected_task_ids = [str(task_id) for task_id in runner_completed]
    if not skip_runner and not selected_task_ids:
        print("\n[judge] 本轮没有收集到可用 Trace，跳过 prepare 和 judge")
        return 0
    print(f"\n[judge] prepare：logs_dir={logs_dir}，run_dir={run_dir}")
    prepare_argv = [
        "prepare",
        "--logs-dir", str(logs_dir),
        "--task-root", str(Path(args.tasks_root).expanduser().resolve()),
        "--run-dir", str(run_dir),
        "--overwrite",
    ]
    for task_id in selected_task_ids:
        prepare_argv.extend(["--task-id", task_id])
    if args.verbose:
        print(f"[judge] prepare argv: {prepare_argv}")
    prepare_rc = judge_cli_main(prepare_argv)
    if prepare_rc != 0:
        print(f"[judge] prepare 失败，退出码 {prepare_rc}，跳过 judge 阶段", file=sys.stderr)
        return prepare_rc
    print("[judge] prepare 完成")

    judge_argv: list[str] = [
        "judge",
        "--run-dir", str(run_dir),
        "--profiles-file", str(Path(args.profiles_file).expanduser().resolve()),
    ]
    judge_argv.append("--resume" if args.judge_skip_existing else "--overwrite")
    if args.judge_profile:
        judge_argv.extend(["--profile", args.judge_profile])
    if args.judge_trace_mode:
        judge_argv.extend(["--trace-mode", args.judge_trace_mode])
    for task_id in selected_task_ids:
        judge_argv.extend(["--task-id", task_id])
    if args.verbose:
        print(f"[judge] judge argv: {judge_argv}")

    print(f"[judge] judge：run_dir={run_dir}")
    judge_rc = judge_cli_main(judge_argv)
    if judge_rc != 0:
        print(f"[judge] judge 阶段返回非零退出码 {judge_rc}", file=sys.stderr)
    else:
        profile = args.judge_profile or os.environ.get("JUDGE_PROFILE", "")
        results_root = run_dir / "results" / (profile or "")
        print(f"[judge] judge 完成，结果目录：{results_root}")
        print("[judge] 流水线止于 judge_result.json，未生成 Excel")
    return judge_rc


def _config_arguments(argv: Sequence[str]) -> tuple[Path | None, bool]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--no-default-config", action="store_true")
    known, _ = parser.parse_known_args(argv)
    config = Path(known.config).expanduser().resolve() if known.config else None
    return config, bool(known.no_default_config)


def main(argv: Sequence[str] | None = None) -> int:
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    explicit_config, no_default_config = _config_arguments(argv_list)
    selected_config = (
        explicit_config
        or (None if no_default_config else default_config_path(_PROJECT_ROOT))
    )
    # local.toml 是唯一的现代配置源。只有它不存在时才加载旧版 .env，
    # 避免两份本机配置互相遮蔽、让迁移后的行为难以解释。
    if selected_config is not None and not selected_config.is_file():
        _load_root_env()
    try:
        settings = load_local_settings(
            project_root=_PROJECT_ROOT,
            config_path=explicit_config,
            discover_default_config=not no_default_config,
        )
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    apply_local_environment(settings)
    args = _build_parser(settings).parse_args(argv_list)
    os.environ["XIAOYI_HDC"] = args.hdc

    effective_settings = replace(
        settings,
        tasks_root=(
            Path(args.tasks_root).expanduser().resolve()
            if args.tasks_root
            else None
        ),
        logs_dir=Path(args.logs_dir).expanduser().resolve(),
        run_dir=Path(args.run_dir).expanduser().resolve(),
        state_file=Path(args.state_file).expanduser().resolve(),
        hdc=args.hdc,
        target=args.target,
        user_id=args.user_id,
        judge_profile=args.judge_profile,
        judge_trace_mode=args.judge_trace_mode,
        judge_skip_existing=args.judge_skip_existing,
        profiles_file=Path(args.profiles_file).expanduser().resolve(),
    )
    if args.doctor:
        return run_doctor(effective_settings)

    if not args.tasks and not args.skip_runner:
        print("参数错误：runner 阶段至少需要一个 task。", file=sys.stderr)
        return 2

    if args.tasks and args.tasks_root is None and any(
        not (Path(raw).exists() or "/" in raw or "\\" in raw or raw.lower().endswith(".json"))
        for raw in args.tasks
    ):
        print("参数错误：使用任务编号时必须提供 --tasks-root", file=sys.stderr)
        return 2

    logs_dir = Path(args.logs_dir).expanduser().resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_dir = _resolve_run_dir(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    runner_completed: list[int] = []
    runner_failed: list[int] = []
    if not args.skip_runner:
        try:
            tasks = collect_task_specs(
                args.tasks,
                tasks_root=args.tasks_root,
                min_task=args.min_task,
                max_task=args.max_task,
            )
        except ValueError as exc:
            print(f"参数错误：{exc}", file=sys.stderr)
            return 2
        if args.tasks_root is None:
            metadata_roots = {task.metadata_path.parent.parent for task in tasks}
            if len(metadata_roots) == 1:
                args.tasks_root = str(next(iter(metadata_roots)))
        runner_completed, runner_failed = run_runner_phase(args, tasks, logs_dir)
        if not runner_completed:
            print("[pipeline] runner 阶段未收集到可用 Trace，跳过 judge", file=sys.stderr)
            return 1
        if args.stop_on_error and runner_failed:
            print("[pipeline] --stop-on-error 已触发，仍继续 judge 已收集 Trace 的任务", file=sys.stderr)
    else:
        print("[pipeline] --skip-runner：直接用已有日志跑 judge")

    if args.skip_judge:
        print("[pipeline] --skip-judge：只跑 runner，结束")
        return 0

    if args.tasks_root is None:
        print("参数错误：judge 阶段需要 --tasks-root（metadata 来源）", file=sys.stderr)
        return 2

    return run_judge_phase(args, logs_dir, run_dir, runner_completed)


if __name__ == "__main__":
    raise SystemExit(main())
