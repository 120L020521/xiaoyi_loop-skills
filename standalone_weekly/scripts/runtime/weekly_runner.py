#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串行执行周报 metadata：note 清空+推送、执行、拉取。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .case_manager import (
    is_case_completed,
    mark_case_completed,
    mark_case_failed,
    mark_case_interrupted,
)
from .hdc_client import (
    HdcError,
    RemoteLog,
    changed_logs,
    hdc_path,
    list_remote_logs,
    remote_shell,
    run_hdc,
    shell_quote,
    snapshot,
    target_args,
)
from .log_monitor import (
    TaskExecutionError,
    TaskTimeoutError,
    read_remote_stop_candidates,
    terminal_stop_reason,
    today_id,
)
from .task_executor import (
    extract_stop_content,
    force_stop,
    pull_log,
    save_prompt_text,
    start_prompt,
)


RUNNER_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = RUNNER_ROOT.parent
DEFAULT_CONFIG = WORKSPACE_ROOT / "assets" / "weekly_config.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

DEFAULTS: dict[str, Any] = {
    "metadata_root": "external:task",
    "deliverables_root": "external:deliverables_final",
    "scripts_root": "../scripts/runtime",
    "mock_runner_script": "external:note/data_yangshi/jiaoben/run_data_mock.py",
    "output_root": "external:xiaoyi_logs",
    "month": "2026-07",
    "calendar_start": "2026-07-01",
    "calendar_end": "2026-07-31",
    "xiaoyi_timeout": 1800,
    "helper_timeout": 300,
    "poll_seconds": 3,
    "task_interval": 3,
    "person_interval": 5,
    "artifact_wait_timeout_seconds": 45,
    "artifact_poll_seconds": 2,
    "artifact_stable_checks": 2,
    "prompt_suffix": "",
}

_XIAOYI_WORKSPACE_ROOT = "/storage/media/100/local/files/Docs/.xiaoyi/workspace"
_WEEKLY_WORKLOG_RELATIVE_ROOT = "memory/weekly-work-report/worklog"
_WEEKLY_SUMMARY_RELATIVE_ROOT = "memory/weekly-work-report/summary"
_MOCK_PERSON_PREFIXES = {
    "周泽宇": "z",
    "苏晚": "s",
    "唐可": "t",
    "陈景明": "c",
    "方一诺": "f",
}
_MOCK_WEEK_LABELS = {
    "第一周": 1,
    "第二周": 2,
}
# Retain the old parser API for callers that import it, but the active Runner no
# longer uses log-declared paths or Desktop fallbacks to collect artifacts.
_VIRTUAL_OUTPUT_PATH_MAPPINGS: tuple[tuple[str, str], ...] = ()
_LOGGED_FILE_EXTENSIONS = "md|markdown|html?|docx?|pdf|xlsx?|csv|jsonl?|txt|log"
_WORKLOG_PATH_HINTS = ("worklog", "work_log", "work-log", "工作日志", "工作记录")
_EXCLUDED_OUTPUT_SEGMENTS = ("工作快捷区", "文件输出")
_CONFIRMATION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"请.{0,20}确认",
        r"请.{0,12}(?:授权|允许)",
        r"需要.{0,20}确认",
        r"需要(?:你|您)?(?:授权|允许|选择)",
        r"确认后.{0,30}(?:继续|生成|创建|保存|执行|读取|访问)",
        r"是否.{0,30}(?:继续|生成|创建|保存|执行|读取|访问|使用|授权|允许)",
        r"(?:要不要|是否要).{0,30}(?:继续|生成|创建|保存|执行|读取)",
        r"(?:可以|能否).{0,30}(?:开始|继续|生成|创建|保存|执行|读取|访问).{0,8}[吗么？?]",
        r"请选择(?:方案|格式|范围|方式)?",
        r"(?:你|您).{0,10}(?:倾向|选择|希望采用|想选)",
    )
)
_PARTIAL_OR_FAILURE_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"无法(?:完成|生成|保存|创建|读取)",
        r"暂时(?:无法|不能)",
        r"(?:生成|保存|创建|读取|执行).{0,12}失败",
        r"未能(?:完成|生成|保存|创建|读取)",
        r"需要手动处理",
        r"请(?:重新)?提供.{0,20}(?:文件|数据|权限|信息)",
        r"尚未完成|未完成的部分",
    )
)
_FUTURE_ONLY_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"(?:准备|接下来|下一步|将会|将要).{0,20}(?:生成|创建|保存|整理|读取)",
        r"待确认|等待确认",
    )
)
_COMPLETION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"(?:周报|日报|报告|worklog|工作日志|工作记录).{0,24}(?:已|已经)(?:生成|创建|保存|完成)",
        r"(?:已|已经)(?:生成|创建|保存|完成).{0,24}(?:周报|日报|报告|worklog|工作日志|工作记录)",
        r"(?:任务|处理).{0,12}(?:已完成|已经完成|完成成功)",
    )
)
_COURTESY_QUESTION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"(?:还有|有).{0,20}(?:其他|其它).{0,20}(?:需要|可以|帮忙)",
        r"是否还需要(?:我)?.{0,24}(?:处理|协助|帮忙|调整|修改)",
        r"如(?:还)?需(?:要)?.{0,24}(?:处理|协助|调整|修改).{0,8}(?:请告诉我|请告知)",
    )
)

_DELIVERY_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("docx", re.compile(r"(?:\.docx\b|\bdocx\b|Word\s*文档|Word\s*文件)", re.IGNORECASE)),
    ("html", re.compile(r"(?:\.html?\b|\bhtml?\b|网页(?:文件|格式)?)", re.IGNORECASE)),
    ("pdf", re.compile(r"(?:\.pdf\b|\bpdf\b)", re.IGNORECASE)),
    ("xlsx", re.compile(r"(?:\.xlsx\b|\bxlsx\b|Excel\s*(?:工作簿|文件|表格))", re.IGNORECASE)),
    ("csv", re.compile(r"(?:\.csv\b|\bcsv\b)", re.IGNORECASE)),
    ("md", re.compile(r"(?:\.md\b|\bmarkdown\b|Markdown\s*文件)", re.IGNORECASE)),
    ("txt", re.compile(r"(?:\.txt\b|\btxt\b|纯文本文件)", re.IGNORECASE)),
    ("pptx", re.compile(r"(?:\.pptx\b|\bpptx\b|PowerPoint\s*(?:演示文稿|文件))", re.IGNORECASE)),
)
_DEFAULT_DELIVERY_FORMATS = frozenset({"docx", "html"})
_EXPLICIT_EXTENSION_PATTERN = re.compile(
    r"\.(docx?|html?|pdf|xlsx?|csv|md|markdown|jsonl?|txt|pptx?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WeeklyTask:
    person: str
    task_id: str
    metadata_path: Path
    metadata: dict[str, Any]


def _task_case_id(task_id: str) -> str:
    """Return a filesystem-safe metadata.absolute_id without a ``task`` prefix."""
    if isinstance(task_id, bool) or not isinstance(task_id, (str, int)):
        raise ValueError(f"metadata.absolute_id 必须是字符串或整数: {task_id!r}")
    normalized = str(task_id).strip()
    invalid_chars = '<>:"/\\|?*'
    if (
        not normalized
        or normalized in {".", ".."}
        or ".." in normalized
        or normalized.endswith((" ", "."))
        or any(char in normalized for char in invalid_chars)
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError(f"不安全的 metadata.absolute_id: {task_id!r}")
    return normalized


def _task_sort_key(task_id: str) -> tuple[int, int | str]:
    normalized = _task_case_id(task_id)
    return (0, int(normalized)) if normalized.isdigit() else (1, normalized.casefold())


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    mtime: int
    root_label: str
    root_path: str


def _is_worklog_path(path: str) -> bool:
    normalized = path.casefold()
    return any(hint.casefold() in normalized for hint in _WORKLOG_PATH_HINTS)


def _has_logged_file_extension(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower().lstrip(".")
    return bool(suffix and re.fullmatch(_LOGGED_FILE_EXTENSIONS, suffix, flags=re.IGNORECASE))


def _is_excluded_output_path(path: str) -> bool:
    return any(segment in path for segment in _EXCLUDED_OUTPUT_SEGMENTS)


def _build_execution_prompt(task_text: str, suffix: str | None) -> str:
    task_text = task_text.rstrip()
    suffix = (suffix or "").strip()
    if not suffix or task_text.endswith(suffix):
        return task_text
    return f"{task_text}\n{suffix}"


def classify_stop_content(content: str | None) -> str:
    """Classify the terminal state represented by XiaoYi's first stop reply."""
    text = (content or "").strip()
    if not text:
        return "missing-content"
    if any(pattern.search(text) for pattern in _PARTIAL_OR_FAILURE_PATTERNS):
        return "partial-or-failed"
    completed = any(pattern.search(text) for pattern in _COMPLETION_PATTERNS)
    courtesy = any(pattern.search(text) for pattern in _COURTESY_QUESTION_PATTERNS)
    if completed and courtesy:
        return "complete"
    if any(pattern.search(text) for pattern in _CONFIRMATION_PATTERNS):
        return "needs-confirmation"
    if any(pattern.search(text) for pattern in _FUTURE_ONLY_PATTERNS):
        return "needs-confirmation"
    return "complete"


def _save_dialog_state(
    task_dir: Path,
    *,
    session_id: str,
    round_number: int,
    verdict: str,
    warnings: list[str] | None = None,
) -> None:
    meta_path = task_dir / f"{task_dir.name}.meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            pass
    meta.update(
        {
            "session_id": session_id or None,
            "dialog_round": round_number,
            "dialog_verdict": verdict,
        }
    )
    if warnings is not None:
        meta["runner_warnings"] = list(warnings)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _configured_remote_roots(remote_output_roots: dict[str, str]) -> list[tuple[str, str]]:
    roots = [(str(path).rstrip("/"), str(label)) for label, path in remote_output_roots.items()]
    return sorted(roots, key=lambda item: len(item[0]), reverse=True)


def map_logged_path_to_remote(
    logged_path: str, remote_output_roots: dict[str, str]
) -> dict[str, str] | None:
    """Map Xiaoyi's user-facing file path to the HDC-visible output path."""
    original = logged_path.strip().strip("`\"'")
    if original.startswith("file://"):
        original = original[7:]
    original = original.rstrip(".,;:!?，。；：！？)]}》")
    mapped = original
    for virtual_root, physical_root in sorted(
        _VIRTUAL_OUTPUT_PATH_MAPPINGS, key=lambda item: len(item[0]), reverse=True
    ):
        if original == virtual_root or original.startswith(virtual_root + "/"):
            mapped = physical_root + original[len(virtual_root):]
            break
    for root_path, root_label in _configured_remote_roots(remote_output_roots):
        if mapped == root_path or mapped.startswith(root_path + "/"):
            return {
                "logged_path": logged_path,
                "remote_path": mapped,
                "root_label": root_label,
                "root_path": root_path,
            }
    return None


def map_relative_path_to_workspace(logged_path: str, session_id: str | None) -> dict[str, str] | None:
    """Map a XiaoYi tool-relative output path to its session workspace."""
    if not session_id:
        return None
    original = logged_path.strip().strip("`\"'")
    if not original or original.startswith("file://"):
        return None
    normalized = original.replace("\\", "/").strip()
    normalized = normalized.rstrip(".,;:!?，。；：！?]})、")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None
    if "://" in normalized:
        return None
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    relative = PurePosixPath(*parts).as_posix()
    if not _has_logged_file_extension(relative) and not _is_worklog_path(relative):
        return None
    root_path = f"{_XIAOYI_WORKSPACE_ROOT}/{session_id}"
    return {
        "logged_path": logged_path,
        "remote_path": f"{root_path}/{relative}",
        "root_label": "",
        "root_path": root_path,
    }


def _walk_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                nested = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                return
            if nested != value:
                yield from _walk_json_strings(nested)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json_strings(item)


_OUTPUT_PATH_KEYS = {
    "filepath", "outputpath", "outputdir", "savedir", "savepath", "savedpath",
    "destinationpath", "targetpath", "directory", "folder", "worklogdir",
}
_WRITE_TOOL_HINTS = ("write", "create", "save", "export", "document", "docx", "worklog")


def _walk_output_path_values(value: Any, *, allow_plain_path: bool = False) -> Iterable[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                yield from _walk_output_path_values(
                    json.loads(stripped), allow_plain_path=allow_plain_path
                )
            except json.JSONDecodeError:
                return
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if isinstance(item, str) and (
                normalized in _OUTPUT_PATH_KEYS or (allow_plain_path and normalized == "path")
            ):
                yield item
            yield from _walk_output_path_values(item, allow_plain_path=allow_plain_path)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_output_path_values(item, allow_plain_path=allow_plain_path)


def _bash_output_fragments(command: str) -> Iterable[str]:
    marker = re.compile(
        r"(?:>>?|--output(?:=|\s+)|--out(?:=|\s+)|-o\s+|"
        r"save(?:_as)?\s*\(|write(?:_text|_bytes)?\s*\()\s*[\"']?",
        flags=re.IGNORECASE,
    )
    for match in marker.finditer(command):
        yield command[match.end():]


def _paths_from_text(text: str, known_roots: Iterable[str]) -> Iterable[str]:
    stripped = text.strip()
    for root in known_roots:
        if stripped == root or stripped.startswith(root + "/"):
            yield stripped
        pattern = re.compile(
            rf"{re.escape(root)}[^\r\n\"'<>`]*?\.(?:{_LOGGED_FILE_EXTENSIONS})(?=$|[\s\"'<>`\]),，。；;:：])",
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(text):
            yield match.group(0)


def _relative_paths_from_text(text: str) -> Iterable[str]:
    quoted = re.compile(
        rf"[\"']([^\"']+\.(?:{_LOGGED_FILE_EXTENSIONS}))[\"']",
        flags=re.IGNORECASE,
    )
    bare = re.compile(
        rf"(?<![\w./-])([^\s\"'<>`|;&]+\.(?:{_LOGGED_FILE_EXTENSIONS}))"
        r"(?=$|[\s\"'<>`\]),，。；;:])",
        flags=re.IGNORECASE,
    )
    quoted_worklog_dir = re.compile(r"[\"']([^\"']*(?:worklog|work_log|work-log)[^\"']*)[\"']", flags=re.IGNORECASE)
    bare_worklog_dir = re.compile(r"(?<![\w./-])([^\s\"'<>`|;&]*(?:worklog|work_log|work-log)[^\s\"'<>`|;&]*)", flags=re.IGNORECASE)
    for pattern in (quoted, bare, quoted_worklog_dir, bare_worklog_dir):
        for match in pattern.finditer(text):
            yield match.group(1)


def extract_logged_output_paths(
    local_log: Path,
    *,
    start_byte: int,
    remote_output_roots: dict[str, str],
) -> list[dict[str, str]]:
    """Extract output locations only from bytes appended during this task."""
    raw = local_log.read_bytes()
    new_text = raw[max(0, min(start_byte, len(raw))):].decode("utf-8", errors="replace")
    configured_roots = [root for root, _ in _configured_remote_roots(remote_output_roots)]
    known_roots = configured_roots + [item for pair in _VIRTUAL_OUTPUT_PATH_MAPPINGS for item in pair]
    detected: dict[str, dict[str, str]] = {}
    for line in new_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_name = str(event.get("event", "")).lower() if isinstance(event, dict) else ""
        session_id = str(event.get("session_id", "")).strip() if isinstance(event, dict) else ""
        payload = event.get("payload", {}) if isinstance(event, dict) else {}
        values: Iterable[str] = ()
        allow_relative = False
        if event_name == "model_output":
            values = _walk_json_strings(payload.get("assistant", payload))
        elif event_name == "tool_call":
            tool_name = str(payload.get("tool_name", "")).lower()
            args = payload.get("args", {})
            if any(hint in tool_name for hint in _WRITE_TOOL_HINTS):
                values = _walk_json_strings(args)
                allow_relative = True
            elif tool_name in {"bash", "shell", "exec"}:
                command = str(args.get("command", "")) if isinstance(args, dict) else ""
                values = _bash_output_fragments(command)
                allow_relative = True
        elif event_name == "tool_result":
            tool_name = str(payload.get("tool_name", "")).lower()
            values = _walk_output_path_values(
                payload, allow_plain_path=any(hint in tool_name for hint in _WRITE_TOOL_HINTS)
            )
            allow_relative = True
        for value in values:
            for candidate in _paths_from_text(value, known_roots):
                mapped = map_logged_path_to_remote(candidate, remote_output_roots)
                if mapped is not None:
                    detected.setdefault(mapped["remote_path"], mapped)
            if allow_relative:
                for candidate in _relative_paths_from_text(value):
                    mapped = map_relative_path_to_workspace(candidate, session_id)
                    if mapped is not None:
                        detected.setdefault(mapped["remote_path"], mapped)
    return list(detected.values())


def _resolve_path(config_dir: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (config_dir / path).resolve()


def load_weekly_config(config_path: Path) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if config_path.is_file():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"配置必须是 JSON 对象: {config_path}")
        config.update(loaded)
    config_dir = config_path.resolve().parent
    for key in (
        "metadata_root", "deliverables_root", "scripts_root", "mock_runner_script",
        "output_root", "task_artifacts_root",
    ):
        if key in config:
            config[key] = _resolve_path(config_dir, str(config[key]))
    return config


def _task_directory(config: dict[str, Any], task_id: str) -> Path:
    task_artifacts_root = config.get("task_artifacts_root")
    if task_artifacts_root is None:
        return Path(config["output_root"]) / _task_case_id(task_id)
    return Path(task_artifacts_root) / _task_case_id(task_id) / "xiaoyi_file_runs"


def discover_tasks(metadata_root: Path) -> list[WeeklyTask]:
    tasks: list[WeeklyTask] = []
    seen_ids: dict[str, Path] = {}
    for metadata_path in metadata_root.glob("*/*/metadata.json"):
        person = metadata_path.parent.parent.name
        source_id = metadata_path.parent.name
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("adapter") != "weekly-report":
            raise ValueError(f"adapter 不是 weekly-report: {metadata_path}")
        if metadata.get("person") != person:
            raise ValueError(f"person 与目录不一致: {metadata_path}")
        if str(metadata.get("absolute_id")) != source_id:
            raise ValueError(f"absolute_id 与目录不一致: {metadata_path}")
        task_id = _task_case_id(metadata.get("absolute_id"))
        uniqueness_key = task_id.casefold()
        if uniqueness_key in seen_ids:
            raise ValueError(
                f"metadata.absolute_id 重复: {task_id!r}; "
                f"{seen_ids[uniqueness_key]} 与 {metadata_path}"
            )
        seen_ids[uniqueness_key] = metadata_path
        task_text = metadata.get("task")
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError(f"task 为空: {metadata_path}")
        tasks.append(WeeklyTask(person, task_id, metadata_path, metadata))
    return sorted(tasks, key=lambda item: (_task_sort_key(item.task_id), item.person))


def _group_by_person(tasks: Iterable[WeeklyTask]) -> list[tuple[str, list[WeeklyTask]]]:
    grouped: dict[str, list[WeeklyTask]] = {}
    for task in tasks:
        grouped.setdefault(task.person, []).append(task)
    return sorted(
        ((person, sorted(items, key=lambda item: _task_sort_key(item.task_id))) for person, items in grouped.items()),
        key=lambda pair: _task_sort_key(pair[1][0].task_id),
    )


def _stream_command(cmd: list[str], *, cwd: Path) -> None:
    print("$", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="")
    returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(f"子流程失败(exit={returncode}): {' '.join(cmd)}")


def _helper_command(script: Path, *args: str) -> list[str]:
    return [sys.executable, "-B", str(script), *args]


def _resolve_mock_target(task: WeeklyTask) -> str:
    explicit = task.metadata.get("mock_target")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()

    prefix = _MOCK_PERSON_PREFIXES.get(task.person)
    if prefix is None:
        supported = "、".join(_MOCK_PERSON_PREFIXES)
        raise ValueError(
            f"note 数据脚本不支持人员 {task.person!r}；当前支持：{supported}。"
            "可在 metadata 中显式设置 mock_target。"
        )

    configured_week = task.metadata.get("mock_week")
    week: int | None = None
    if isinstance(configured_week, int):
        week = configured_week
    elif isinstance(configured_week, str) and configured_week.isdigit():
        week = int(configured_week)
    if week is None:
        task_text = str(task.metadata.get("task") or "")
        week = next(
            (number for label, number in _MOCK_WEEK_LABELS.items() if label in task_text),
            None,
        )
    if week not in {1, 2}:
        raise ValueError(
            f"无法为 Task {task.task_id} 解析 note 数据目标；当前脚本只声明第一周和第二周。"
            "请在 metadata 中设置 mock_target（例如 c1 或 z2）。"
        )
    return f"{prefix}{week}"


def _call_prepare_task_data(task: WeeklyTask, config: dict[str, Any], *,
                            dry_run: bool) -> None:
    script = Path(config["mock_runner_script"])
    if not script.is_file():
        raise FileNotFoundError(f"note 清空+推送脚本不存在: {script}")
    mock_target = _resolve_mock_target(task)
    cmd = _helper_command(script, mock_target)
    if dry_run:
        print(f"[{task.task_id}] [DRY-RUN] 清空+推送: {' '.join(cmd)}")
        return
    print(f"[{task.task_id}] 清空并推送 note 数据: {mock_target}")
    _stream_command(cmd, cwd=script.parent)


def _safe_relative(remote_file: RemoteFile) -> Path:
    prefix = remote_file.root_path + "/"
    relative = remote_file.path[len(prefix):] if remote_file.path.startswith(prefix) else PurePosixPath(remote_file.path).name
    parts = [part for part in PurePosixPath(relative).parts if part not in {"", ".", "..", "/"}]
    return Path(remote_file.root_label, *parts) if remote_file.root_label else Path(*parts)


def pull_remote_files(files: Iterable[RemoteFile], *, local_root: Path,
                      target: str | None, verbose: bool = False) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for remote_file in files:
        local_path = local_root / _safe_relative(remote_file)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "remote_path": remote_file.path,
            "local_path": str(local_path),
            "size": remote_file.size,
            "mtime": remote_file.mtime,
        }
        try:
            run_hdc(
                [*target_args(target), "file", "recv", remote_file.path, str(local_path)],
                timeout=300,
                verbose=verbose,
            )
            record["status"] = "pulled"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
        manifest.append(record)
    return manifest


def extract_trace_session_id(local_log: Path) -> str:
    """Return the main Agent session_id recorded by the current raw Trace."""
    fallback = ""
    with local_log.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            session_id = event.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip():
                continue
            session_id = session_id.strip()
            if not fallback:
                fallback = session_id
            if event.get("agent_role") == "main":
                return session_id
    return fallback


def _validated_session_id(session_id: str) -> str:
    normalized = session_id.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
    ):
        raise ValueError(f"不安全的 session_id: {session_id!r}")
    return normalized


def _remote_files_from_stat(
    payload: str, *, root_label: str, root_path: str
) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for line in payload.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        mtime_text, size_text, path = parts
        try:
            files.append(
                RemoteFile(
                    path=path,
                    size=int(size_text),
                    mtime=int(mtime_text),
                    root_label=root_label,
                    root_path=root_path,
                )
            )
        except ValueError:
            continue
    return sorted(files, key=lambda item: item.path)


def list_session_workspace_artifacts(
    session_id: str, *, target: str | None, verbose: bool
) -> tuple[list[RemoteFile], list[RemoteFile], list[RemoteFile]]:
    """List session reports plus the workspace-level weekly worklog and summary trees."""
    session_id = _validated_session_id(session_id)
    session_root = f"{_XIAOYI_WORKSPACE_ROOT}/{session_id}"
    worklog_root = f"{_XIAOYI_WORKSPACE_ROOT}/{_WEEKLY_WORKLOG_RELATIVE_ROOT}"
    summary_root = f"{_XIAOYI_WORKSPACE_ROOT}/{_WEEKLY_SUMMARY_RELATIVE_ROOT}"
    quoted_session = shell_quote(session_root)
    quoted_worklog = shell_quote(worklog_root)
    quoted_summary = shell_quote(summary_root)
    command = (
        f"if [ -d {quoted_session} ]; then "
        f"find {quoted_session} -mindepth 1 -maxdepth 1 -type f "
        "-exec stat -c '%Y|%s|%n' {} \\;; fi; "
        "echo __WORKLOG__; "
        f"if [ -d {quoted_worklog} ]; then "
        f"find {quoted_worklog} -type f -exec stat -c '%Y|%s|%n' {{}} \\;; fi; "
        "echo __SUMMARY__; "
        f"if [ -d {quoted_summary} ]; then "
        f"find {quoted_summary} -type f -exec stat -c '%Y|%s|%n' {{}} \\;; fi; "
        "echo __END__"
    )
    output = remote_shell(command, target=target, timeout=90, verbose=verbose)
    before_end = output.split("__END__", 1)[0]
    before_summary, summary_separator, summary_payload = before_end.partition("__SUMMARY__")
    report_payload, worklog_separator, worklog_payload = before_summary.partition("__WORKLOG__")
    if not worklog_separator or not summary_separator:
        raise RuntimeError(f"session workspace 枚举结果缺少分隔标记: {session_root}")
    reports = _remote_files_from_stat(
        report_payload, root_label="", root_path=session_root
    )
    worklogs = _remote_files_from_stat(
        worklog_payload, root_label="worklog", root_path=worklog_root
    )
    summaries = _remote_files_from_stat(
        summary_payload, root_label="summary", root_path=summary_root
    )
    return reports, worklogs, summaries


def wait_for_session_workspace_artifacts(
    session_id: str,
    *,
    target: str | None,
    verbose: bool,
    require_worklog: bool,
    timeout_seconds: float,
    poll_seconds: float,
    stable_checks: int,
    required_formats: set[str] | frozenset[str] | None = None,
) -> tuple[list[RemoteFile], list[RemoteFile], list[RemoteFile]]:
    """Wait until required remote artifacts exist and their stat snapshot is stable."""
    timeout_seconds = max(0.0, float(timeout_seconds))
    poll_seconds = max(0.1, float(poll_seconds))
    stable_checks = max(1, int(stable_checks))
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[tuple[str, int, int], ...] | None = None
    stable_count = 0
    latest: tuple[list[RemoteFile], list[RemoteFile], list[RemoteFile]] = ([], [], [])
    while True:
        latest = list_session_workspace_artifacts(
            session_id, target=target, verbose=verbose
        )
        reports, worklogs, summaries = latest
        signature = tuple(
            (item.path, item.size, item.mtime)
            for item in [*reports, *worklogs, *summaries]
        )
        report_formats = {
            _normalize_delivery_format(PurePosixPath(item.path).suffix)
            for item in reports
        }
        formats_present = (
            set(required_formats).issubset(report_formats)
            if required_formats
            else bool(reports)
        )
        required_present = formats_present and (bool(worklogs) or not require_worklog)
        if required_present and signature == last_signature:
            stable_count += 1
        elif required_present:
            stable_count = 1
        else:
            stable_count = 0
        if required_present and stable_count >= stable_checks:
            return latest
        if time.monotonic() >= deadline:
            return latest
        last_signature = signature
        time.sleep(poll_seconds)


def wait_for_new_stop(*, task_id: str, before: dict[str, tuple[int, int]],
                      target: str | None, timeout_seconds: int,
                      poll_seconds: float, verbose: bool = False) -> RemoteLog:
    deadline = time.monotonic() + timeout_seconds
    active_log: RemoteLog | None = None
    last_message = 0.0
    while time.monotonic() < deadline:
        logs = list_remote_logs(target=target, user_id=None, date_id=today_id(), verbose=False)
        candidates = changed_logs(before, logs)
        if candidates:
            active_log = candidates[0]
        for log in candidates:
            base_size = before.get(log.path, (0, 0))[0]
            text = read_remote_stop_candidates(
                log,
                target=target,
                lines=300,
                start_byte=base_size + 1,
                verbose=verbose and (time.monotonic() - last_message > 180),
            )
            reason = terminal_stop_reason(text)
            if reason == "stop":
                return log
            if reason == "error":
                raise TaskExecutionError(
                    f"{task_id} 主 Agent 返回 stop_reason=error",
                    active_log=active_log,
                )
        if time.monotonic() - last_message > 300:
            print(f"[{task_id}] 等待 baseline 之后的新 stop_reason=stop/error ...")
            last_message = time.monotonic()
        time.sleep(poll_seconds)
    raise TaskTimeoutError(f"{task_id} 等待新 stop_reason=stop 超时", active_log=active_log)


def _present_formats(outputs_dir: Path) -> set[str]:
    if not outputs_dir.is_dir():
        return set()
    return {
        _normalize_delivery_format(path.suffix)
        for path in outputs_dir.rglob("*")
        if path.is_file()
    }


def _normalize_delivery_format(extension: str) -> str:
    normalized = extension.lower().lstrip(".")
    return {"htm": "html", "markdown": "md"}.get(normalized, normalized)


def required_delivery_formats(task_text: str) -> set[str]:
    """Infer explicit deliverable formats from metadata.task, or use docx+html."""
    requested = {
        extension
        for extension, pattern in _DELIVERY_FORMAT_PATTERNS
        if pattern.search(task_text or "")
    }
    requested.update(
        _normalize_delivery_format(match.group(1))
        for match in _EXPLICIT_EXTENSION_PATTERN.finditer(task_text or "")
    )
    return requested or set(_DEFAULT_DELIVERY_FORMATS)


def _mark_weekly_failure(
    case_id: str, run_dir: Path, task_dir: Path, error: str, *, failure_kind: str
) -> None:
    mark_case_failed(case_id, str(run_dir), error, case_dir=str(task_dir))
    marker_path = task_dir / "failed.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["failure_kind"] = failure_kind
    marker_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _task_failure_kind(task: WeeklyTask, config: dict[str, Any]) -> str | None:
    marker_path = _task_directory(config, task.task_id) / "failed.json"
    if not marker_path.is_file():
        return None
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("failure_kind") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None


def _archive_previous_attempt(task_dir: Path) -> None:
    """保留旧尝试，确保本轮格式校验不被历史输出污染。"""
    if not task_dir.is_dir():
        return
    entries = [entry for entry in task_dir.iterdir() if entry.name != "attempts"]
    if not entries:
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive_dir = task_dir / "attempts" / stamp
    archive_dir.mkdir(parents=True, exist_ok=False)
    for entry in entries:
        shutil.move(str(entry), str(archive_dir / entry.name))


def _write_artifact_manifest(task_dir: Path, *, task_id: str,
                             output_records: list[dict[str, Any]],
                             worklog_records: list[dict[str, Any]],
                             summary_records: list[dict[str, Any]],
                             session_id: str) -> None:
    session_root = f"{_XIAOYI_WORKSPACE_ROOT}/{session_id}"
    manifest = {
        "task_id": task_id,
        "case_id": task_dir.name,
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "workspace": {
            "session_root": session_root,
            "worklog_root": f"{_XIAOYI_WORKSPACE_ROOT}/{_WEEKLY_WORKLOG_RELATIVE_ROOT}",
            "summary_root": f"{_XIAOYI_WORKSPACE_ROOT}/{_WEEKLY_SUMMARY_RELATIVE_ROOT}",
        },
        "outputs": output_records,
        "worklogs": worklog_records,
        "summaries": summary_records,
    }
    (task_dir / "artifacts_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve_logged_remote_files(
    logged_paths: list[dict[str, Any]], *, target: str | None, verbose: bool
) -> list[RemoteFile]:
    """Resolve concrete output files; recurse only into logged worklog directories."""
    files: dict[str, RemoteFile] = {}
    for detected in logged_paths:
        remote_path = detected["remote_path"].rstrip("/")
        if _is_excluded_output_path(remote_path):
            detected["status"] = "ignored_internal_workspace"
            continue
        quoted = shell_quote(remote_path)
        if _is_worklog_path(remote_path):
            command = (
                f"if [ -f {quoted} ]; then stat -c '%Y|%s|%n' {quoted}; "
                f"elif [ -d {quoted} ]; then find {quoted} -type f -exec stat -c '%Y|%s|%n' {{}} \\;; "
                "fi; echo __END__"
            )
        else:
            command = f"if [ -f {quoted} ]; then stat -c '%Y|%s|%n' {quoted}; fi; echo __END__"
        try:
            output = remote_shell(command, target=target, timeout=90, verbose=verbose)
        except HdcError as exc:
            detected["status"] = "resolve_failed"
            detected["error"] = str(exc)
            continue
        matched: list[str] = []
        payload = output.split("__END__", 1)[0]
        for line in payload.splitlines():
            parts = line.strip().split("|", 2)
            if len(parts) != 3:
                continue
            mtime_text, size_text, path = parts
            try:
                remote_file = RemoteFile(
                    path=path,
                    size=int(size_text),
                    mtime=int(mtime_text),
                    root_label=detected["root_label"],
                    root_path=detected["root_path"],
                )
            except ValueError:
                continue
            files[path] = remote_file
            matched.append(path)
        detected["matched_files"] = matched
        detected["status"] = "found" if matched else "not_found"
    return sorted(files.values(), key=lambda item: item.path)


def _collect_task_artifacts(
    task_dir: Path,
    *,
    task_id: str,
    target: str | None,
    verbose: bool,
    session_id: str,
    require_worklog: bool = True,
    wait_timeout_seconds: float = 0,
    poll_seconds: float = 2,
    stable_checks: int = 1,
    required_formats: set[str] | frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull reports, worklogs, and summaries from fixed session workspace paths."""
    report_files, worklog_files, summary_files = wait_for_session_workspace_artifacts(
        session_id,
        target=target,
        verbose=verbose,
        require_worklog=require_worklog,
        timeout_seconds=wait_timeout_seconds,
        poll_seconds=poll_seconds,
        stable_checks=stable_checks,
        required_formats=required_formats,
    )
    output_records = pull_remote_files(
        report_files, local_root=task_dir / "outputs", target=target, verbose=verbose
    )
    worklog_records = pull_remote_files(
        worklog_files, local_root=task_dir, target=target, verbose=verbose
    )
    summary_records = pull_remote_files(
        summary_files, local_root=task_dir, target=target, verbose=verbose
    )
    for record in output_records:
        record["selection_source"] = "session-workspace-root"
    for record in worklog_records:
        record["selection_source"] = "workspace-weekly-worklog"
    for record in summary_records:
        record["selection_source"] = "workspace-weekly-summary"
    _write_artifact_manifest(
        task_dir,
        task_id=task_id,
        output_records=output_records,
        worklog_records=worklog_records,
        summary_records=summary_records,
        session_id=session_id,
    )
    return output_records, worklog_records, summary_records


def run_weekly_task(task: WeeklyTask, config: dict[str, Any], *, target: str | None,
                    verbose: bool, dry_run: bool, rerun: bool) -> bool:
    case_id = _task_case_id(task.task_id)
    task_dir = _task_directory(config, task.task_id)
    run_dir = task_dir.parent
    execution_prompt = _build_execution_prompt(task.metadata["task"], config.get("prompt_suffix"))
    required_formats = required_delivery_formats(task.metadata["task"])
    if not rerun and is_case_completed(case_id, str(run_dir), case_dir=str(task_dir)):
        print(f"[{task.task_id}] 已完成，跳过")
        return True

    print(f"\n{'=' * 70}\n[{task.task_id}] {task.person}: {task.metadata['task']}\n{'=' * 70}")
    if dry_run:
        print(f"[{task.task_id}] [DRY-RUN] execution prompt:\n{execution_prompt}")
        print(
            f"[{task.task_id}] [DRY-RUN] 将监控并拉取 Trace，"
            "从 session workspace 拉取周报，并从 workspace memory 拉取 worklog/summary"
        )
        return True

    task_dir.mkdir(parents=True, exist_ok=True)
    _archive_previous_attempt(task_dir)
    shutil.copy2(task.metadata_path, task_dir / "metadata.json")
    save_prompt_text(
        execution_prompt,
        case_id=case_id,
        run_dir=str(run_dir),
        tag="prompt",
        task_dir=task_dir,
    )

    before_logs = snapshot(list_remote_logs(target=target, user_id=None, date_id=today_id(), verbose=verbose))
    active_before = before_logs

    done_log: RemoteLog | None = None
    local_log: Path | None = None
    failure: str | None = None
    interrupted = False
    output_records: list[dict[str, Any]] = []
    worklog_records: list[dict[str, Any]] = []
    summary_records: list[dict[str, Any]] = []
    artifacts_collected = False
    session_id = ""
    runner_warnings: list[str] = []
    round_number = 0
    dialog_verdict = "not-started"
    timed_out = False
    agent_error = False
    try:
        for _single_round in range(1):
            start_prompt(
                execution_prompt,
                target=target,
                verbose=verbose,
            )
            done_log = wait_for_new_stop(
                task_id=task.task_id,
                before=active_before,
                target=target,
                timeout_seconds=int(config["xiaoyi_timeout"]),
                poll_seconds=float(config["poll_seconds"]),
                verbose=verbose,
            )
            time.sleep(1.5)
            local_log = pull_log(
                done_log,
                case_id=case_id,
                run_dir=str(run_dir),
                target=target,
                verbose=verbose,
                task_dir=task_dir,
            )
            stop_content = extract_stop_content(
                local_log, case_id, run_dir, task_dir=task_dir
            )
            print(f"[{task.task_id}] 从当前 Trace 解析 session_id...")
            session_id = extract_trace_session_id(local_log)
            if not session_id:
                warning = (
                    "当前 Trace 未提供 session_id；无法定位本次任务的 "
                    ".xiaoyi/workspace 产物目录"
                )
                if warning not in runner_warnings:
                    runner_warnings.append(warning)
                print(f"[{task.task_id}] WARNING: {warning}", file=sys.stderr)
            dialog_verdict = classify_stop_content(stop_content)
            if session_id:
                output_records, worklog_records, summary_records = _collect_task_artifacts(
                    task_dir,
                    task_id=task.task_id,
                    target=target,
                    verbose=verbose,
                    session_id=session_id,
                    require_worklog=False,
                    wait_timeout_seconds=float(config.get("artifact_wait_timeout_seconds", 45)),
                    poll_seconds=float(config.get("artifact_poll_seconds", 2)),
                    stable_checks=int(config.get("artifact_stable_checks", 2)),
                    required_formats=required_formats,
                )
                artifacts_collected = True
            print(
                f"[{task.task_id}] 对话轮次 {round_number}: {dialog_verdict}"
            )
            _save_dialog_state(
                task_dir,
                session_id=session_id,
                round_number=round_number,
                verdict=dialog_verdict,
            )

            break

    except KeyboardInterrupt:
        interrupted = True
        failure = "手动中断"
    except Exception as exc:
        failure = str(exc)
        timed_out = isinstance(exc, TaskTimeoutError)
        agent_error = isinstance(exc, TaskExecutionError)
        print(f"[{task.task_id}] 执行失败: {exc}", file=sys.stderr)
        if isinstance(exc, (TaskTimeoutError, TaskExecutionError)) and exc.active_log is not None:
            done_log = exc.active_log
    finally:
        try:
            force_stop(target=target, verbose=verbose)
        except Exception as exc:
            failure = failure or f"force-stop 失败: {exc}"

    if local_log is None and done_log is None:
        try:
            current_logs = list_remote_logs(target=target, user_id=None, date_id=today_id(), verbose=verbose)
            candidates = changed_logs(active_before, current_logs)
            done_log = candidates[0] if candidates else None
        except Exception:
            done_log = None
    if local_log is None and done_log is not None:
        try:
            local_log = pull_log(
                done_log,
                case_id=case_id,
                run_dir=str(run_dir),
                target=target,
                verbose=verbose,
                task_dir=task_dir,
            )
            extract_stop_content(local_log, case_id, run_dir, task_dir=task_dir)
            session_id = extract_trace_session_id(local_log)
        except Exception as exc:
            failure = failure or f"日志拉取失败: {exc}"

    if not artifacts_collected:
        try:
            if not session_id:
                raise RuntimeError("当前 Trace 没有 session_id，无法定位任务 workspace")
            output_records, worklog_records, summary_records = _collect_task_artifacts(
                task_dir,
                task_id=task.task_id,
                target=target,
                verbose=verbose,
                session_id=session_id,
                require_worklog=False,
                wait_timeout_seconds=float(config.get("artifact_wait_timeout_seconds", 45)),
                poll_seconds=float(config.get("artifact_poll_seconds", 2)),
                stable_checks=int(config.get("artifact_stable_checks", 2)),
                required_formats=required_formats,
            )
            artifacts_collected = True
        except Exception as exc:
            failure = failure or f"产物拉取失败: {exc}"

    present_formats = _present_formats(task_dir / "outputs")
    missing_formats = required_formats - present_formats
    if missing_formats:
        failure = failure or (
            "缺少要求的交付件格式: " + ", ".join(sorted(missing_formats))
        )

    _save_dialog_state(
        task_dir,
        session_id=session_id,
        round_number=round_number,
        verdict=dialog_verdict,
        warnings=runner_warnings,
    )

    result = {
        "person": task.person,
        "present_formats": sorted(present_formats),
        "required_formats": sorted(required_formats),
        "missing_formats": sorted(missing_formats),
        "outputs_pulled": sum(1 for item in output_records if item.get("status") == "pulled"),
        "worklogs_pulled": sum(1 for item in worklog_records if item.get("status") == "pulled"),
        "summaries_pulled": sum(1 for item in summary_records if item.get("status") == "pulled"),
        "dialog_rounds": 1,
        "pushes": 1,
        "dialog_verdict": dialog_verdict,
        "session_id": session_id or None,
        "warnings": runner_warnings,
    }
    if interrupted:
        mark_case_interrupted(case_id, str(run_dir), case_dir=str(task_dir))
        raise KeyboardInterrupt
    if failure:
        failure_kind = "timeout" if timed_out else (
            "agent-error" if agent_error else (
            "missing-deliverables" if missing_formats else "execution-error"
            )
        )
        _mark_weekly_failure(
            case_id, run_dir, task_dir, failure, failure_kind=failure_kind
        )
        print(f"[{task.task_id}] FAILED: {failure}", file=sys.stderr)
        return False
    mark_case_completed(
        case_id, str(run_dir), result=result, case_dir=str(task_dir)
    )
    print(
        f"[{task.task_id}] 完成: outputs={result['outputs_pulled']} "
        f"worklogs={result['worklogs_pulled']} summaries={result['summaries_pulled']}"
    )
    return True


def _task_handoff_entry(task: WeeklyTask, config: dict[str, Any]) -> dict[str, Any]:
    case_id = _task_case_id(task.task_id)
    task_dir = _task_directory(config, task.task_id)
    marker_names = (
        ("interrupted.json", "interrupted"),
        ("failed.json", "failed"),
        ("completed.json", "complete"),
    )
    outcome = "not-run"
    marker_path: Path | None = None
    for marker_name, marker_outcome in marker_names:
        candidate = task_dir / marker_name
        if candidate.is_file():
            outcome = marker_outcome
            marker_path = candidate
            break
    trace_path = task_dir / f"{case_id}.jsonl"
    metadata_path = task.metadata_path.resolve()
    outputs_path = (task_dir / "outputs").resolve()
    runner_task_dir = task_dir.resolve()
    return {
        "taskId": task.task_id,
        "person": task.person,
        "executionOutcome": outcome,
        "metadata": str(metadata_path),
        "trace": str(trace_path.resolve()) if trace_path.is_file() else None,
        "outputs": str(outputs_path),
        "marker": str(marker_path.resolve()) if marker_path else None,
        "judgeInputs": {
            "metadata": str(metadata_path),
            "data": None,
            "outputs": str(outputs_path),
            "runnerTaskDir": str(runner_task_dir),
        },
    }


def _handoff_inputs_ready(entries: Iterable[dict[str, Any]]) -> bool:
    terminal = {"complete", "failed", "interrupted"}
    for entry in entries:
        if entry.get("executionOutcome") not in terminal:
            return False
        if entry.get("executionOutcome") != "complete":
            continue
        judge_inputs = entry.get("judgeInputs")
        if not isinstance(judge_inputs, dict):
            return False
        required_paths = {
            "metadata": "file",
            "outputs": "dir",
            "runnerTaskDir": "dir",
        }
        for name, kind in required_paths.items():
            value = judge_inputs.get(name)
            if not isinstance(value, str) or not value:
                return False
            path = Path(value)
            if kind == "file" and not path.is_file():
                return False
            if kind == "dir" and not path.is_dir():
                return False
        trace = entry.get("trace")
        marker = entry.get("marker")
        if not isinstance(trace, str) or not Path(trace).is_file():
            return False
        if not isinstance(marker, str) or not Path(marker).is_file():
            return False
    return True


def write_weekly_runner_handoff(
    tasks: list[WeeklyTask], config: dict[str, Any], *, run_date: str, runner_finished: bool
) -> Path:
    output_root: Path = config["output_root"]
    handoff_path = output_root / "weekly_runner_batch.json"
    entries = [_task_handoff_entry(task, config) for task in tasks]
    effective_runner_finished = runner_finished and _handoff_inputs_ready(entries)
    payload = {
        "version": 1,
        "adapter": "weekly-report",
        "runId": run_date,
        "runnerFinished": effective_runner_finished,
        "writtenAt": datetime.now().isoformat(timespec="seconds"),
        "roots": {
            "metadata": str(Path(config["metadata_root"]).resolve()),
            "deliverables": str(Path(config["deliverables_root"]).resolve()),
            "logs": str(output_root.resolve()),
            "taskArtifacts": (
                str(Path(config["task_artifacts_root"]).resolve())
                if config.get("task_artifacts_root") is not None
                else None
            ),
        },
        "taskIds": [task.task_id for task in tasks],
        "tasks": entries,
    }
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return handoff_path


def run_person(person: str, tasks: list[WeeklyTask], config: dict[str, Any], *,
               target: str | None, verbose: bool, dry_run: bool, rerun: bool,
               stop_on_error: bool, skip_push: bool,
               skip_clear: bool, skip_initial_clear: bool,
               clear_on_interrupt: bool) -> tuple[int, int, bool, bool]:
    pending = tasks if rerun else [
        task for task in tasks
        if not is_case_completed(
            _task_case_id(task.task_id),
            str(_task_directory(config, task.task_id).parent),
            case_dir=str(_task_directory(config, task.task_id)),
        )
    ]
    if not pending:
        print(f"[{person}] 任务均已完成，跳过该人员的数据推送与清理")
        return 0, 0, False, False

    print(f"\n{'#' * 76}\n人员: {person}，本轮任务 {len(pending)} 个\n{'#' * 76}")
    interrupted = False
    success_count = 0
    fail_count = 0
    lifecycle_error: str | None = None
    if skip_push or skip_clear or skip_initial_clear or clear_on_interrupt:
        raise ValueError(
            "note 数据脚本将清空和推送作为一个原子准备步骤；旧的 skip-clear/skip-push/"
            "skip-initial-clear/clear-on-interrupt 选项不再支持"
        )
    try:
        for index, task in enumerate(pending, 1):
            print(f"[{person}] 任务进度 {index}/{len(pending)}")
            ok = False
            for attempt in range(1, 3):
                if attempt > 1 and not dry_run:
                    _archive_previous_attempt(_task_directory(config, task.task_id))
                try:
                    _call_prepare_task_data(task, config, dry_run=dry_run)
                except Exception as exc:
                    error = f"note 数据清空+推送失败: {exc}"
                    if not dry_run:
                        _mark_weekly_failure(
                            _task_case_id(task.task_id),
                            _task_directory(config, task.task_id).parent,
                            _task_directory(config, task.task_id),
                            error,
                            failure_kind="data-preparation",
                        )
                    print(f"[{task.task_id}] FAILED: {error}", file=sys.stderr)
                    break
                ok = run_weekly_task(
                    task,
                    config,
                    target=target,
                    verbose=verbose,
                    dry_run=dry_run,
                    rerun=rerun or attempt > 1,
                )
                if ok:
                    break
                failure_kind = _task_failure_kind(task, config)
                if attempt == 1 and failure_kind in {"timeout", "agent-error"}:
                    label = "超时" if failure_kind == "timeout" else "stop_reason=error"
                    print(f"[{task.task_id}] 首次 Runner {label}，完整重跑一次")
                    continue
                break
            if ok:
                success_count += 1
            else:
                fail_count += 1
                if stop_on_error:
                    break
            if index < len(pending) and float(config["task_interval"]) > 0 and not dry_run:
                time.sleep(float(config["task_interval"]))
    except KeyboardInterrupt:
        interrupted = True
        lifecycle_error = "手动中断"
    except Exception as exc:
        lifecycle_error = str(exc)
        print(f"[{person}] 人员流程失败: {exc}", file=sys.stderr)

    if interrupted:
        raise KeyboardInterrupt
    return (
        success_count,
        fail_count + (1 if lifecycle_error else 0),
        bool(lifecycle_error),
        False,
    )


def _preflight_hdc(target: str | None) -> None:
    hdc_path()
    output = run_hdc(["list", "targets"], timeout=15, verbose=False)
    targets = [
        line.strip() for line in output.splitlines()
        if line.strip() and "empty" not in line.lower()
    ]
    if not targets:
        raise RuntimeError("未检测到 HDC 设备")
    if target and not any(target in line for line in targets):
        raise RuntimeError(f"指定 HDC 设备不在线: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="周报生成任务批跑器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--person", action="append", help="仅执行指定人员，可重复")
    parser.add_argument("--task", action="append", help="仅执行指定 task ID，可重复")
    parser.add_argument("--device", default=None, help="HDC 目标设备 ID")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true", help="列出发现的人员与任务后退出")
    parser.add_argument("--rerun", action="store_true", help="忽略 completed.json 重新执行")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument("--skip-clear", action="store_true")
    parser.add_argument("--skip-initial-clear", action="store_true")
    parser.add_argument("--clear-on-interrupt", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = load_weekly_config(config_path)
    if args.device:
        config["hdc_target"] = args.device
    target = config.get("hdc_target")
    tasks = discover_tasks(config["metadata_root"])
    if not tasks:
        print(f"未发现 metadata: {config['metadata_root']}", file=sys.stderr)
        return 1

    all_ids = {task.task_id for task in tasks}
    if args.task:
        unknown = set(args.task) - all_ids
        if unknown:
            print(f"未知 task ID: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        tasks = [task for task in tasks if task.task_id in set(args.task)]
    if args.person:
        tasks = [task for task in tasks if task.person in set(args.person)]
    grouped = _group_by_person(tasks)
    if not grouped:
        print("筛选后没有任务", file=sys.stderr)
        return 1

    if args.list:
        for person, person_tasks in grouped:
            print(f"{person}: " + ", ".join(f"{task.task_id}={task.metadata['task']}" for task in person_tasks))
        return 0

    output_root: Path = config["output_root"]
    output_root.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        _preflight_hdc(target)

    total_success = 0
    total_failed = 0
    stopped_before_lifecycle = False
    try:
        for person_index, (person, person_tasks) in enumerate(grouped):
            has_pending = args.rerun or any(
                not is_case_completed(
                    _task_case_id(task.task_id),
                    str(_task_directory(config, task.task_id).parent),
                    case_dir=str(_task_directory(config, task.task_id)),
                )
                for task in person_tasks
            )
            if not has_pending:
                print(f"[{person}] 任务均已完成，跳过该人员的数据推送与清理")
                continue
            if not args.dry_run and not stopped_before_lifecycle:
                print("[batch] 首次人员数据操作前停止小艺一次")
                force_stop(target=target, verbose=args.verbose)
                stopped_before_lifecycle = True

            success, failed, lifecycle_failed, _cleanup_succeeded = run_person(
                person,
                person_tasks,
                config,
                target=target,
                verbose=args.verbose,
                dry_run=args.dry_run,
                rerun=args.rerun,
                stop_on_error=args.stop_on_error,
                skip_push=args.skip_push,
                skip_clear=args.skip_clear,
                skip_initial_clear=args.skip_initial_clear,
                clear_on_interrupt=args.clear_on_interrupt,
            )
            total_success += success
            total_failed += failed
            if args.stop_on_error and (failed or lifecycle_failed):
                break
            if person_index < len(grouped) - 1 and float(config["person_interval"]) > 0 and not args.dry_run:
                time.sleep(float(config["person_interval"]))
    except KeyboardInterrupt:
        if not args.dry_run:
            handoff_path = write_weekly_runner_handoff(
                tasks, config, run_date=args.date, runner_finished=False
            )
            print(f"未完成 handoff: {handoff_path}", file=sys.stderr)
        print("\n批跑被手动中断；默认保留当前人员设备数据，便于排查。", file=sys.stderr)
        return 130
    if not args.dry_run:
        handoff_path = write_weekly_runner_handoff(
            tasks, config, run_date=args.date, runner_finished=True
        )
        print(f"Runner handoff: {handoff_path}")
    print(f"\n批跑结束: 成功 {total_success}，失败 {total_failed}")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
