"""Contract tests for canonical XiaoYi Runner artifact paths."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import batch_runner
import pipeline
from batch_runner import RemoteLog, TaskSpec, TaskTimeoutError
from prepare_logs import _is_judgeable_log_dir


def test_failed_trace_uses_canonical_path_and_remains_judgeable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_run_hdc(arguments, *, timeout, verbose=False):
        Path(arguments[-1]).write_text(
            '{"event":"agent_start","payload":{}}\n',
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr(batch_runner, "run_hdc", fake_run_hdc)
    monkeypatch.setattr(batch_runner, "pull_declared_outputs", lambda *args, **kwargs: [])

    task = TaskSpec(
        task_id=25,
        metadata_path=tmp_path / "metadata.json",
        task_text="test",
    )
    log = RemoteLog(
        user_id="100",
        name="session.jsonl",
        path="/remote/session.jsonl",
        size=1,
        mtime=1,
    )

    trace_path = batch_runner.pull_log(
        log,
        task=task,
        query_characters=4,
        out_dir=tmp_path,
        target=None,
        status="failed",
        failure_reason="runner error",
    )

    task_dir = tmp_path / "task25"
    assert trace_path == task_dir / "task25.jsonl"
    assert {path.name for path in tmp_path.iterdir() if path.is_dir()} == {"task25"}
    meta = json.loads((task_dir / "task25.meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert _is_judgeable_log_dir(task_dir, "25") is True


def test_missing_trace_is_not_judgeable(tmp_path: Path) -> None:
    task_dir = tmp_path / "task26"
    task_dir.mkdir()
    (task_dir / "task26.meta.json").write_text(
        json.dumps({"task_id": 26, "status": "failed"}),
        encoding="utf-8",
    )

    assert _is_judgeable_log_dir(task_dir, "26") is False


def test_failure_with_trace_still_enters_judge_when_output_collection_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = TaskSpec(
        task_id=27,
        metadata_path=tmp_path / "metadata.json",
        task_text="test",
    )
    log = RemoteLog(
        user_id="100",
        name="session.jsonl",
        path="/remote/session.jsonl",
        size=1,
        mtime=1,
    )

    def partial_pull(*args, **kwargs):
        task_dir = tmp_path / "task27"
        task_dir.mkdir()
        (task_dir / "task27.jsonl").write_text("{}\n", encoding="utf-8")
        raise OSError("output collection failed")

    monkeypatch.setattr(pipeline, "pull_log", partial_pull)
    error = RuntimeError("runner failed")
    error.active_log = log
    args = SimpleNamespace(
        no_force_stop=True,
        settle=0,
        target=None,
        verbose=False,
    )

    collected = pipeline._handle_runner_failure(
        args,
        task,
        4,
        error,
        tmp_path,
    )

    assert collected is True
    meta = json.loads(
        (tmp_path / "task27" / "task27.meta.json").read_text(encoding="utf-8")
    )
    assert meta["status"] == "failed"
    assert meta["artifact_error"] == "output collection failed"


def test_timeout_with_trace_still_enters_judge_when_output_collection_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = TaskSpec(
        task_id=28,
        metadata_path=tmp_path / "metadata.json",
        task_text="test",
    )
    log = RemoteLog(
        user_id="100",
        name="session.jsonl",
        path="/remote/session.jsonl",
        size=1,
        mtime=1,
    )

    def partial_pull(*args, **kwargs):
        task_dir = tmp_path / "task28"
        task_dir.mkdir(exist_ok=True)
        (task_dir / "task28.jsonl").write_text("{}\n", encoding="utf-8")
        raise OSError("output collection failed")

    monkeypatch.setattr(pipeline, "pull_log", partial_pull)
    error = TaskTimeoutError("task28 exceeded 60 seconds", active_log=log)
    args = SimpleNamespace(
        no_force_stop=True,
        settle=0,
        target=None,
        verbose=False,
    )

    collected = pipeline._handle_runner_timeout(
        args,
        task,
        4,
        error,
        tmp_path,
    )

    assert collected is True
    meta = json.loads(
        (tmp_path / "task28" / "task28.meta.json").read_text(encoding="utf-8")
    )
    assert meta["status"] == "timeout"
    assert meta["artifact_error"] == "output collection failed"
