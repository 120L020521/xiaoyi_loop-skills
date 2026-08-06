"""Offline tests for external Runner case preparation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from standalone_judge.batch import (
    _compact_trace_for_judge,
    _normalize_judge_result,
    _prepared_input_fingerprint,
    discover_cases,
    judge_case,
    prepare_batch,
)
from standalone_judge.config import JudgeProfile, resolve_profile
from standalone_judge.vendor import agent_eval


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _metadata() -> dict[str, object]:
    return {
        "absolute_id": 120,
        "task": "Create result.md.",
        "output_files": ["result.md"],
        "rubrics": [
            "The result file exists.",
            "The result contains the required answer.",
        ],
    }


def test_prepare_batch_builds_native_judge_task(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks"
    _write_json(task_root / "120" / "metadata.json", _metadata())
    data = tmp_path / "data"
    log = data / "logs" / "task_120.jsonl"
    log.parent.mkdir(parents=True)
    events = [
        {
            "event": "tool_call",
            "payload": {
                "tool_name": "create_file",
                "tool_call_id": "call-1",
                "args": {"path": "result.md"},
                "api_key": "secret-value",
            },
        },
        {
            "event": "tool_result",
            "payload": {
                "tool_name": "create_file",
                "tool_call_id": "call-1",
                "success": True,
            },
        },
    ]
    log.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    output = data / "outputs" / "task_120"
    output.mkdir(parents=True)
    (output / "result.md").write_text(
        "# Required answer",
        encoding="utf-8",
    )
    cases = data / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "task_id": "120",
                "log": "logs/task_120.jsonl",
                "output": "outputs/task_120",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = prepare_batch(
        cases_path=cases,
        task_root=task_root,
        prepared_dir=data / "prepared",
    )

    assert report["summary"] == {
        "total": 1,
        "prepared": 1,
        "failed": 0,
    }
    task_dir = data / "prepared" / "task120"
    agent = json.loads((task_dir / "agent.json").read_text(encoding="utf-8"))
    trace = agent["trace"]["executionTrace"]
    assert trace[0]["schema"] == "workspace-bench.runner-event.v1"
    assert trace[0]["eventType"] == "tool_call"
    assert "rawEvent" not in trace[0]
    assert "secret-value" not in json.dumps(agent)
    assert (task_dir / "output" / "result.md").is_file()
    assert (task_dir / "metadata.json").is_file()
    assert (task_dir / "normalized_runner_log.jsonl").is_file()
    assert not (task_dir / "judge_runner_log.jsonl").exists()
    assert not (task_dir / "sanitized_runner_log.jsonl").exists()
    assert agent["trace"]["audit"]["compactTraceEmbedded"] is True
    output_row = agent["trace"]["outputs"]["outputManifest"][0]
    assert output_row["sourcePath"] == output_row["outputPath"]
    assert not Path(output_row["sourcePath"]).is_absolute()

    previous_mode = os.environ.get("STANDALONE_JUDGE_TRACE_MODE")
    try:
        os.environ["STANDALONE_JUDGE_TRACE_MODE"] = "compact"
        compact_trace = agent_eval._extract_trace(
            str(task_dir),
            "evaluation_sys",
        )["executionTrace"]
        os.environ["STANDALONE_JUDGE_TRACE_MODE"] = "full"
        full_trace = agent_eval._extract_trace(
            str(task_dir),
            "evaluation_sys",
        )["executionTrace"]
    finally:
        if previous_mode is None:
            os.environ.pop("STANDALONE_JUDGE_TRACE_MODE", None)
        else:
            os.environ["STANDALONE_JUDGE_TRACE_MODE"] = previous_mode
    assert "rawEvent" not in compact_trace[0]
    assert "rawEvent" in full_trace[0]


def test_prepare_batch_discovers_xiaoyi_task_directories(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "metadata"
    _write_json(task_root / "13" / "metadata.json", _metadata())
    _write_json(task_root / "112" / "metadata.json", _metadata())
    logs_root = tmp_path / "xiaoyi_logs"

    task_13 = logs_root / "task13"
    task_13.mkdir(parents=True)
    (task_13 / "task13.jsonl").write_text(
        '{"event":"agent_start","payload":{}}\n',
        encoding="utf-8",
    )
    outputs = task_13 / "outputs"
    outputs.mkdir()
    (outputs / "result.md").write_text("done", encoding="utf-8")
    (outputs / "extra-artifact.bin").write_bytes(b"artifact")

    task_112 = logs_root / "task112"
    task_112.mkdir()
    (task_112 / "task112.jsonl").write_text(
        '{"event":"agent_start","payload":{}}\n',
        encoding="utf-8",
    )

    cases = discover_cases(logs_dir=logs_root, task_root=task_root)

    assert [case.task_id for case in cases] == ["13", "112"]
    assert cases[0].log_path.name == "task13.jsonl"
    assert cases[0].output_paths == (outputs.resolve(),)
    assert cases[1].log_path.name == "task112.jsonl"
    assert cases[1].output_paths == ()
    assert cases[1].metadata_path == (
        task_root / "112" / "metadata.json"
    ).resolve()

    report = prepare_batch(
        logs_dir=logs_root,
        task_root=task_root,
        prepared_dir=tmp_path / "prepared",
        log_format="xiaoyi",
    )

    assert report["summary"] == {
        "total": 2,
        "prepared": 2,
        "failed": 0,
    }
    assert report["casesFile"] is None
    assert report["logsDir"] == str(logs_root.resolve())
    assert (
        tmp_path / "prepared" / "task13" / "output" / "result.md"
    ).is_file()
    assert (
        tmp_path
        / "prepared"
        / "task13"
        / "output"
        / "extra-artifact.bin"
    ).is_file()

    filtered_root = tmp_path / "filtered-prepared"
    filtered_report = prepare_batch(
        logs_dir=logs_root,
        task_root=task_root,
        prepared_dir=filtered_root,
        log_format="xiaoyi",
        task_ids=["task13"],
    )
    assert filtered_report["summary"] == {
        "total": 1,
        "prepared": 1,
        "failed": 0,
    }
    assert filtered_report["requestedTaskIds"] == ["13"]
    assert (filtered_root / "task13").is_dir()
    assert not (filtered_root / "task112").exists()


def test_prepared_fingerprint_is_independent_of_workstation_paths(
    tmp_path: Path,
) -> None:
    fingerprints: list[dict[str, object]] = []
    for machine_name in ("machine-a", "machine-b"):
        machine = tmp_path / machine_name
        task_root = machine / "tasks"
        _write_json(task_root / "120" / "metadata.json", _metadata())
        log_dir = machine / "xiaoyi_logs" / "task120"
        log_dir.mkdir(parents=True)
        (log_dir / "task120.jsonl").write_text(
            '{"event":"agent_start","payload":{}}\n',
            encoding="utf-8",
        )
        outputs = log_dir / "outputs"
        outputs.mkdir()
        (outputs / "result.md").write_text("same output", encoding="utf-8")

        report = prepare_batch(
            logs_dir=machine / "xiaoyi_logs",
            task_root=task_root,
            prepared_dir=machine / "xiaoyi_judge" / "prepared",
            log_format="xiaoyi",
        )
        fingerprints.append(report["cases"][0]["inputFingerprint"])

    assert fingerprints[0] == fingerprints[1]


def test_xiaoyi_compaction_removes_only_structural_duplicates() -> None:
    events = [
        {
            "schemaVersion": 1,
            "schema": "workspace-bench.runner-event.v1",
            "sequence": 1,
            "eventType": "model_input",
            "sourceFormat": "event-stream",
            "rawEvent": {"event": "model_input", "payload": {"messages": ["old"]}},
            "content": {
                "messages": ["old"],
                "systemPrompt": "large repeated prompt",
                "tools": [{"name": "read"}],
            },
        },
        {
            "schemaVersion": 1,
            "schema": "workspace-bench.runner-event.v1",
            "sequence": 2,
            "eventType": "tool_result",
            "sourceFormat": "event-stream",
            "rawEvent": {"event": "tool_result", "payload": {"value": "evidence"}},
            "toolName": "read",
            "toolOutput": {"value": "evidence"},
        },
        {
            "schemaVersion": 1,
            "schema": "workspace-bench.runner-event.v1",
            "sequence": 3,
            "eventType": "model_output",
            "sourceFormat": "event-stream",
            "rawEvent": {"event": "model_output", "payload": {"text": "answer"}},
            "content": {"text": "answer"},
        },
    ]

    compacted, report = _compact_trace_for_judge(
        events,
        source_format="event-stream",
    )

    assert [event["eventType"] for event in compacted] == [
        "tool_result",
        "model_output",
    ]
    assert all("rawEvent" not in event for event in compacted)
    assert compacted[0]["toolOutput"] == {"value": "evidence"}
    assert compacted[1]["content"] == {"text": "answer"}
    assert report["omittedCumulativeModelInputEvents"] == 1
    assert report["removedRawEventCopies"] == 3
    assert report["uniquePayloadTruncation"] is False
    assert report["bytesAfter"] < report["bytesBefore"]


def test_prepare_batch_keeps_other_cases_when_one_input_is_missing(
    tmp_path: Path,
) -> None:
    task_root = tmp_path / "tasks"
    _write_json(task_root / "120" / "metadata.json", _metadata())
    _write_json(task_root / "121" / "metadata.json", _metadata())
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    (logs / "task_120.jsonl").write_text(
        '{"event":"agent_start","payload":{}}\n',
        encoding="utf-8",
    )
    cases = data / "cases.jsonl"
    cases.write_text(
        "\n".join(
            [
                '{"task_id":"120","log":"logs/task_120.jsonl","output":null}',
                '{"task_id":"121","log":"logs/missing.jsonl","output":null}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = prepare_batch(
        cases_path=cases,
        task_root=task_root,
        prepared_dir=data / "prepared",
    )

    assert report["summary"] == {
        "total": 2,
        "prepared": 1,
        "failed": 1,
    }
    assert report["cases"][1]["status"] == "error"
    assert "Runner JSONL log not found" in report["cases"][1]["error"]


def test_judge_result_validation_marks_missing_rubrics_failed() -> None:
    metadata = _metadata()
    native_result = {
        "rubrics": [
            {
                "index": 0,
                "passed": True,
                "confidence": 1.2,
                "evidence": "result.md exists",
            }
        ]
    }

    normalized = _normalize_judge_result(
        metadata=metadata,
        result=native_result,
    )

    assert normalized["score"] == 0.5
    assert normalized["summary"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
    }
    assert normalized["rubrics"][0]["confidence"] == 1.0
    assert normalized["rubrics"][1]["passed"] is False
    assert any(
        "Missing rubric index 1" in warning
        for warning in normalized["validationWarnings"]
    )


def test_resume_skips_only_when_prepared_input_fingerprint_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_dir = tmp_path / "prepared" / "task120"
    _write_json(task_dir / "metadata.json", _metadata())
    _write_json(task_dir / "agent.json", {"trace": {"executionTrace": []}})
    (task_dir / "normalized_runner_log.jsonl").write_text(
        '{"eventType":"agent_start"}\n',
        encoding="utf-8",
    )
    output_dir = task_dir / "output"
    output_dir.mkdir()
    (output_dir / "result.md").write_text("first", encoding="utf-8")
    fingerprint = _prepared_input_fingerprint(task_dir)
    _write_json(
        task_dir / "case_manifest.json",
        {"taskId": "120", "inputFingerprint": fingerprint},
    )

    profile = JudgeProfile(
        name="test",
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        temperature=0.0,
        extra_body=None,
    )
    results_dir = tmp_path / "results" / "test"
    existing = {
        "taskId": "120",
        "status": "success",
        "traceMode": "compact",
        "judgeProfile": profile.name,
        "judgeModel": profile.model,
        "judgeBaseUrl": profile.base_url,
        "inputFingerprint": fingerprint,
        "score": 1.0,
    }
    _write_json(results_dir / "task120" / "judge_result.json", existing)

    import standalone_judge.judge_core.judge_agent as judge_agent

    calls = 0

    def fake_run_judge(**kwargs):
        nonlocal calls
        calls += 1
        return {
            "rubrics": [
                {"index": 0, "passed": True, "confidence": 1.0, "evidence": "ok"},
                {"index": 1, "passed": True, "confidence": 1.0, "evidence": "ok"},
            ],
            "judge": {"error": None},
        }

    monkeypatch.setattr(judge_agent, "run_judge", fake_run_judge)
    resumed = judge_case(
        task_dir=task_dir,
        results_dir=results_dir,
        profile=profile,
        trace_mode="compact",
        resume=True,
        overwrite=False,
    )
    assert resumed["_resumed"] is True
    assert calls == 0

    (output_dir / "result.md").write_text("changed", encoding="utf-8")
    changed_fingerprint = _prepared_input_fingerprint(task_dir)
    rerun = judge_case(
        task_dir=task_dir,
        results_dir=results_dir,
        profile=profile,
        trace_mode="compact",
        resume=True,
        overwrite=False,
    )
    assert calls == 1
    assert rerun["inputFingerprint"] == changed_fingerprint
    assert rerun["status"] == "success"


def test_profile_resolves_request_controls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(
        """
[profiles.glm47]
base_url = "https://example.test/v1"
model = "glm-4.7"
api_key_env = "TEST_GLM_KEY"
temperature = 1.0
request_timeout_s = 600
max_retries = 0
max_tokens = 8192
inter_task_delay_s = 30
extra_body_json = '{"thinking":{"type":"disabled"}}'
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_GLM_KEY", "test-only")

    profile = resolve_profile(name="glm47", profiles_path=profiles)

    assert profile.request_timeout_s == 600
    assert profile.max_retries == 0
    assert profile.max_tokens == 8192
    assert profile.inter_task_delay_s == 30
    assert profile.extra_body == {"thinking": {"type": "disabled"}}
