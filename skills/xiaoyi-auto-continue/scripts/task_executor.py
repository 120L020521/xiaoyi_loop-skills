#!/usr/bin/env python3
"""
task_executor.py - 任务执行器

负责启动任务、读取 prompt、拉动日志和输出文件。
"""

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .hdc_client import (
    HdcError,
    RemoteLog,
    remote_shell,
    run_hdc,
    target_args,
)

HDC_BIN_NAME = "hdc.exe" if sys.platform == "win32" else "hdc"
VASSISTANT_BUNDLE = "com.huawei.hmos.vassistant"
VASSISTANT_ABILITY = "PCAgentTaskAbility"
SHELL_TIMEOUT = 30
PULL_TIMEOUT = 180

_ACCESSIBLE_OUTPUT_ROOTS = (
    "/storage/media/100/local/files/Docs/Download",
    "/storage/media/100/local/files/Docs/Desktop",
    "/storage/media/100/local/files/Docs/Documents",
)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def start_prompt(
    prompt_text: str,
    *,
    target: str | None,
    verbose: bool = False,
    history_session_id: str | None = None,
) -> None:
    """启动小艺任务"""
    command = (
        f"aa start -b {VASSISTANT_BUNDLE} "
        f"-a {VASSISTANT_ABILITY} "
        f"--ps launch_type pc_agent_task_start "
        f"--ps query {shell_quote(prompt_text)}"
    )
    if history_session_id:
        command += f" --ps historySessionId {shell_quote(history_session_id)}"
    out = remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
    if out.strip():
        print(out.strip())

def continue_start_prompt(
    prompt_text: str,
    *,
    target: str | None,
    verbose: bool = False,
    history_session_id: str | None = None,
) -> None:
    """启动小艺任务"""
    command = (
        f"aa start -b {VASSISTANT_BUNDLE} "
        f"-a {VASSISTANT_ABILITY} "
        f"--ps launch_type pc_agent_task_list_history "
        f"--ps query {shell_quote(prompt_text)}"
    )
    if history_session_id:
        command += f" --ps historySessionId {shell_quote(history_session_id)}"
    out = remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
    if out.strip():
        print(out.strip())


def force_stop(*, target: str | None, verbose: bool = False) -> None:
    remote_shell(f"aa force-stop {VASSISTANT_BUNDLE}", target=target, timeout=SHELL_TIMEOUT, verbose=verbose)


def count_stop_events(
    log: "RemoteLog",
    *,
    target: str | None,
    lines: int = 10000,
    verbose: bool = False,
) -> int:
    """统计远程日志中 stop_reason=stop 出现的次数（使用与 wait_for_task_done 相同的管道）"""
    quoted = shell_quote(log.path)
    start_byte = 1
    source = f"tail -c +{start_byte} {quoted} 2>/dev/null"
    recent_lines = max(1, int(lines))
    event_pattern = shell_quote(r'"event"[[:space:]]*:[[:space:]]*"model_output"')
    role_pattern = shell_quote(r'"agent_role"[[:space:]]*:[[:space:]]*"main"')
    stop_pattern = shell_quote(r'"stop_reason"[[:space:]]*:[[:space:]]*"stop"')
    command = (
        f"{source} | grep -E {event_pattern} "
        f"| tail -n {recent_lines} "
        f"| grep -E {role_pattern} "
        f"| grep -E {stop_pattern} "
        f"| wc -l"
    )
    out = remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
    try:
        return int(out.strip())
    except:
        return 0


def read_prompt_text(prompt_file: str) -> str:
    """读取 prompt 文件内容"""
    try:
        with open(prompt_file, 'r', encoding='utf-8-sig') as f:
            query = f.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{prompt_file} 不是有效的 UTF-8 TXT 文件。") from exc
    if not query.strip():
        raise ValueError(f"{prompt_file} 的提示词内容为空。")
    if "\x00" in query:
        raise ValueError(f"{prompt_file} 的提示词包含不支持的空字符。")
    return query


def save_prompt_text(query_text: str, *, case_id: str, run_dir: str, tag: str = "prompt") -> Path:
    """把推送给小艺的 query 文本单独落盘到 case 目录，便于事后回查。

    tag 用于区分首次启动 (prompt) 与续接 (continue)。
    """
    run_path = Path(run_dir) / case_id
    run_path.mkdir(parents=True, exist_ok=True)
    out_file = run_path / f"{case_id}.{tag}.txt"
    out_file.write_text(query_text, encoding="utf-8")
    print(f"[{case_id}] {tag} text saved to: {out_file}")
    return out_file


def pull_log(
    log: RemoteLog,
    *,
    case_id: str,
    run_dir: str,
    target: str | None,
    verbose: bool = False,
) -> Path:
    """拉取远程日志到本地"""
    run_name = case_id
    run_path = Path(run_dir) / run_name
    run_path.mkdir(parents=True, exist_ok=True)
    local_log = run_path / f"{run_name}.jsonl"

    run_hdc([*target_args(target), "file", "recv", log.path, str(local_log)], timeout=PULL_TIMEOUT, verbose=verbose)

    # 合并写 meta.json：先读旧内容，再覆盖本次更新的字段，
    # 保留已有的 dialog_page_id 等字段（pull_log 在 continue 流程里会被
    # 再次调用，整体重写会丢掉 dialog_page_id 导致后续续接失败）
    meta_file = run_path / f"{run_name}.meta.json"
    meta: dict = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta.update({
        "case_id": case_id,
        "remote_log": log.path,
        "remote_user_id": log.user_id,
        "remote_log_name": log.name,
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
    })
    meta_file.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return local_log


def pull_outputs(
    case_id: str,
    run_dir: str,
    target: str | None,
    verbose: bool = False,
) -> list[dict]:
    """拉取三个远程目录，并用本轮干净快照整体替换旧 outputs。

    HDC 在目标目录已经存在时接收同名远程目录，会把目录再次嵌套到
    旧目录中。continue 轮因此不能直接复用 outputs；先拉到同级暂存
    目录，再整体发布，确保 Judge 只看到当前远程最终状态。
    """
    case_output_dir = Path(run_dir) / case_id
    case_output_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir = case_output_dir / "outputs"
    staging_dir = case_output_dir / f".outputs-{uuid4().hex}.tmp"
    staging_dir.mkdir(parents=False, exist_ok=False)

    pulled_files = []
    root_results = []
    snapshot_complete = True

    # 目录名映射：remote root -> local dir name
    root_to_dirname = {
        "/storage/media/100/local/files/Docs/Desktop": "Desktop",
        "/storage/media/100/local/files/Docs/Download": "Download",
        "/storage/media/100/local/files/Docs/Documents": "Documents",
    }

    for remote_root in _ACCESSIBLE_OUTPUT_ROOTS:
        # 获取本地子目录名
        local_subdir = root_to_dirname.get(remote_root, remote_root.rsplit("/", 1)[-1])
        local_subdir_path = staging_dir / local_subdir
        local_subdir_path.mkdir(parents=True, exist_ok=True)

        # 包括隐藏项；空目录仍由上面的本地根目录表示。
        list_cmd = f"ls -1A {shell_quote(remote_root)} 2>/dev/null"
        try:
            out = remote_shell(list_cmd, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
        except HdcError as e:
            snapshot_complete = False
            root_results.append({
                "remote_root": remote_root,
                "local_root": local_subdir,
                "status": "failed",
                "error": str(e),
            })
            if verbose:
                print(f"[{case_id}] Failed to list {remote_root}: {e}")
            continue

        if not out.strip():
            root_results.append({
                "remote_root": remote_root,
                "local_root": local_subdir,
                "status": "pulled",
                "top_level_entries": 0,
            })
            continue

        top_level_entries = 0
        for filename in out.strip().splitlines():
            filename = filename.strip()
            if not filename or filename in {".", ".."}:
                continue
            top_level_entries += 1

            remote_path = f"{remote_root}/{filename}"
            local_path = local_subdir_path / filename

            try:
                if verbose:
                    print(f"[{case_id}] Pulling {remote_path} -> {local_path}")

                run_hdc(
                    [*target_args(target), "file", "recv", remote_path, str(local_path)],
                    timeout=PULL_TIMEOUT,
                    verbose=verbose,
                )

                if local_path.exists():
                    pulled_files.append({
                        "remote_path": remote_path,
                        "local_path": str(local_path.relative_to(Path(run_dir) / case_id)),
                        "status": "pulled",
                    })
                    print(f"[{case_id}] Pulled: {local_subdir}/{filename}")
                else:
                    snapshot_complete = False
                    pulled_files.append({
                        "remote_path": remote_path,
                        "status": "failed",
                        "error": "file not found after pull",
                    })
            except Exception as e:
                snapshot_complete = False
                pulled_files.append({
                    "remote_path": remote_path,
                    "status": "failed",
                    "error": str(e),
                })
                print(f"[{case_id}] Failed to pull {filename}: {e}", file=sys.stderr)

        root_results.append({
            "remote_root": remote_root,
            "local_root": local_subdir,
            "status": "pulled" if all(
                item.get("status") == "pulled"
                for item in pulled_files
                if item.get("remote_path", "").startswith(remote_root + "/")
            ) else "failed",
            "top_level_entries": top_level_entries,
        })

    # 保存拉取清单
    manifest = {
        "case_id": case_id,
        "remote_roots": list(_ACCESSIBLE_OUTPUT_ROOTS),
        "pulled_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_complete": snapshot_complete,
        "roots": root_results,
        "files": pulled_files,
    }
    (staging_dir / "outputs_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Publish the current round as one snapshot. Keep the previous snapshot until
    # the staging rename succeeds so an interrupted rename can be restored.
    previous_dir = case_output_dir / f".outputs-{uuid4().hex}.previous"
    had_previous = outputs_dir.exists()
    try:
        if had_previous:
            outputs_dir.rename(previous_dir)
        staging_dir.rename(outputs_dir)
    except Exception:
        if not outputs_dir.exists() and previous_dir.exists():
            previous_dir.rename(outputs_dir)
        raise
    else:
        if previous_dir.exists():
            try:
                shutil.rmtree(previous_dir)
            except OSError as exc:
                print(
                    f"[{case_id}] Warning: failed to remove previous output snapshot "
                    f"{previous_dir}: {exc}",
                    file=sys.stderr,
                )

    print(f"[{case_id}] Output files pulled: {len(pulled_files)}")
    return pulled_files


def extract_stop_content(local_log: Path, case_id: str, run_dir: str | Path) -> str | None:
    """从本地 JSONL 日志中提取 stop_reason=stop 的 assistant content（取最后一处），并落盘保存"""
    last_content = None
    try:
        with open(local_log, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 只检查 main agent 的 model_output 事件
                if event.get("agent_role") != "main":
                    continue
                if event.get("event") != "model_output":
                    continue

                payload = event.get("payload", {})
                assistant = payload.get("assistant", {})

                if assistant.get("stop_reason") != "stop":
                    continue

                # 提取 content
                content = assistant.get("content", [])
                if isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    if text_parts:
                        last_content = "".join(text_parts)

        # 输出并落盘最后一处 stop content
        if last_content:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            print(f"\n[{case_id}] === Stop Content (last) ===")
            print(last_content)
            print(f"[{case_id}] ====================\n")

            # 落盘保存
            run_path = Path(run_dir) / case_id
            run_path.mkdir(parents=True, exist_ok=True)
            content_file = run_path / f"{case_id}.content.txt"
            content_file.write_text(last_content, encoding='utf-8')
            print(f"[{case_id}] Stop content saved to: {content_file}")

            return last_content
    except Exception as e:
        print(f"[{case_id}] Failed to extract stop content: {e}", file=sys.stderr)
    return None
