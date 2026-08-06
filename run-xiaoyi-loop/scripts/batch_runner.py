#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在工作机读取 WorkspaceBench metadata.json，批量向鸿蒙 PC 小艺发送 task 并拉取日志与 output。

示例：
  python batch_runner.py 117 127 --tasks-root path/to/tasks
  python batch_runner.py path/to/tasks/127/metadata.json
  python batch_runner.py 1-388 --tasks-root path/to/tasks

完成判定：
  新 session 中出现 agent_role == "main" 且 payload.assistant.stop_reason == "stop"。
"""

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
from pathlib import Path
from typing import Iterable


HDC_BIN_NAME = "hdc.exe" if os.name == "nt" else "hdc"
VASSISTANT_BUNDLE = "com.huawei.hmos.vassistant"
VASSISTANT_ABILITY = "PCAgentTaskAbility"
WORKSPACE_BASE = "/data/app/el2/100/base/com.huawei.hmos.vassistant/files/taichu_data"
INTERACTION_SUBPATH = "interaction"
WORKSPACE_QUERY_SUFFIX = (
    "Please search for the required files on the Desktop, in Downloads, and in Documents; other work materials are in Documents/ResearchWorkspace."
    "If no output directory is specified, save to Documents. Do not terminate until all file operations are completed."
)

SHELL_TIMEOUT = 30
PULL_TIMEOUT = 180
WAIT_STATUS_INTERVAL_SECONDS = 300.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class HdcError(RuntimeError):
    def __init__(self, message: str, *, stdout: str = "", stderr: str = "", returncode: int | None = None):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@dataclass(frozen=True)
class RemoteLog:
    user_id: str
    name: str
    path: str
    size: int
    mtime: int


@dataclass(frozen=True)
class TaskSpec:
    task_id: int
    metadata_path: Path
    task_text: str

class TaskTimeoutError(TimeoutError):
    def __init__(self, message: str, *, active_log: RemoteLog | None):
        super().__init__(message)
        self.active_log = active_log


def hdc_path() -> str:
    configured = os.environ.get("XIAOYI_HDC", "").strip()
    path: str | None = None
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute() or any(separator in configured for separator in ("/", "\\")):
            if candidate.is_file():
                path = str(candidate.resolve())
        else:
            path = shutil.which(configured)
    path = path or shutil.which(HDC_BIN_NAME) or shutil.which("hdc")
    if not path:
        raise HdcError(
            "没有找到 hdc/hdc.exe；请在工作目录 .xiaoyi-loop/local.toml 的 device.hdc "
            "填写可执行文件路径，或将 hdc 加入 PATH。"
        )
    return path


def assistant_bundle() -> str:
    return os.environ.get("XIAOYI_BUNDLE_NAME", "").strip() or VASSISTANT_BUNDLE


def assistant_ability() -> str:
    return os.environ.get("XIAOYI_ABILITY_NAME", "").strip() or VASSISTANT_ABILITY


def workspace_base() -> str:
    return os.environ.get("XIAOYI_REMOTE_WORKSPACE_BASE", "").strip() or WORKSPACE_BASE


def run_hdc(args: list[str], *, timeout: int, verbose: bool = False) -> str:
    cmd = [hdc_path(), *args]
    if verbose:
        print("[hdc]", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise HdcError(f"hdc 命令超时：{' '.join(cmd)}", stdout=exc.stdout or "", stderr=exc.stderr or "") from exc

    if proc.returncode != 0:
        raise HdcError(
            f"hdc 命令失败，退出码 {proc.returncode}：{' '.join(cmd)}",
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )
    return proc.stdout


def target_args(target: str | None) -> list[str]:
    return ["-t", target] if target else []


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def remote_shell(command: str, *, target: str | None, timeout: int = SHELL_TIMEOUT, verbose: bool = False) -> str:
    return run_hdc([*target_args(target), "shell", command], timeout=timeout, verbose=verbose)


def parse_tasks(parts: Iterable[str], *, min_task: int, max_task: int) -> list[int]:
    tasks: list[int] = []
    seen: set[int] = set()

    for part in parts:
        for token in re.split(r"[,\s]+", part.strip()):
            if not token:
                continue
            m = re.fullmatch(r"(\d+)(?:-|\.\.)(\d+)", token)
            if m:
                start, end = int(m.group(1)), int(m.group(2))
                step = 1 if start <= end else -1
                numbers = range(start, end + step, step)
            elif token.isdigit():
                numbers = [int(token)]
            else:
                raise ValueError(f"无法识别任务编号：{token!r}，可用格式示例：1-10,13,20")

            for number in numbers:
                if number < min_task or number > max_task:
                    raise ValueError(f"任务编号越界：{number}，允许范围是 {min_task}-{max_task}")
                if number not in seen:
                    tasks.append(number)
                    seen.add(number)

    if not tasks:
        raise ValueError("没有解析到任何任务编号。")
    return tasks


def _looks_like_metadata_path(value: str) -> bool:
    return value.lower().endswith(".json") or "/" in value or "\\" in value or bool(re.match(r"^[A-Za-z]:", value))


def load_task_spec(metadata_path: Path, *, min_task: int, max_task: int) -> TaskSpec:
    path = metadata_path.expanduser().resolve()
    if path.is_dir():
        path = path / "metadata.json"
    if not path.is_file():
        raise ValueError(f"metadata.json 不存在：{path}")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"无法读取 metadata.json：{path}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"metadata.json 不是有效的 UTF-8 文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata.json 格式错误：{path}：{exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"metadata.json 顶层必须是对象：{path}")

    task_text = metadata.get("task")
    if not isinstance(task_text, str) or not task_text.strip():
        raise ValueError(f"metadata.json 缺少非空 task 字段：{path}")

    raw_id = metadata.get("absolute_id")
    if isinstance(raw_id, bool):
        raise ValueError(f"metadata.json 的 absolute_id 不是整数：{path}")
    if isinstance(raw_id, str) and raw_id.isdigit():
        raw_id = int(raw_id)
    if raw_id is not None and not isinstance(raw_id, int):
        raise ValueError(f"metadata.json 的 absolute_id 不是整数：{path}")
    parent_id = int(path.parent.name) if path.parent.name.isdigit() else None
    if raw_id is not None and parent_id is not None and raw_id != parent_id:
        raise ValueError(f"absolute_id={raw_id} 与父目录 {parent_id} 不一致：{path}")
    task_id = raw_id if raw_id is not None else parent_id
    if task_id is None:
        raise ValueError(f"无法从 absolute_id 或父目录确定任务 ID：{path}")
    if task_id < min_task or task_id > max_task:
        raise ValueError(f"任务编号越界：{task_id}，允许范围是 {min_task}-{max_task}")
    return TaskSpec(task_id=task_id, metadata_path=path, task_text=task_text.strip())


def collect_task_specs(
    parts: Iterable[str],
    *,
    tasks_root: str | None,
    min_task: int,
    max_task: int,
) -> list[TaskSpec]:
    root: Path | None = None
    if tasks_root:
        root = Path(tasks_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"tasks root 不存在或不是目录：{root}")

    specs: list[TaskSpec] = []
    seen_ids: set[int] = set()
    for raw in parts:
        candidate = Path(raw).expanduser()
        if candidate.exists() or _looks_like_metadata_path(raw):
            candidates = [candidate]
        else:
            if root is None:
                raise ValueError(f"使用任务编号 {raw!r} 时必须提供 --tasks-root。")
            task_ids = parse_tasks([raw], min_task=min_task, max_task=max_task)
            candidates = [root / str(task_id) / "metadata.json" for task_id in task_ids]

        for metadata_path in candidates:
            spec = load_task_spec(metadata_path, min_task=min_task, max_task=max_task)
            if spec.task_id in seen_ids:
                raise ValueError(f"重复任务 ID：{spec.task_id}")
            seen_ids.add(spec.task_id)
            specs.append(spec)

    if not specs:
        raise ValueError("没有解析到任何 metadata.json。")
    return specs


def today_id() -> str:
    return datetime.now().strftime("%Y%m%d")


def list_users(*, target: str | None, verbose: bool = False) -> list[str]:
    out = remote_shell(
        f"ls -1 {shell_quote(workspace_base())} 2>/dev/null",
        target=target,
        verbose=verbose,
    )
    users = []
    for line in out.splitlines():
        name = line.strip()
        if name and "/" not in name and name not in {".", ".."}:
            users.append(name)
    return sorted(set(users))


def stat_remote(path: str, *, target: str | None, verbose: bool = False) -> tuple[int, int]:
    quoted = shell_quote(path)
    commands = [
        f"stat -c '%Y %s' {quoted} 2>/dev/null",
        f"stat -f '%m %z' {quoted} 2>/dev/null",
    ]
    for command in commands:
        try:
            out = remote_shell(command, target=target, verbose=verbose).strip()
        except HdcError:
            continue
        m = re.search(r"(\d+)\s+(\d+)", out)
        if m:
            return int(m.group(2)), int(m.group(1))
    return 0, 0


def list_remote_logs(
    *,
    target: str | None,
    user_id: str | None,
    date_id: str,
    verbose: bool = False,
) -> list[RemoteLog]:
    user_ids = [user_id] if user_id else list_users(target=target, verbose=verbose)
    logs: list[RemoteLog] = []
    suffix = ".jsonl"

    for uid in user_ids:
        interaction_dir = f"{workspace_base()}/{uid}/{INTERACTION_SUBPATH}"
        # Enumerate all JSONL names; the task-start snapshot excludes historical logs.
        pattern = f"{shell_quote(interaction_dir)}/*{suffix}"
        command = f"stat -c '%Y %s %n' {pattern} 2>/dev/null; echo __END__"
        try:
            out = remote_shell(command, target=target, verbose=verbose)
        except HdcError:
            continue
        if "__END__" not in out:
            continue
        payload = out.split("__END__", 1)[0]
        for line in payload.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) != 3:
                continue
            mtime_text, size_text, path = parts
            try:
                mtime = int(mtime_text)
                size = int(size_text)
            except ValueError:
                continue
            name = path.rsplit("/", 1)[-1]
            if not name.endswith(suffix) or "/" in name or name in {".", ".."}:
                continue
            logs.append(RemoteLog(user_id=uid, name=name, path=path, size=size, mtime=mtime))

    return logs


def snapshot(logs: Iterable[RemoteLog]) -> dict[str, tuple[int, int]]:
    return {log.path: (log.size, log.mtime) for log in logs}


def changed_logs(before: dict[str, tuple[int, int]], current: Iterable[RemoteLog]) -> list[RemoteLog]:
    changed = []
    for log in current:
        old = before.get(log.path)
        if old is None or log.size > old[0] or log.mtime > old[1]:
            changed.append(log)
    return sorted(changed, key=lambda item: (item.mtime, item.size, item.path), reverse=True)


def has_stop_reason_stop(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict) or event.get("agent_role") != "main":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        assistant = payload.get("assistant")
        if isinstance(assistant, dict) and assistant.get("stop_reason") == "stop":
            return True
    return False


def read_remote_stop_candidates(
    log: RemoteLog,
    *,
    target: str | None,
    lines: int,
    start_byte: int,
    verbose: bool = False,
) -> str:
    """Filter current-task stop candidates remotely without transferring large JSONL entries."""
    quoted = shell_quote(log.path)
    start_byte = max(1, int(start_byte))
    source = f"tail -c +{start_byte} {quoted} 2>/dev/null"
    recent_model_outputs = max(1, int(lines))
    event_pattern = shell_quote(r'"event"[[:space:]]*:[[:space:]]*"model_output"')
    role_pattern = shell_quote(r'"agent_role"[[:space:]]*:[[:space:]]*"main"')
    stop_pattern = shell_quote(r'"stop_reason"[[:space:]]*:[[:space:]]*"stop"')
    command = (
        f"{source} | grep -E {event_pattern} "
        f"| tail -n {recent_model_outputs} "
        f"| grep -E {role_pattern} "
        f"| grep -E {stop_pattern} "
        f"| tail -n 1"
    )
    return remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)


def remote_log_created_time(
    log: RemoteLog,
    *,
    target: str | None,
    verbose: bool = False,
) -> float:
    """Read the first JSONL event timestamp; mtime is only a fallback."""
    quoted = shell_quote(log.path)
    try:
        first_line = remote_shell(
            f"head -n 1 {quoted} 2>/dev/null",
            target=target,
            timeout=SHELL_TIMEOUT,
            verbose=verbose,
        ).strip()
        event = json.loads(first_line)
        timestamp = event.get("timestamp") if isinstance(event, dict) else None
        if isinstance(timestamp, str):
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except (HdcError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return float(log.mtime)


def select_main_new_log(
    logs: Iterable[RemoteLog],
    *,
    before: dict[str, tuple[int, int]],
    target: str | None,
    verbose: bool = False,
) -> RemoteLog | None:
    """Prefer the longest new filename; use creation time only as a tie-breaker."""
    new_logs = [log for log in logs if log.path not in before]
    if not new_logs:
        return None
    return min(
        new_logs,
        key=lambda log: (
            -len(log.name),
            remote_log_created_time(log, target=target, verbose=verbose),
            log.path,
        ),
    )


def start_task(task: TaskSpec, *, target: str | None, verbose: bool = False) -> str:
    query = f"{task.task_text}\n{WORKSPACE_QUERY_SUFFIX}"
    command = (
        f"aa start -b {assistant_bundle()} "
        f"-a {assistant_ability()} "
        f"--ps launch_type pc_agent_task_start "
        f"--ps query {shell_quote(query)}"
    )
    out = remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
    if out.strip():
        print(out.strip())
    return query


def force_stop(*, target: str | None, verbose: bool = False) -> None:
    remote_shell(
        f"aa force-stop {assistant_bundle()}",
        target=target,
        timeout=SHELL_TIMEOUT,
        verbose=verbose,
    )


_FILES_BLOCK_RE = re.compile(r"```files\s*(\[.*?\])\s*```", re.IGNORECASE | re.DOTALL)
_ACCESSIBLE_OUTPUT_ROOTS = (
    "/storage/media/100/local/files/Docs/Download",
    "/storage/media/100/local/files/Docs/Desktop",
    "/storage/media/100/local/files/Docs/Documents",
)
_VIRTUAL_OUTPUT_PATH_MAPPINGS = (
    ("/storage/User/currentUser/Desktop", "/storage/media/100/local/files/Docs/Desktop"),
    ("/storage/Users/currentUser/Desktop", "/storage/media/100/local/files/Docs/Desktop"),
    ("/storage/User/currentUser/Download", "/storage/media/100/local/files/Docs/Download"),
    ("/storage/Users/currentUser/Download", "/storage/media/100/local/files/Docs/Download"),
    ("/storage/User/currentUser/Documents", "/storage/media/100/local/files/Docs/Document"),
    ("/storage/Users/currentUser/Documents", "/storage/media/100/local/files/Docs/Documents"),
)


def _safe_relative_remote_path(relative: str) -> bool:
    parts = relative.split("/")
    return bool(relative) and all(part not in {"", ".", ".."} for part in parts)


def map_output_path(declared_path: str) -> str | None:
    """把小艺返回的用户可见路径映射到 HDC 可访问路径。"""
    normalized = declared_path.strip().replace("\\", "/").rstrip("/")
    if not normalized.startswith("/"):
        return None

    for virtual_root, accessible_root in _VIRTUAL_OUTPUT_PATH_MAPPINGS:
        prefix = virtual_root + "/"
        if normalized.startswith(prefix):
            relative = normalized[len(prefix):]
            if not _safe_relative_remote_path(relative):
                return None
            mapped = f"{accessible_root}/{relative}"
            if any(mapped == root or mapped.startswith(root + "/") for root in _ACCESSIBLE_OUTPUT_ROOTS):
                return mapped
            return None

    for accessible_root in _ACCESSIBLE_OUTPUT_ROOTS:
        prefix = accessible_root + "/"
        if normalized.startswith(prefix):
            relative = normalized[len(prefix):]
            return normalized if _safe_relative_remote_path(relative) else None
    return None


def _safe_local_output_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name.strip()).strip(" .")
    return cleaned or "output"


def extract_declared_outputs(local_log: Path) -> list[dict[str, str]]:
    """只读取 main agent assistant 声明的 files，不扫描其他日志字段。"""
    outputs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_entries(entries: object) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            declared_path = entry.get("filePath") or entry.get("path")
            if not isinstance(declared_path, str):
                continue
            declared_path = declared_path.strip().replace("\\", "/")
            if not declared_path or declared_path in seen:
                continue
            seen.add(declared_path)
            declared_name = entry.get("fileName")
            if not isinstance(declared_name, str) or not declared_name.strip():
                declared_name = declared_path.rstrip("/").rsplit("/", 1)[-1]
            outputs.append({
                "declared_path": declared_path,
                "declared_name": declared_name,
                "local_name": _safe_local_output_name(declared_name),
            })

    for line in local_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("agent_role") != "main":
            continue
        payload = event.get("payload")
        assistant = payload.get("assistant") if isinstance(payload, dict) else None
        if not isinstance(assistant, dict):
            continue
        add_entries(assistant.get("files"))
        content = assistant.get("content")
        content_items = content if isinstance(content, list) else [content]
        for item in content_items:
            if isinstance(item, dict):
                add_entries(item.get("files"))
                text_value = item.get("text")
            else:
                text_value = item
            if not isinstance(text_value, str):
                continue
            for match in _FILES_BLOCK_RE.finditer(text_value):
                try:
                    add_entries(json.loads(match.group(1)))
                except json.JSONDecodeError:
                    continue
    return outputs


def pull_declared_outputs(
    local_log: Path,
    *,
    task_dir: Path,
    target: str | None,
    verbose: bool = False,
) -> list[dict[str, object]]:
    declared = extract_declared_outputs(local_log)
    if not declared:
        return []

    outputs_dir = task_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    used_names: set[str] = set()
    for item in declared:
        mapped_path = map_output_path(item["declared_path"])
        record: dict[str, object] = {
            "declared_path": item["declared_path"],
            "mapped_path": mapped_path,
            "declared_name": item["declared_name"],
        }
        if mapped_path is None:
            record["status"] = "skipped_unmapped_path"
            record["error"] = "路径不在允许的三组映射目录中"
            results.append(record)
            print(f"[{task_dir.name}] 跳过未映射 output：{item['declared_path']}")
            continue

        base_name = item["local_name"]
        local_name = base_name
        suffix = 2
        while local_name.casefold() in used_names:
            stem, extension = os.path.splitext(base_name)
            local_name = f"{stem}_{suffix}{extension}"
            suffix += 1
        used_names.add(local_name.casefold())
        local_path = outputs_dir / local_name
        record["local_path"] = str(local_path.relative_to(task_dir))
        try:
            run_hdc(
                [*target_args(target), "file", "recv", mapped_path, str(local_path)],
                timeout=PULL_TIMEOUT,
                verbose=verbose,
            )
            if not local_path.exists():
                raise HdcError("hdc 返回成功，但本地未发现拉取的 output")
            record["status"] = "pulled"
            print(f"[{task_dir.name}] output 已拉取：{local_path}")
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            print(f"[{task_dir.name}] output 拉取失败：{mapped_path}：{exc}", file=sys.stderr)
        results.append(record)

    manifest = {
        "source": "main_agent_jsonl_files_only",
        "allowed_remote_roots": list(_ACCESSIBLE_OUTPUT_ROOTS),
        "outputs": results,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (task_dir / "outputs_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results


def pull_log(
    log: RemoteLog,
    *,
    task: TaskSpec,
    query_characters: int | None,
    out_dir: Path,
    target: str | None,
    status: str | None = None,
    failure_reason: str | None = None,
    verbose: bool = False,
) -> Path:
    task_name = f"task{task.task_id}"
    task_dir = out_dir / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    local_log = task_dir / f"{task_name}.jsonl"
    run_hdc([*target_args(target), "file", "recv", log.path, str(local_log)], timeout=PULL_TIMEOUT, verbose=verbose)
    outputs = pull_declared_outputs(
        local_log,
        task_dir=task_dir,
        target=target,
        verbose=verbose,
    )
    meta = {
        "task_id": task.task_id,
        "metadata_path": str(task.metadata_path),
        "query_characters": query_characters,
        "status": status or "completed",
        "failure_reason": failure_reason,
        "remote_log": log.path,
        "remote_user_id": log.user_id,
        "remote_log_name": log.name,
        "output_count": sum(1 for output in outputs if output.get("status") == "pulled"),
        "outputs_manifest": "outputs_manifest.json" if outputs else None,
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
    }
    (task_dir / f"{task_name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return local_log

def wait_for_task_done(
    *,
    task_id: int,
    before: dict[str, tuple[int, int]],
    target: str | None,
    user_id: str | None,
    initial_date_id: str,
    dynamic_date: bool,
    poll_seconds: float,
    timeout_seconds: int,
    tail_lines: int,
    verbose: bool = False,
    status_interval_seconds: float = WAIT_STATUS_INTERVAL_SECONDS,
) -> RemoteLog:
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    heartbeat_interval = max(1.0, float(status_interval_seconds))
    next_heartbeat = started_at + heartbeat_interval
    active_log: RemoteLog | None = None

    print(
        f"[task{task_id}] 小艺任务运行中；耗时较长属正常现象。"
        f"每 {heartbeat_interval / 60:g} 分钟报告一次状态，"
        f"最长等待 {timeout_seconds / 60:g} 分钟。",
        flush=True,
    )

    while time.monotonic() < deadline:
        date_id = today_id() if dynamic_date else initial_date_id
        # Polling is deliberately quiet even under --verbose. Printing every HDC command or
        # every empty stop_reason check can exhaust an Agent tool's output buffer on long runs.
        logs = list_remote_logs(
            target=target,
            user_id=user_id,
            date_id=date_id,
            verbose=False,
        )

        if active_log is None:
            active_log = select_main_new_log(
                logs,
                before=before,
                target=target,
                verbose=False,
            )
            if active_log is not None:
                print(
                    f"[task{task_id}] 已发现本轮 main 日志，继续等待完成标记："
                    f"{active_log.path}",
                    flush=True,
                )

        if active_log is not None:
            current = next((log for log in logs if log.path == active_log.path), active_log)
            active_log = current
            text = read_remote_stop_candidates(
                active_log,
                target=target,
                lines=tail_lines,
                start_byte=1,
                verbose=False,
            )
            if has_stop_reason_stop(text):
                return active_log

        now = time.monotonic()
        if now >= next_heartbeat:
            elapsed_minutes = int((now - started_at) // 60)
            log_status = "已发现本轮 main 日志" if active_log is not None else "正在等待本轮 main 日志"
            print(
                f"[task{task_id}] 仍在正常等待 stop_reason=stop，"
                f"已等待 {elapsed_minutes} 分钟；{log_status}。",
                flush=True,
            )
            while next_heartbeat <= now:
                next_heartbeat += heartbeat_interval
        time.sleep(poll_seconds)

    raise TaskTimeoutError(
        f"task{task_id} timed out waiting for stop_reason=stop.",
        active_log=active_log,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从本机 WorkspaceBench metadata.json 提取 task，批量发送给鸿蒙 PC 小艺并拉取日志与 output。"
    )
    parser.add_argument(
        "tasks",
        nargs="+",
        help="metadata.json/任务目录，或配合 --tasks-root 使用的任务编号（支持 1-10、1..10、1-10,13,20）。",
    )
    parser.add_argument("--tasks-root", help="本机 tasks_lite 根目录；使用任务编号时必须提供。")
    parser.add_argument("--target", help="hdc 目标 connect key；不填时使用 hdc 默认目标。")
    parser.add_argument("--user-id", help="taichu_data 下的 user_id；不填时自动扫描所有 user_id。")
    parser.add_argument("--date", dest="date_id", help="日志日期 ID，例如 20260629；不填则启动时取本机当天日期。")
    parser.add_argument("--dynamic-date", action="store_true", help="轮询时动态刷新日期 ID，适合跨天长跑。")
    parser.add_argument("--out", default="xiaoyi_logs", help="日志和 output 保存目录，默认 ./xiaoyi_logs。")
    parser.add_argument("--poll", type=float, default=3.0, help="轮询间隔秒数，默认 3。")
    parser.add_argument("--timeout", type=int, default=1800, help="单个任务最大等待秒数，默认 1800（30 分钟）。")
    parser.add_argument("--settle", type=float, default=1.5, help="检测完成后拉日志前等待秒数，默认 1.5。")
    parser.add_argument("--restart-delay", type=float, default=5.0, help="相邻任务间等待秒数，默认 5。")
    parser.add_argument("--tail-lines", type=int, default=300, help="每次检查日志末尾行数，默认 300。")
    parser.add_argument("--min-task", type=int, default=1, help="最小任务编号，默认 1。")
    parser.add_argument("--max-task", type=int, default=388, help="最大任务编号，默认 388。")
    parser.add_argument("--no-force-stop", action="store_true", help="任务完成后不执行 aa force-stop。")
    parser.add_argument("--continue-on-error", action="store_true", help="兼容选项；现在默认会在单个任务失败后继续。")
    parser.add_argument("--stop-on-error", action="store_true", help="某个任务失败后立即结束整个批次。")
    parser.add_argument("--verbose", action="store_true", help="打印 hdc 命令。")
    args = parser.parse_args()

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

    date_id = args.date_id or today_id()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"任务队列：{[task.task_id for task in tasks]}")
    print(f"日志日期：{date_id}{'（轮询时动态刷新）' if args.dynamic_date else ''}")
    print(f"日志与 output 输出：{out_dir}")
    for task in tasks:
        print(f"  task{task.task_id}: {task.metadata_path}")
    if args.user_id:
        print(f"user_id：{args.user_id}")
    if args.target:
        print(f"hdc target：{args.target}")

    for index, task in enumerate(tasks, start=1):
        task_id = task.task_id
        query_characters: int | None = None
        print(f"\n[{index}/{len(tasks)}] 启动 task{task_id}")
        try:
            current_date_id = today_id() if args.dynamic_date else date_id
            before_logs = list_remote_logs(
                target=args.target,
                user_id=args.user_id,
                date_id=current_date_id,
                verbose=args.verbose,
            )
            before = snapshot(before_logs)

            query_characters = len(start_task(task, target=args.target, verbose=args.verbose))

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
            print(f"[task{task_id}] 完成，日志：{done_log.path}")

            if args.settle > 0:
                time.sleep(args.settle)
            local_log = pull_log(
                done_log,
                task=task,
                query_characters=query_characters,
                out_dir=out_dir,
                target=args.target,
                verbose=args.verbose,
            )
            print(f"[task{task_id}] 日志和 output 已处理：{local_log}")

            if not args.no_force_stop:
                force_stop(target=args.target, verbose=args.verbose)
                print(f"[task{task_id}] 已 force-stop {VASSISTANT_BUNDLE}")

            if index < len(tasks) and args.restart_delay > 0:
                print(f"[task{task_id}] 等待 {args.restart_delay:g} 秒后启动下一任务 ...")
                time.sleep(args.restart_delay)

        except Exception as exc:
            timeout_collected = False
            if isinstance(exc, TaskTimeoutError):
                print(f"[task{task_id}] 达到超时上限：{exc}", file=sys.stderr)
            else:
                print(f"[task{task_id}] 失败：{exc}", file=sys.stderr)
            if isinstance(exc, HdcError):
                if exc.stdout.strip():
                    print("stdout:", exc.stdout.strip(), file=sys.stderr)
                if exc.stderr.strip():
                    print("stderr:", exc.stderr.strip(), file=sys.stderr)

            if isinstance(exc, TaskTimeoutError):
                task_name = f"task{task_id}"
                task_dir = out_dir / task_name
                task_dir.mkdir(parents=True, exist_ok=True)
                if args.settle > 0:
                    time.sleep(args.settle)
                if exc.active_log is not None:
                    try:
                        timeout_log = pull_log(
                            exc.active_log,
                            task=task,
                            query_characters=query_characters,
                            out_dir=out_dir,
                            target=args.target,
                            status="timeout",
                            failure_reason=str(exc),
                            verbose=args.verbose,
                        )
                        timeout_collected = True
                        print(f"[task{task_id}] 超时日志和已声明 output 已处理：{timeout_log}")
                    except Exception as pull_exc:
                        failure_meta = {
                            "task_id": task_id,
                            "metadata_path": str(task.metadata_path),
                            "query_characters": query_characters,
                            "status": "timeout_log_pull_failed",
                            "failure_reason": str(exc),
                            "pull_error": str(pull_exc),
                            "remote_log": exc.active_log.path,
                            "recorded_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        (task_dir / f"{task_name}.meta.json").write_text(
                            json.dumps(failure_meta, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        print(f"[task{task_id}] 超时日志拉取失败：{pull_exc}", file=sys.stderr)
                else:
                    failure_meta = {
                        "task_id": task_id,
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
                    print(f"[task{task_id}] 超时前未发现 session 日志，已保存失败元数据。")

                if not args.no_force_stop:
                    try:
                        force_stop(target=args.target, verbose=args.verbose)
                        print(f"[task{task_id}] 超时处理后已 force-stop {VASSISTANT_BUNDLE}")
                    except Exception as stop_exc:
                        print(
                            f"[task{task_id}] 超时处理后 force-stop 失败：{stop_exc}",
                            file=sys.stderr,
                        )
            elif not args.no_force_stop:
                try:
                    force_stop(target=args.target, verbose=args.verbose)
                    print(f"[task{task_id}] 失败后已尝试 force-stop {VASSISTANT_BUNDLE}")
                except Exception as stop_exc:
                    print(f"[task{task_id}] 失败后 force-stop 也失败：{stop_exc}", file=sys.stderr)

            if args.stop_on_error and not timeout_collected:
                return 1
            if index < len(tasks) and args.restart_delay > 0:
                label = "超时处理" if timeout_collected else "失败"
                print(f"[task{task_id}] {label}后等待 {args.restart_delay:g} 秒再继续 ...")
                time.sleep(args.restart_delay)

    print("\n全部任务处理完毕。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
