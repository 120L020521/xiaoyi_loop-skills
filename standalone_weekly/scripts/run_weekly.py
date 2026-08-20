#!/usr/bin/env python3
"""Launch the standalone XiaoYi weekly-report runner against external data."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime"
BUNDLED_CONFIG = PACKAGE_ROOT / "assets" / "weekly_config.json"
DATA_PATH_KEYS = {
    "metadata_root", "deliverables_root", "output_root", "scripts_root",
    "task_artifacts_root", "mock_runner_script",
}
DEFAULT_MOCK_RUNNER = (
    PACKAGE_ROOT.parent / "note" / "data_yangshi" / "jiaoben" / "run_data_mock.py"
)


def _is_data_root(path: Path) -> bool:
    return (path / "task").is_dir() and (path / "deliverables_final").is_dir()


def _candidate_roots(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    current = Path.cwd().resolve()
    return [current, *current.parents]


def _resolve_data_root(explicit: str | None) -> Path:
    candidates = _candidate_roots(explicit)
    for candidate in candidates:
        if _is_data_root(candidate):
            return candidate
    checked = "\n  - ".join(str(path) for path in candidates)
    raise RuntimeError(
        "未找到外部数据根目录；该目录必须包含 task/ 和 deliverables_final/。"
        f"已检查：\n  - {checked}"
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"配置必须是 JSON 对象: {path}")
    return loaded


def _resolve_config_path(config_path: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def _external_path(
    cli_value: str | None,
    custom_config: dict[str, Any],
    custom_config_path: Path | None,
    key: str,
) -> Path | None:
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    if key in custom_config and custom_config_path is not None:
        return _resolve_config_path(custom_config_path, custom_config[key])
    return None


def _common_data_root(metadata_root: Path, deliverables_root: Path) -> Path | None:
    if (
        metadata_root.name == "task"
        and deliverables_root.name == "deliverables_final"
        and metadata_root.parent == deliverables_root.parent
    ):
        return metadata_root.parent
    return None


def _batch_mmdd(date_text: str) -> str:
    try:
        return datetime.strptime(date_text, "%Y%m%d").strftime("%m%d")
    except ValueError as exc:
        raise ValueError(f"--date 必须是 YYYYMMDD: {date_text!r}") from exc


def allocate_weekly_batch_root(agent_workspace: Path, date_text: str) -> Path:
    """Atomically allocate weekly-batches-MMDD-vN without reusing an old batch."""
    workspace = agent_workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    prefix = f"weekly-batches-{_batch_mmdd(date_text)}"
    for version in range(1, 10000):
        candidate = workspace / f"{prefix}-v{version}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"无法为 {prefix} 分配可用版本号")


def _is_non_run_invocation(forwarded: list[str]) -> bool:
    """Return whether the runtime invocation must not allocate an artifact batch."""
    return any(flag in forwarded for flag in ("--list", "--help", "-h", "--dry-run"))


def _agent_workspace(explicit: str | None) -> Path:
    """Resolve the exact workspace root without adding a named child directory."""
    return Path(explicit).expanduser().resolve() if explicit else Path.cwd().resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--project-root", help="包含 task/ 和 deliverables_final/ 的外部数据根目录")
    parser.add_argument("--metadata-root", help="外部 task 目录")
    parser.add_argument("--deliverables-root", help="外部 deliverables_final 目录")
    parser.add_argument(
        "--output-root",
        help="兼容模式的外部运行结果目录；真实运行默认写入当前 Agent 项目目录的版本化批次根",
    )
    parser.add_argument(
        "--agent-workspace",
        help="显式覆盖 Agent workspace；默认直接使用启动命令时的当前项目目录",
    )
    parser.add_argument(
        "--task-artifacts-root",
        help="任务优先布局根目录；每个 Task 写入 <root>/<metadata.absolute_id>/xiaoyi_file_runs/。",
    )
    parser.add_argument(
        "--mock-runner-script",
        help="note 数据清空+推送入口，默认使用当前仓库 note/data_yangshi/jiaoben/run_data_mock.py",
    )
    parser.add_argument("--config", help="可选覆盖配置；其中 scripts_root 始终使用 独立版内置脚本")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    known, forwarded = parser.parse_known_args(argv)

    custom_config_path = Path(known.config).expanduser().resolve() if known.config else None
    custom_config = _load_json_object(custom_config_path) if custom_config_path else {}

    metadata_root = _external_path(
        known.metadata_root, custom_config, custom_config_path, "metadata_root"
    )
    deliverables_root = _external_path(
        known.deliverables_root, custom_config, custom_config_path, "deliverables_root"
    )
    output_root = _external_path(
        known.output_root, custom_config, custom_config_path, "output_root"
    )
    task_artifacts_root = _external_path(
        known.task_artifacts_root,
        custom_config,
        custom_config_path,
        "task_artifacts_root",
    )
    batch_root: Path | None = None
    if known.agent_workspace:
        if known.output_root or known.task_artifacts_root:
            raise ValueError(
                "--agent-workspace 不能与 --output-root 或 --task-artifacts-root 同时使用"
            )
        batch_root = allocate_weekly_batch_root(
            _agent_workspace(known.agent_workspace), known.date
        )
        output_root = batch_root
        task_artifacts_root = batch_root
    elif (
        output_root is None
        and task_artifacts_root is None
        and not _is_non_run_invocation(forwarded)
    ):
        # The Agent workspace is the directory in which the launcher is invoked.
        # Do not synthesize an additional child directory named "workspace".
        batch_root = allocate_weekly_batch_root(_agent_workspace(None), known.date)
        output_root = batch_root
        task_artifacts_root = batch_root
    mock_runner_script = _external_path(
        known.mock_runner_script,
        custom_config,
        custom_config_path,
        "mock_runner_script",
    )

    data_root: Path | None = None
    if known.project_root or metadata_root is None or deliverables_root is None:
        data_root = _resolve_data_root(known.project_root)
    if metadata_root is None:
        assert data_root is not None
        metadata_root = data_root / "task"
    if deliverables_root is None:
        assert data_root is not None
        deliverables_root = data_root / "deliverables_final"
    if output_root is None:
        inferred_root = data_root or _common_data_root(metadata_root, deliverables_root)
        output_root = (inferred_root or Path.cwd().resolve()) / "xiaoyi_logs"

    if not metadata_root.is_dir():
        raise RuntimeError(f"task 数据目录不存在: {metadata_root}")
    if not deliverables_root.is_dir():
        raise RuntimeError(f"deliverables_final 数据目录不存在: {deliverables_root}")
    if mock_runner_script is None:
        mock_runner_script = DEFAULT_MOCK_RUNNER.resolve()
    if not mock_runner_script.is_file():
        raise RuntimeError(f"note 清空+推送脚本不存在: {mock_runner_script}")

    config = _load_json_object(BUNDLED_CONFIG)
    config.update({key: value for key, value in custom_config.items() if key not in DATA_PATH_KEYS})
    config.update(
        {
            "metadata_root": str(metadata_root),
            "deliverables_root": str(deliverables_root),
            "scripts_root": str(RUNTIME_ROOT),
            "mock_runner_script": str(mock_runner_script),
            "output_root": str(output_root),
            **(
                {"task_artifacts_root": str(task_artifacts_root)}
                if task_artifacts_root is not None
                else {}
            ),
        }
    )

    from runtime.weekly_runner import main as runner_main

    print(f"[weekly-runner] data_root={data_root or '<separate paths>'}", flush=True)
    print(f"[weekly-runner] runtime_root={RUNTIME_ROOT}", flush=True)
    if batch_root is not None:
        print(f"[weekly-runner] batch_root={output_root}", flush=True)
    with tempfile.TemporaryDirectory(prefix="xiaoyi-weekly-") as temp_dir:
        generated_config = Path(temp_dir) / "weekly_config.json"
        generated_config.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return runner_main(
            ["--config", str(generated_config), "--date", known.date, *forwarded]
        )


if __name__ == "__main__":
    raise SystemExit(main())
