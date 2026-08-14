#!/usr/bin/env python3
"""Compact standard-library regression suite for the production HALO skill path."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "halo-trace-converter")]

import demo_make_traces  # noqa: E402
from converter_core.conversion import convert_events  # noqa: E402
from halo_rlm.better_harness import (  # noqa: E402
    BETTER_HARNESS_COMPONENTS,
    DEFAULT_EDITABLE_SURFACES,
)
from halo_rlm.models import TraceFilters  # noqa: E402
from halo_rlm.report_contract import (  # noqa: E402
    REPORT_SCHEMA_VERSION,
    REPORT_STRUCTURE_GUIDANCE,
    build_report,
    normalize_json_report,
    render_report_example,
)
from halo_rlm.trace_store import TraceStore  # noqa: E402

PASSED = 0
TMP = Path(tempfile.mkdtemp(prefix="halo_rlm_tests_"))
TRACE = TMP / "traces.jsonl"


def check(condition: bool, label: str) -> None:
    global PASSED
    if not condition:
        raise AssertionError(label)
    PASSED += 1
    print(f"  ok: {label}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def run_python(*args: str, cwd: Path | None = None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def valid_evidence(source: str = "TRACE") -> dict:
    return {
        "source": source,
        "reference": "span-1" if source == "TRACE" else "input.xlsx/Sheet1!V:V",
        "tool": "test_tool" if source == "TRACE" else "",
        "fact": "测试证据证明数据处理存在问题。",
        "raw_log_excerpt": "test error" if source == "TRACE" else "",
        "error": "test error" if source == "TRACE" else "",
    }


def valid_error() -> dict:
    return {
        "error_id": "ERR1",
        "priority": "P0",
        "category": "TOOL_FAILURE",
        "title": "测试工具执行失败",
        "occurrence_count": 1,
        "summary": "测试工具没有成功完成预期操作。",
        "evidence": [valid_evidence()],
        "root_cause": "工具参数与运行环境不兼容。",
        "recovery_status": "UNRECOVERED",
        "impact": "任务未完成预期操作。",
    }


def valid_change(component="prompt", target="runner_skill.md", priority="P0") -> dict:
    return {
        "priority": priority,
        "component": component,
        "target": target,
        "title": "修复测试问题",
        "error_refs": ["ERR1"],
        "problem": "当前实现不能可靠处理该输入。",
        "implementation": "增加明确处理逻辑并在完成后校验结果。",
        "acceptance_criteria": ["相同输入必须通过验证。"],
        "expected_impact": "提高任务执行正确性。",
    }


def valid_report(classification="UNKNOWN", change_count=3) -> dict:
    changes = [
        valid_change(),
        valid_change("tool_impl", "workspace_bench_tools.ts", "P1"),
        valid_change("tool_definition", "workspace_bench_tools.ts", "P2"),
    ][:change_count]
    return build_report(
        report_summary={
            "task_id": "task-test",
            "task": "测试任务内容。",
            "trace_ids": ["trace-1"],
        },
        execution_classification=classification,
        primary_failure_mode="工具参数与运行环境不兼容。",
        error_findings=[valid_error()],
        proposed_changes=changes,
    )


def normalize(report: dict) -> str:
    return normalize_json_report(
        json.dumps(report, ensure_ascii=False),
        allowed_components=BETTER_HARNESS_COMPONENTS,
        allowed_targets=DEFAULT_EDITABLE_SURFACES,
    )


def expect_error(report: dict, text: str, label: str) -> None:
    try:
        normalize(report)
    except ValueError as exc:
        check(text in str(exc), label)
        return
    raise AssertionError(label)


def test_trace_store() -> None:
    section("TraceStore core behavior")
    demo_make_traces.main(["demo_make_traces.py", str(TRACE)])
    store = TraceStore(str(TRACE))
    overview = store.get_overview()
    check(overview["total_traces"] == 6 and overview["total_spans"] == 71, "demo dataset shape")
    check(overview["error_trace_count"] == 3, "error traces are indexed")
    check(store.count_traces(TraceFilters(has_errors=True))["total"] == 3, "error filter")
    check(store.count_traces(TraceFilters(model_names=["gpt-4o-mini"]))["total"] == 2, "model filter")

    page1 = store.query_traces(TraceFilters(), limit=3, offset=0)
    page2 = store.query_traces(TraceFilters(), limit=3, offset=3)
    ids = [row["trace_id"] for row in page1["traces"] + page2["traces"]]
    check(len(ids) == len(set(ids)) == 6, "pagination partitions traces")

    trace = store.view_trace("trace-ok-001")
    check(trace["trace_id"] == "trace-ok-001" and trace["spans"], "small trace discovery")
    big = store.view_trace("trace-big-003")
    check(not big["spans"] and big["oversized"]["span_count"] == 60, "oversized trace summary")
    span = store.view_spans("trace-big-003", ["span-003-00"])
    check(len(span["spans"]) == 1, "surgical span read")

    found = store.search_trace("trace-err-002", "MaxTurnsExceeded")
    check(found["match_count"] == 2, "trace search finds attributes and status")
    check("trace_id: trace-err-002" in store.render_trace("trace-err-002"), "bounded text rendering")
    try:
        store.view_trace("missing")
    except KeyError:
        check(True, "unknown trace is rejected")
    else:
        raise AssertionError("unknown trace is rejected")


def test_report_contract() -> None:
    section("v7 report contract")
    example = render_report_example(BETTER_HARNESS_COMPONENTS, include_evaluator_context=True)
    check(
        '"error_findings"' in example
        and '"raw_log_excerpt"' in example
        and "P0 directly causes" in REPORT_STRUCTURE_GUIDANCE,
        "prompt exposes the fixed v7 structure and priority policy",
    )
    report = valid_report()
    check(json.loads(normalize(report))["schema_version"] == REPORT_SCHEMA_VERSION, "valid report normalizes")

    cases = [
        (lambda r: r["diagnosis"].update(execution_classification="SUCCESS"), "execution_classification must be one of", "classification enum"),
        (lambda r: r["diagnosis"]["error_findings"][0].update(summary="English only"), "Simplified Chinese", "Chinese narratives"),
        (lambda r: r["diagnosis"]["error_findings"][0]["evidence"][0].update(raw_log_excerpt=""), "must be non-empty", "TRACE excerpt required"),
        (lambda r: r["proposed_changes"][0].update(component="harness"), "component must be one of", "component whitelist"),
        (lambda r: r["proposed_changes"][0].update(error_refs=["ERR99"]), "unknown error ids", "change references"),
    ]
    for mutate, message, label in cases:
        candidate = copy.deepcopy(report)
        mutate(candidate)
        expect_error(candidate, message, label)

    source_only = copy.deepcopy(report)
    source_only["diagnosis"]["error_findings"][0]["evidence"] = [valid_evidence("SOURCE_FILE")]
    check(bool(normalize(source_only)), "non-TRACE evidence may independently prove an error")
    expect_error(valid_report("FAILED", 1), "exactly 3-5", "FAILED change-count rule")


def test_agent_cli() -> None:
    section("agent_cli complete validation")
    root = TMP / "agent-cli"
    root.mkdir()
    task = root / "task.json"
    judge = root / "judge.json"
    prompt = root / "halo_prompt.txt"
    report = root / "halo_report.json"
    source = root / "source.jsonl"
    prepared = root / "prepared.halo.jsonl"
    manifest = root / "halo-prepared-manifest.json"
    write_json(task, {"id": 15, "task": "Create result.txt", "output_files": ["result.txt"]})
    write_json(judge, {"passed": False, "score": 0.25, "feedback": "incomplete"})
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)

    def agent(*args):
        return run_python("-m", "halo_rlm.agent_cli", *args, cwd=HERE, env=env)

    built = agent("build-prompt", "--output", prompt, "--task-json", task, "--judge-result", judge)
    check(built.returncode == 0 and "task_id: task15" in prompt.read_text(encoding="utf-8"), "prompt build without API key")
    write_json(report, valid_report())
    check(agent("validate-report", report).returncode == 0, "schema validation")

    span = {"trace_id": "trace-1", "span_id": "span-1", "status": "STATUS_CODE_ERROR", "error": "test error"}
    write_jsonl(source, [span])
    write_jsonl(prepared, [span])
    write_json(manifest, {
        "schema_version": 3,
        "prepared_traces": [{
            "source": str(source), "selected": str(prepared), "prompt_path": str(prompt),
            "report_path": str(report), "manifest_path": str(manifest),
        }],
        "errors": [],
    })
    completed = agent("validate-report", report, "--manifest", manifest)
    check(completed.returncode == 0 and json.loads(completed.stdout)["validation"] == "complete", "manifest-aware validation")

    invalid = valid_report()
    invalid["diagnosis"]["error_findings"][0]["evidence"][0]["raw_log_excerpt"] = "fabricated"
    write_json(report, invalid)
    rejected = agent("validate-report", report, "--manifest", manifest)
    check(rejected.returncode == 2 and "not a verbatim substring" in rejected.stdout, "fabricated excerpt rejected")


def tool_cli(*args):
    return run_python("-m", "halo_rlm.tool_cli", *args, cwd=HERE)


def test_tool_cli() -> None:
    section("tool_cli production surface")
    listed = tool_cli(TRACE, "--list")
    names = {item["function"]["name"] for item in json.loads(listed.stdout)["tools"]}
    expected = {"get_dataset_overview", "query_traces", "count_traces", "view_trace", "view_spans", "search_trace", "search_span", "render_trace"}
    check(listed.returncode == 0 and names == expected, "only supported host-agent tools are exposed")
    overview = tool_cli(TRACE, "get_dataset_overview")
    check(json.loads(overview.stdout)["result"]["total_traces"] == 6, "overview CLI")
    viewed = tool_cli(TRACE, "view_trace", "--trace-id", "trace-ok-001")
    check(json.loads(viewed.stdout)["result"]["trace_id"] == "trace-ok-001", "named flags")
    error = tool_cli(TRACE, "view_trace", "--trace-id", "missing")
    check(error.returncode == 0 and "error" in json.loads(error.stdout), "tool errors remain JSON results")
    rejected = tool_cli(TRACE, "synthesize_traces")
    check(rejected.returncode == 2, "host-replaced tool is unavailable")


def event(timestamp, name, session, payload=None, *, role="main", parent=None):
    row = {"timestamp": timestamp, "event": name, "payload": payload or {}, "agent_role": role}
    if session is not None:
        row["session_id"] = session
    if parent is not None:
        row["parent_session_id"] = parent
    return row


def test_converter() -> None:
    section("trace converter")
    rows = [
        event("2026-07-31T12:00:00+08:00", "agent_start", "main", {"run_id": "main-run"}),
        event("2026-07-31T12:00:01+08:00", "tool_call", "main", {"tool_call_id": "delegate", "tool_name": "run_subagent", "args": {}}),
        event("2026-07-31T12:00:02+08:00", "session_started", "child", {"run_id": "child-run"}, role="subagent", parent="main"),
        event("2026-07-31T12:00:03+08:00", "tool_call", "child", {"tool_call_id": "bash", "tool_name": "bash", "args": {}}, role="subagent", parent="main"),
        event("2026-07-31T12:00:04+08:00", "tool_result", "child", {"tool_call_id": "bash", "tool_name": "bash", "is_error": True, "content": [{"type": "text", "text": "failed"}]}, role="subagent", parent="main"),
        event("2026-07-31T12:00:05+08:00", "session_ended", "child", {"run_id": "child-run", "status": "failed"}, role="subagent", parent="main"),
        event("2026-07-31T12:00:06+08:00", "tool_result", "main", {"tool_call_id": "delegate", "tool_name": "run_subagent", "is_error": True}),
        event("2026-07-31T12:00:07+08:00", "agent_end", "main", {"run_id": "main-run", "status": "completed"}),
    ]
    spans = convert_events(rows, "test-project", "fallback")
    roots = [span for span in spans if not span["parent_span_id"]]
    check({span["trace_id"] for span in roots} == {"main-run", "child-run"}, "main and subagent traces remain distinct")
    check(next(span for span in roots if span["trace_id"] == "child-run")["status"]["code"] == "STATUS_CODE_ERROR", "child failure is preserved")
    check(sum(span["name"] == "function.run_subagent" for span in spans) == 1, "paired delegate call/result produces one TOOL span")

    tool_failure = [
        event("2026-07-24T09:05:25+08:00", "tool_call", None, {"tool_call_id": "call-1", "tool_name": "bash", "args": {}}),
        event("2026-07-24T09:05:26+08:00", "tool_result", None, {"tool_call_id": "call-1", "tool_name": "bash", "is_error": False, "details": {"ok": False, "raw": {"error": "use TrashTool"}}}),
    ]
    converted = convert_events(tool_failure, "test-project", "fallback")
    tool = next(span for span in converted if span["attributes"].get("tool.name") == "bash")
    check(tool["status"]["code"] == "STATUS_CODE_ERROR", "details.ok=false remains a tool failure")


def test_prepare_trace_layout() -> None:
    section("prepare_trace output layout")
    source_root = TMP / "trace-input"
    output_root = TMP / "trace-output"
    task_dir = source_root / "task13"
    task_dir.mkdir(parents=True)
    nested = task_dir / "task13.jsonl"
    flat = source_root / "flat.jsonl"
    write_jsonl(nested, [{"trace_id": "nested", "span_id": "s1", "attributes": {}}])
    write_jsonl(flat, [{"trace_id": "flat", "span_id": "s2", "attributes": {}}])
    prepared = run_python(HERE / "prepare_trace.py", source_root, "--output-root", output_root)
    manifest = json.loads(prepared.stdout)
    entries = {Path(item["source"]): item for item in manifest["prepared_traces"]}
    check(prepared.returncode == 0 and len(entries) == 2, "directory preparation")
    nested_entry = entries[nested.resolve()]
    flat_entry = entries[flat.resolve()]
    check(Path(nested_entry["selected"]).parent == (output_root / "task13_halo").resolve(), "nested task directory mapping")
    check(Path(flat_entry["selected"]).parent == (output_root / "flat_halo").resolve(), "flat trace directory mapping")
    check(not Path(nested_entry["prompt_path"]).exists(), "prompt path is reserved, not pre-created")
    rerun = run_python(HERE / "prepare_trace.py", source_root, "--output-root", output_root)
    check(json.loads(rerun.stdout)["snapshot_jsonl_count"] == 2, "external output is excluded on rescan")
    rejected = run_python(HERE / "prepare_trace.py", source_root, "--output", source_root)
    check(rejected.returncode == 2, "ambiguous --output option is rejected")


def main() -> int:
    test_trace_store()
    test_report_contract()
    test_agent_cli()
    test_tool_cli()
    test_converter()
    test_prepare_trace_layout()
    print(f"\nALL {PASSED} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
