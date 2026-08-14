from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from judge_file_organization import JudgeInputError, judge_file_organization


class JudgeFileOrganizationTests(unittest.TestCase):
    def _fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        outputs = root / "outputs"
        for name in ("Desktop", "Download", "Documents"):
            (outputs / name).mkdir(parents=True)
        target = outputs / "Desktop" / "move_file" / "ceshi.txt"
        target.parent.mkdir()
        target.write_bytes(b"fixture")
        digest = hashlib.md5(b"fixture").hexdigest()
        metadata = {
            "absolute_id": "FileOrganization_0_002",
            "task": "move fixture",
            "rubrics": [
                "Desktop 的直接子项是否恰好为 1 个，且完整名称集合为 move_file？",
                "Desktop\\move_file 是否存在且类型为目录，其直接子项是否恰好为 1 个，且完整名称集合为 ceshi.txt？",
                f"以下文件是否存在且类型为文件，且 MD5 分别正确：Desktop\\move_file\\ceshi.txt（{digest}）？",
            ],
        }
        metadata_path = root / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        return temp, metadata_path, outputs

    def test_all_rubrics_pass_for_clean_tree(self) -> None:
        temp, metadata, outputs = self._fixture()
        self.addCleanup(temp.cleanup)
        result = judge_file_organization(metadata, outputs)
        self.assertTrue(result["passed"])
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["summary"], {"total": 3, "passed": 3, "failed": 0})

    def test_extra_nested_directory_fails_exact_child_rubric(self) -> None:
        temp, metadata, outputs = self._fixture()
        self.addCleanup(temp.cleanup)
        nested = outputs / "Desktop" / "move_file" / "move_file"
        nested.mkdir()
        (nested / "ceshi.txt").write_bytes(b"fixture")
        result = judge_file_organization(metadata, outputs)
        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"], {"total": 3, "passed": 2, "failed": 1})
        self.assertFalse(result["rubrics"][1]["passed"])

    def test_missing_output_root_is_error(self) -> None:
        temp, metadata, outputs = self._fixture()
        self.addCleanup(temp.cleanup)
        (outputs / "Documents").rmdir()
        with self.assertRaisesRegex(JudgeInputError, "missing roots: Documents"):
            judge_file_organization(metadata, outputs)

    def test_unsupported_rubric_is_error(self) -> None:
        temp, metadata, outputs = self._fixture()
        self.addCleanup(temp.cleanup)
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["rubrics"] = ["任务看起来是否完成？"]
        metadata.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(JudgeInputError, "unsupported"):
            judge_file_organization(metadata, outputs)

    def test_manifest_can_block_incomplete_snapshot(self) -> None:
        temp, metadata, outputs = self._fixture()
        self.addCleanup(temp.cleanup)
        (outputs / "outputs_manifest.json").write_text(
            json.dumps({"snapshot_complete": False}), encoding="utf-8"
        )
        with self.assertRaisesRegex(JudgeInputError, "reports failure"):
            judge_file_organization(metadata, outputs)


if __name__ == "__main__":
    unittest.main()
