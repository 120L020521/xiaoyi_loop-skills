#!/usr/bin/env python3
"""Command-line entry for the standalone XiaoYi weekly-report runner.

This file contains only the user-facing CLI.  The readable runner source lives
under ``standalone_weekly/`` and does not depend on the installed Skills tree.
"""

from __future__ import annotations

import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType


SCRIPT_ROOT = Path(__file__).resolve().parent
STANDALONE_ROOT = SCRIPT_ROOT / "standalone_weekly"
LAUNCHER_PATH = STANDALONE_ROOT / "scripts" / "run_weekly.py"

TARGETS: dict[str, tuple[str, int]] = {
    "z1": ("zhouzeyu", 3),
    "z2": ("zhouzeyu", 2),
    "s1": ("苏晚", 1),
    "s2": ("苏晚", 2),
    "t1": ("唐可", 1),
    "t2": ("唐可", 2),
    "c1": ("陈景明", 1),
    "c2": ("陈景明", 2),
    "f1": ("方一诺", 1),
    "f2": ("方一诺", 2),
}

WEEK_LABELS = {1: "第一周", 2: "第二周",3:"firstweek"}


def _load_launcher() -> ModuleType:
    """Load the extracted canonical launcher from readable source files."""
    if not LAUNCHER_PATH.is_file():
        raise RuntimeError(
            f"找不到独立 Runner：{LAUNCHER_PATH}\n"
            "请同时复制 run_weekly.py 和 standalone_weekly/ 目录。"
        )

    scripts_root = LAUNCHER_PATH.parent
    scripts_root_text = str(scripts_root)
    if scripts_root_text not in sys.path:
        sys.path.insert(0, scripts_root_text)

    spec = importlib.util.spec_from_file_location(
        "standalone_weekly_launcher", LAUNCHER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载独立 Runner：{LAUNCHER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_targets(values: list[str]) -> list[str]:
    """Accept space-, comma-, or Chinese-comma-separated target codes."""
    targets: list[str] = []
    for value in values:
        targets.extend(
            item.lower()
            for item in re.split(r"[,，\s]+", value.strip())
            if item.strip()
        )
    return list(dict.fromkeys(targets))


def _resolve_task_id(project_root: Path, target: str) -> str:
    """Map a note target such as c1 to one globally unique absolute_id."""
    try:
        person, week = TARGETS[target]
    except KeyError as exc:
        supported = " ".join(TARGETS)
        raise ValueError(f"未知 target {target!r}；可用值：{supported}") from exc

    person_root = project_root / "task" / person
    if not person_root.is_dir():
        raise ValueError(f"找不到人员任务目录：{person_root}")

    week_label = WEEK_LABELS[week]
    matches: list[tuple[str, Path]] = []
    for metadata_path in person_root.glob("*/metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        explicit_target = str(metadata.get("mock_target") or "").strip().lower()
        task_text = str(metadata.get("task") or "")
        configured_week = str(metadata.get("mock_week") or "").strip()

        if explicit_target:
            selected = explicit_target == target
        else:
            selected = week_label in task_text or configured_week in {
                str(week),
                week_label,
            }
        if not selected:
            continue

        task_id = str(metadata.get("absolute_id") or "").strip()
        if task_id:
            matches.append((task_id, metadata_path))

    if not matches:
        raise ValueError(f"未找到 {target} 对应的 {person}{week_label} metadata")
    if len(matches) > 1:
        paths = ", ".join(str(path) for _, path in matches)
        raise ValueError(f"{target} 对应的 metadata 不唯一：{paths}")
    return matches[0][0]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 c1/c2 等 note target 串行执行小艺周报 Runner"
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="一个或多个 note target，例如 c1 c2 f1；也支持逗号分隔",
    )
    parser.add_argument(
        "--project-root",
        default=str(SCRIPT_ROOT),
        help="包含 task/ 和 deliverables_final/ 的数据根目录；默认也从这里找 note/",
    )
    parser.add_argument(
        "--note-root",
        help="note 目录，内部需包含 data_yangshi/jiaoben/run_data_mock.py",
    )
    parser.add_argument(
        "--mock-runner-script",
        help="直接指定 run_data_mock.py；优先级高于 --note-root",
    )
    parser.add_argument(
        "--agent-workspace",
        help="批次产物输出根目录；默认使用执行命令时的当前目录",
    )
    parser.add_argument("--config", help="自定义周报 Runner JSON 配置")
    parser.add_argument("--device", help="HDC 目标设备 ID")
    parser.add_argument("--date", help="运行日期 YYYYMMDD")
    parser.add_argument("--dry-run", action="store_true", help="只预演，不操作 HDC")
    parser.add_argument("--rerun", action="store_true", help="覆盖已完成的任务")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="列出 target 与人员/周次映射后退出",
    )
    return parser


def _mock_runner_path(args: argparse.Namespace, project_root: Path) -> Path:
    if args.mock_runner_script:
        return Path(args.mock_runner_script).expanduser().resolve()
    note_root = (
        Path(args.note_root).expanduser().resolve()
        if args.note_root
        else project_root / "note"
    )
    return note_root / "data_yangshi" / "jiaoben" / "run_data_mock.py"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list_targets:
        for target, (person, week) in TARGETS.items():
            print(f"{target}: {person}{WEEK_LABELS[week]}")
        return 0

    targets = _normalize_targets(args.targets)
    if not targets:
        parser.error("请至少指定一个 target，例如：c1 c2")

    project_root = Path(args.project_root).expanduser().resolve()
    if not (project_root / "task").is_dir():
        parser.error(f"找不到 task 目录：{project_root / 'task'}")
    if not (project_root / "deliverables_final").is_dir():
        parser.error(
            f"找不到 deliverables_final 目录：{project_root / 'deliverables_final'}"
        )

    mock_runner_script = _mock_runner_path(args, project_root)
    if not mock_runner_script.is_file():
        parser.error(f"找不到 note 数据入口：{mock_runner_script}")

    try:
        selections = [
            (target, _resolve_task_id(project_root, target)) for target in targets
        ]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    forwarded = [
        "--project-root",
        str(project_root),
        "--mock-runner-script",
        str(mock_runner_script),
    ]
    for name in ("agent_workspace", "config", "device", "date"):
        value = getattr(args, name)
        if value:
            forwarded.extend([f"--{name.replace('_', '-')}", value])
    for flag in ("dry_run", "rerun", "stop_on_error", "verbose"):
        if getattr(args, flag):
            forwarded.append(f"--{flag.replace('_', '-')}")

    for target, task_id in selections:
        person, week = TARGETS[target]
        print(
            f"[selection] {target} -> {person}{WEEK_LABELS[week]} -> Task {task_id}"
        )
        forwarded.extend(["--task", task_id])

    launcher = _load_launcher()
    return int(launcher.main(forwarded))


if __name__ == "__main__":
    raise SystemExit(main())
