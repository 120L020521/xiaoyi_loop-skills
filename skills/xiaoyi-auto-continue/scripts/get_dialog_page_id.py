#!/usr/bin/env python3
"""
get_dialog_page_id.py - 获取小艺历史对话的 dialogPageId

根据调试命令获取当前活跃的历史对话列表，返回每个对话的 dialogPageId。
"""

import argparse
import json
import subprocess
import sys
import time
import os

# 当作为模块 import 时，添加 scripts 目录到 sys.path
if __name__ != "__main__":
    _scripts_dir = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

from hdc_client import remote_shell
from hdc_client import run_hdc
VASSISTANT_BUNDLE = "com.huawei.hmos.vassistant"
VASSISTANT_ABILITY = "PCAgentTaskAbility"
HISTORY_FILE = "/data/app/el2/100/base/com.huawei.hmos.vassistant/files/history_list.json"
SHELL_TIMEOUT = 30


def hdc_path() -> str:
    import shutil
    path = shutil.which("hdc.exe") or shutil.which("hdc")
    if not path:
        raise RuntimeError("hdc not found in PATH")
    return path


def fetch_history_list(target: str | None = None) -> list[dict]:
    """获取历史对话列表"""
    # 构建 aa start 命令
    command = (
        f"aa start -b {VASSISTANT_BUNDLE} "
        f"-a {VASSISTANT_ABILITY} "
        f"--ps launch_type pc_agent_task_list_history "
    )
    verbose = False
    out = remote_shell(command, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
    if out.strip():
        print(out.strip())

    # 等待数据准备好（增加等待时间，因为历史列表可能需要更久才能刷新）
    wait_seconds = 5
    print(f"Waiting {wait_seconds} seconds for history list to load...")
    time.sleep(wait_seconds)

    # 读取 history_list.json（带重试，避免文件异步生成/未写完导致的偶发读空）
    cmd = (f"cat /data/app/el2/100/base/com.huawei.hmos.vassistant/files/history_list.json")

    verbose = False
    max_retries = 3
    retry_delay = 2  # 秒
    for attempt in range(1, max_retries + 1):
        out = remote_shell(cmd, target=target, timeout=SHELL_TIMEOUT, verbose=verbose)
        text = out.strip()
        if text:
            parsed = _parse_history_json(text)
            if isinstance(parsed, list) and len(parsed) > 0:
                return parsed
        if attempt < max_retries:
            print(f"[get_dialog_page_id] history_list.json not ready or malformed, retrying ({attempt}/{max_retries})...", file=sys.stderr)
            time.sleep(retry_delay)
    # 重试耗尽仍未取到有效列表
    print("[get_dialog_page_id] Warning: history_list.json still empty after retries", file=sys.stderr)
    return []


def _parse_history_json(text: str) -> list[dict]:
    """解析 history_list.json，兼容设备端偶发的「双层方括号」坏 JSON。

    设备端 bug 有时会写出 `[{...}, {...}]]`（末尾多一个 `]`），导致
    json.loads 报 Extra data。这里先严格解析，失败则从首个 `[` 开始
    逐个尝试以每个 `]` 结尾的子串解析，取第一个能成功解析的。
    """
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    if start < 0:
        return []
    # 从后往前找最后一个能成功解析的 ] —— 实际坏数据是末尾多 ]，
    # 所以从首个 ] 之后逐个往后试更稳：找到第一个能解析成功的子串
    pos = start
    while True:
        end = text.find("]", pos)
        if end < 0:
            break
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pos = end + 1
    print(f"[get_dialog_page_id] fallback parse failed: no valid [...] substring", file=sys.stderr)
    return []


def get_latest_dialog_page_id(target: str | None = None) -> str:
    """获取最新的 dialogPageId（index=0）

    Returns:
        最新的 dialogPageId 字符串，如果失败或无历史则返回空字符串
    """
    try:
        history = fetch_history_list(target=target)
        if history and len(history) > 0:
            return history[0].get("dialogPageId", "")
        return ""
    except Exception as e:
        print(f"[get_dialog_page_id] Failed to get dialogPageId: {e}", file=sys.stderr)
        return ""


def print_history_list(history: list[dict]) -> None:
    """格式化输出历史列表"""
    if not history:
        print("No history found.")
        return

    print(f"\n{'='*60}")
    print(f"Total history entries: {len(history)}")
    print(f"{'='*60}\n")

    for item in history:
        print(f"Index: {item.get('index')}")
        print(f"  dialogPageId: {item.get('dialogPageId')}")
        print(f"  agentId: {item.get('agentId')}")
        print(f"  title: {item.get('title')}")
        print(f"  updateTime: {item.get('updateTime')}")
        print(f"  isTop: {item.get('isTop')}")
        print(f"  generatingStatus: {item.get('generatingStatus')}")
        print()


def main():
    parser = argparse.ArgumentParser(description="获取小艺历史对话的 dialogPageId")
    parser.add_argument("--target", "-t", help="hdc target address")
    parser.add_argument("--json", "-j", action="store_true", help="Output raw JSON")
    parser.add_argument("--latest", "-l", action="store_true", help="Only output latest dialogPageId")
    args = parser.parse_args()

    target = args.target

    try:
        if args.json:
            history = fetch_history_list(target=target)
            print(json.dumps(history, ensure_ascii=False, indent=2))
        elif args.latest:
            # 使用新函数获取最新 dialogPageId（内部只调用一次 fetch_history_list）
            page_id = get_latest_dialog_page_id(target=target)
            if page_id:
                print(page_id)
            else:
                print("Failed to get latest dialogPageId", file=sys.stderr)
                sys.exit(1)
        else:
            history = fetch_history_list(target=target)
            print_history_list(history)

            # 单独输出 dialogPageId 列表方便脚本使用
            page_ids = [item.get("dialogPageId") for item in history if item.get("dialogPageId")]
            if page_ids:
                print(f"\nAll dialogPageIds:")
                for pid in page_ids:
                    print(f"  {pid}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
