"""Tests for generic XiaoYi Task dataset discovery and unbounded IDs."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import run_tasks
from batch_runner import load_task_spec, parse_tasks
from xiaoyi_loop.task_locator import (
    TaskLocationError,
    discover_workspace_metadata,
    resolve_task_specs,
)


def _write_task(dataset_root: Path, task_id: int) -> Path:
    task_dir = dataset_root / str(task_id)
    task_dir.mkdir(parents=True)
    metadata = task_dir / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "absolute_id": task_id,
                "task": f"run task {task_id}",
                "rubrics": ["completed"],
            }
        ),
        encoding="utf-8",
    )
    return metadata.resolve()


class TaskLocatorTests(unittest.TestCase):
    def test_discovers_every_immediate_directory_containing_task(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            expected = {
                _write_task(workspace / "task", 39),
                _write_task(workspace / "task1", 40),
                _write_task(workspace / "filestask", 112),
                _write_task(workspace / "MyTasks2026", 9001),
            }
            _write_task(workspace / "dataset", 77)

            self.assertEqual(
                set(discover_workspace_metadata(workspace)),
                expected,
            )

    def test_explicit_dataset_root_selects_reused_id(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            selected = _write_task(workspace / "filestask", 112)

            located = resolve_task_specs(
                ["112"],
                workspace=workspace,
                explicit_locations=(workspace / "filestask",),
            )

            self.assertEqual(located.source, "explicit")
            self.assertEqual(
                [spec.metadata_path for spec in located.specs],
                [selected],
            )

    def test_ambiguous_id_across_datasets_requires_explicit_root(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            _write_task(workspace / "filestask", 112)

            with self.assertRaisesRegex(TaskLocationError, "路径不唯一"):
                resolve_task_specs(["112"], workspace=workspace)

    def test_explicit_dataset_root_never_falls_back_to_another_dataset(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            _write_task(workspace / "filestask", 39)

            with self.assertRaisesRegex(TaskLocationError, "找不到 Task 112"):
                resolve_task_specs(
                    ["112"],
                    workspace=workspace,
                    explicit_locations=(workspace / "filestask",),
                )

    def test_direct_task_path_is_authoritative(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            selected = _write_task(workspace / "filestask", 112)

            located = resolve_task_specs(
                [str(selected.parent)],
                workspace=workspace,
            )

            self.assertEqual(located.source, "explicit")
            self.assertEqual(located.specs[0].metadata_path, selected)

    def test_positional_dataset_root_can_scope_a_numeric_selector(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            selected = _write_task(workspace / "filestask", 112)

            located = resolve_task_specs(
                [str(workspace / "filestask"), "112"],
                workspace=workspace,
            )

            self.assertEqual(len(located.specs), 1)
            self.assertEqual(located.specs[0].metadata_path, selected)

    def test_multiple_exact_task_dirs_need_no_redundant_selectors(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            task112 = _write_task(workspace / "task", 112)
            task39 = _write_task(workspace / "filetask", 39)

            located = resolve_task_specs(
                [],
                workspace=workspace,
                explicit_locations=(task112.parent, task39.parent),
            )

            self.assertEqual(located.source, "explicit")
            self.assertEqual(
                {spec.metadata_path for spec in located.specs},
                {task112, task39},
            )

    def test_dataset_roots_without_ids_return_actionable_error(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            _write_task(workspace / "task", 112)
            _write_task(workspace / "task", 113)
            _write_task(workspace / "filetask", 39)

            with self.assertRaisesRegex(
                TaskLocationError,
                "组合成精确的 <数据集>/<ID>",
            ):
                resolve_task_specs(
                    [],
                    workspace=workspace,
                    explicit_locations=(
                        workspace / "task",
                        workspace / "filetask",
                    ),
                )

    def test_ids_have_no_fixed_upper_bound(self) -> None:
        with TemporaryDirectory() as raw:
            metadata = _write_task(Path(raw) / "customtask", 1_000_000)

            self.assertEqual(parse_tasks(["0,389,1000000"]), [0, 389, 1_000_000])
            self.assertEqual(load_task_spec(metadata).task_id, 1_000_000)

    def test_public_launcher_passes_scoped_high_id_metadata_to_runner(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            metadata = _write_task(workspace / "filestask", 1_000_000)

            with patch.object(run_tasks, "pipeline_main", return_value=0) as runner:
                result = run_tasks.main(
                    [
                        "1000000",
                        "--workspace",
                        str(workspace),
                        "--task-dir",
                        str(workspace / "filestask"),
                    ]
                )

            self.assertEqual(result, 0)
            runner_arguments = runner.call_args.args[0]
            self.assertEqual(Path(runner_arguments[0]), metadata)
            self.assertNotIn("--min-task", runner_arguments)
            self.assertNotIn("--max-task", runner_arguments)

    def test_public_launcher_accepts_multiple_exact_task_dirs_without_ids(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            task112 = _write_task(workspace / "task", 112)
            task39 = _write_task(workspace / "filetask", 39)

            with patch.object(run_tasks, "pipeline_main", return_value=0) as runner:
                result = run_tasks.main(
                    [
                        "--workspace",
                        str(workspace),
                        "--task-dir",
                        str(task112.parent),
                        "--task-dir",
                        str(task39.parent),
                    ]
                )

            self.assertEqual(result, 0)
            runner_arguments = runner.call_args.args[0]
            self.assertEqual(
                {Path(runner_arguments[0]), Path(runner_arguments[1])},
                {task112, task39},
            )


if __name__ == "__main__":
    unittest.main()
