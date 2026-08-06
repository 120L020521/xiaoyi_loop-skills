"""Offline environment checks for a workstation configuration."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from xiaoyi_loop.settings import LocalSettings


def _command_path(command: str) -> str | None:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or any(separator in command for separator in ("/", "\\")):
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which(command)


def run_doctor(settings: LocalSettings) -> int:
    """Print secret-free checks and return zero only when execution is ready."""
    checks: list[tuple[bool, str]] = []

    config_label = str(settings.config_path) if settings.config_path else "未找到（使用默认值）"
    checks.append((settings.config_path is not None, f"本机配置：{config_label}"))
    checks.append((sys.version_info >= (3, 10), f"Python：{sys.version.split()[0]}（要求 >= 3.10）"))

    hdc = _command_path(settings.hdc)
    checks.append((hdc is not None, f"HDC：{hdc or settings.hdc + '（未找到）'}"))
    checks.append(
        (
            settings.tasks_root is not None and settings.tasks_root.is_dir(),
            f"任务目录：{settings.tasks_root or '未配置'}",
        )
    )
    checks.append((settings.profiles_file.is_file(), f"Judge profiles：{settings.profiles_file}"))

    dependency_modules = {
        "httpx": "httpx",
        "langchain-openai": "langchain_openai",
        "PyYAML": "yaml",
        "python-pptx": "pptx",
        "pypdf": "pypdf",
        "pdfminer.six": "pdfminer",
        "xlrd": "xlrd",
    }
    missing = [
        package
        for package, module in dependency_modules.items()
        if importlib.util.find_spec(module) is None
    ]
    checks.append((not missing, "Python 依赖：" + ("完整" if not missing else "缺少 " + ", ".join(missing))))

    if settings.judge_profile:
        try:
            from standalone_judge.config import list_profiles

            profiles = list_profiles(profiles_path=settings.profiles_file)
            selected = next(
                (row for row in profiles if row.get("name") == settings.judge_profile),
                None,
            )
            if selected is None:
                checks.append((False, f"Judge profile：{settings.judge_profile}（不存在）"))
            else:
                key_name = selected.get("apiKeyEnv")
                configured = bool(selected.get("configured"))
                checks.append(
                    (
                        configured,
                        f"Judge profile：{settings.judge_profile}；密钥变量 {key_name} "
                        + ("已配置" if configured else "未配置"),
                    )
                )
        except Exception as exc:
            checks.append((False, f"Judge profile 无法读取：{type(exc).__name__}: {exc}"))
    else:
        checks.append((False, "Judge profile：未配置"))

    print("XiaoYi Loop 环境检查（不会显示密钥内容）")
    for passed, message in checks:
        print(f"[{'OK' if passed else 'FAIL'}] {message}")
    print(f"[INFO] 日志目录：{settings.logs_dir}")
    print(f"[INFO] 运行目录：{settings.run_dir}")
    print(f"[INFO] 状态文件：{settings.state_file}")
    return 0 if all(passed for passed, _ in checks) else 1
