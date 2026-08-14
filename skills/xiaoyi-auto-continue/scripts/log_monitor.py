#!/usr/bin/env python3
"""
log_monitor.py - 日志监控和任务完成检测

监控远程 JSONL 日志，检测任务何时完成。
"""

import json
import time
from datetime import datetime
from typing import Iterable

from .hdc_client import (
    HdcError,
    RemoteLog,
    changed_logs,
    list_remote_logs,
    remote_shell,
    shell_quote,
    target_args,
    run_hdc,
)

SHELL_TIMEOUT = 30


def has_stop_reason_stop(text: str) -> bool:
    """检测 content 中是否包含完成关键词"""
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
            content = assistant.get("content", [])
            content_text = ""
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        content_text += item.get("text", "")
            #if "任务完成" in content_text or "完成" in content_text or "搞定" in content_text:
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


def today_id() -> str:
    return datetime.now().strftime("%Y%m%d")


class TaskTimeoutError(TimeoutError):
    def __init__(self, message: str, *, active_log: RemoteLog | None):
        super().__init__(message)
        self.active_log = active_log


def wait_for_task_done(
    *,
    item_id: str,
    before: dict[str, tuple[int, int]],
    target: str | None,
    user_id: str | None,
    initial_date_id: str,
    dynamic_date: bool,
    poll_seconds: float,
    timeout_seconds: int,
    tail_lines: int,
    verbose: bool = False,
) -> RemoteLog:
    deadline = time.monotonic() + timeout_seconds
    active_log: RemoteLog | None = None
    last_wait_print = 0.0  # 上次打印 waiting 消息的时间（每 5 分钟打印一次，避免刷屏）
    verbose_interval = 180  # [hdc] 命令详情打印间隔（秒），避免 poll 时刷屏
    last_verbose_print = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        poll_verbose = verbose and (last_verbose_print == 0 or now - last_verbose_print >= verbose_interval)
        date_id = today_id() if dynamic_date else initial_date_id
        logs = list_remote_logs(target=target, user_id=user_id, date_id=date_id, verbose=poll_verbose)

        if active_log is None:
            # 优先选择新日志
            active_log = select_main_new_log(
                logs,
                before=before,
                target=target,
                verbose=poll_verbose,
            )
            # 如果没有新日志，选择内容已变化的旧日志（小艺可能复用旧session）
            if active_log is None:
                changed = changed_logs(before, logs)
                if changed:
                    active_log = changed[0]
                    print(f"[{item_id}] No new log, monitoring changed log: {active_log.path}")

            if active_log is not None:
                print(f"[{item_id}] Monitoring longest-name main log: {active_log.path}")

        if active_log is not None:
            current = next((log for log in logs if log.path == active_log.path), active_log)
            active_log = current
            text = read_remote_stop_candidates(
                active_log,
                target=target,
                lines=tail_lines,
                start_byte=1,
                verbose=poll_verbose,
            )
            if has_stop_reason_stop(text):
                return active_log

        if poll_verbose:
            last_verbose_print = now
        # 每 5 分钟才打印一次等待消息，避免刷屏
        if now - last_wait_print >= 300:
            print(f"[{item_id}] Waiting for main agent stop_reason=stop ...")
            last_wait_print = now
        time.sleep(poll_seconds)

    raise TaskTimeoutError(
        f"{item_id} timed out waiting for stop_reason=stop.",
        active_log=active_log,
    )
