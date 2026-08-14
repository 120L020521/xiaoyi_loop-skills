#!/usr/bin/env python3
"""
hdc_client.py - HDC 底层操作封装

提供与远程设备通信的底层接口。
"""

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

HDC_BIN_NAME = "hdc.exe" if os.name == "nt" else "hdc"
SHELL_TIMEOUT = 30
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


# ---------------------------------------------------------------------------
# HDC 命令日志记录器
# ---------------------------------------------------------------------------
# 全局单例。run_test.py 通过 set_hdc_logger(...) 启用；
# 不调用时为 None，run_hdc 完全不落盘，保持原有行为。
_hdc_logger: "HdcCommandLogger | None" = None


class HdcCommandLogger:
    """把每条 hdc 命令（命令行、退出码、耗时、stdout 摘要）追加写到日志文件。

    线程不安全；当前 batch runner 为单线程顺序执行，可直接使用。
    """

    _STDOUT_SUMMARY_LIMIT = 500

    def __init__(self, log_path: str):
        self.log_path = log_path
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        # 追加写一个文件头，方便区分多次运行共用同一文件的情况
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(
                f"\n# ==== hdc command log opened at {datetime.now().isoformat(timespec='seconds')} ====\n"
            )

    def log(
        self,
        *,
        cmd: list[str],
        returncode: int | None,
        elapsed: float,
        stdout: str = "",
        stderr: str = "",
        error: str | None = None,
    ) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        cmd_text = " ".join(cmd)
        stdout_summary = (stdout or "").strip()
        if len(stdout_summary) > self._STDOUT_SUMMARY_LIMIT:
            stdout_summary = stdout_summary[: self._STDOUT_SUMMARY_LIMIT] + f"...(+{len(stdout) - self._STDOUT_SUMMARY_LIMIT} bytes)"
        stderr_summary = (stderr or "").strip()
        if len(stderr_summary) > self._STDOUT_SUMMARY_LIMIT:
            stderr_summary = stderr_summary[: self._STDOUT_SUMMARY_LIMIT] + f"...(+{len(stderr) - self._STDOUT_SUMMARY_LIMIT} bytes)"

        lines = [
            f"[{ts}] CMD: {cmd_text}",
            f"          rc={'<timeout>' if returncode is None else returncode} elapsed={elapsed:.3f}s",
        ]
        if error:
            lines.append(f"          ERROR: {error}")
        if stdout_summary:
            lines.append(f"          OUT: {stdout_summary}")
        if stderr_summary:
            lines.append(f"          ERR: {stderr_summary}")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass


def set_hdc_logger(logger: "HdcCommandLogger | None") -> None:
    """安装/卸载全局 hdc 命令日志记录器。"""
    global _hdc_logger
    _hdc_logger = logger


def hdc_path() -> str:
    path = shutil.which(HDC_BIN_NAME) or shutil.which("hdc")
    if not path:
        raise HdcError("没有在 PATH 中找到 hdc/hdc.exe，请先确认 hdc 已安装并加入 PATH。")
    return path


def run_hdc(args: list[str], *, timeout: int, verbose: bool = False) -> str:
    cmd = [hdc_path(), *args]
    if verbose:
        print("[hdc]", " ".join(cmd))
    started = time.monotonic()
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
        elapsed = time.monotonic() - started
        if _hdc_logger:
            _hdc_logger.log(
                cmd=cmd,
                returncode=None,
                elapsed=elapsed,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                error=f"timeout after {timeout}s",
            )
        raise HdcError(f"hdc 命令超时：{' '.join(cmd)}", stdout=exc.stdout or "", stderr=exc.stderr or "") from exc

    elapsed = time.monotonic() - started
    if _hdc_logger:
        _hdc_logger.log(
            cmd=cmd,
            returncode=proc.returncode,
            elapsed=elapsed,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

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


def list_users(*, target: str | None, verbose: bool = False) -> list[str]:
    workspace_base = "/data/app/el2/100/base/com.huawei.hmos.vassistant/files/taichu_data"
    out = remote_shell(f"ls -1 {shell_quote(workspace_base)} 2>/dev/null", target=target, verbose=verbose)
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
    workspace_base = "/data/app/el2/100/base/com.huawei.hmos.vassistant/files/taichu_data"
    interaction_subpath = "interaction"
    user_ids = [user_id] if user_id else list_users(target=target, verbose=verbose)
    logs: list[RemoteLog] = []
    suffix = ".jsonl"

    for uid in user_ids:
        interaction_dir = f"{workspace_base}/{uid}/{interaction_subpath}"
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
