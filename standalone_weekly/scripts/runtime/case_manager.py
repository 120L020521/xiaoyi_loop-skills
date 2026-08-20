#!/usr/bin/env python3
"""
case_manager.py - Case 状态管理

负责 case 的完成标记、失败标记、中断标记等状态管理。
"""

import json
import os
from datetime import datetime


def _case_output(case_id: str, run_dir: str, case_dir: str | None = None) -> str:
    return case_dir if case_dir is not None else os.path.join(run_dir, case_id)


def load_config(config_path: str) -> dict:
    """加载配置文件"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def is_case_completed(case_id: str, run_dir: str, *, case_dir: str | None = None) -> bool:
    """检查case是否已完成"""
    marker = os.path.join(_case_output(case_id, run_dir, case_dir), 'completed.json')
    return os.path.exists(marker)


def mark_case_completed(
    case_id: str, run_dir: str, result: dict = None, *, case_dir: str | None = None
) -> None:
    """标记case完成"""
    case_output = _case_output(case_id, run_dir, case_dir)
    os.makedirs(case_output, exist_ok=True)

    marker = {
        'case_id': case_id,
        'completed_at': datetime.now().isoformat(),
        'result': result or {}
    }
    marker_file = os.path.join(case_output, 'completed.json')
    with open(marker_file, 'w', encoding='utf-8') as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)


def mark_case_failed(
    case_id: str, run_dir: str, error: str, *, case_dir: str | None = None
) -> None:
    """标记case失败"""
    case_output = _case_output(case_id, run_dir, case_dir)
    os.makedirs(case_output, exist_ok=True)

    marker = {
        'case_id': case_id,
        'failed_at': datetime.now().isoformat(),
        'error': error
    }
    marker_file = os.path.join(case_output, 'failed.json')
    with open(marker_file, 'w', encoding='utf-8') as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)


def mark_case_interrupted(
    case_id: str, run_dir: str, *, case_dir: str | None = None
) -> None:
    """标记case手动退出"""
    case_output = _case_output(case_id, run_dir, case_dir)
    os.makedirs(case_output, exist_ok=True)

    marker = {
        'case_id': case_id,
        'interrupted_at': datetime.now().isoformat(),
    }
    marker_file = os.path.join(case_output, 'interrupted.json')
    with open(marker_file, 'w', encoding='utf-8') as f:
        json.dump(marker, f, indent=2, ensure_ascii=False)
