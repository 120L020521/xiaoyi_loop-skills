#!/usr/bin/env python3
"""Self-test suite for halo-rlm (no pytest required, plain asserts).

Run:
    python run_tests.py

Covers (per SPEC "self-test requirements"):
  - demo trace generation (>= 6 traces: success / OTel error / oversized /
    semantic failure)
  - TraceStore: overview counts, filters, pagination, view_trace oversized,
    search_trace hits + has_more, attribute truncation markers, flat
    projection key dropping, view_spans surgical cap, render_trace
  - AgentContext compaction correctness (>12 plain messages, >3 tool turn
    groups, no orphan tool messages, get_context_item retrieves originals,
    compaction failure does not interrupt)
  - Tool layer: error results instead of exceptions, run_code, depth-capped
    call_subagent registration
  - Full mock-demo engine run: root -> subagents -> report, <final/> stripped
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONVERTER_DIR = os.path.join(HERE, "halo-trace-converter")
sys.path.insert(0, CONVERTER_DIR)

import demo_make_traces  # noqa: E402
from halo_rlm.better_harness import (  # noqa: E402
    BETTER_HARNESS_COMPONENTS,
    DEFAULT_EDITABLE_SURFACES,
)
from halo_rlm.context import AgentContext, ContextItem  # noqa: E402
from halo_rlm.engine import EngineConfig, _Engine, run_engine, scripted_mock_for_demo  # noqa: E402
from halo_rlm.llm_client import LLMClient  # noqa: E402
from halo_rlm.models import TraceFilters  # noqa: E402
from halo_rlm.prompts import (  # noqa: E402
    FINAL_SENTINEL,
    SYSTEM_PROMPT,
    build_trace_only_prompt,
)
from halo_rlm.report_contract import (  # noqa: E402
    REPORT_SCHEMA_VERSION,
    REPORT_STRUCTURE_GUIDANCE,
    build_report,
    normalize_json_report,
    render_report_example,
)
from halo_rlm.tools import ToolRegistry  # noqa: E402
from halo_rlm.trace_store import (  # noqa: E402
    TraceStore,
    _DISCOVERY_ATTR_TRUNCATION_CHARS,
    _SURGICAL_ATTR_TRUNCATION_CHARS,
)
from converter_core.conversion import convert_events  # noqa: E402

_PASSED = 0


def check(cond: bool, label: str) -> None:
    global _PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _PASSED += 1
    print(f"  ok: {label}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _valid_report_summary() -> dict:
    return {
        "task_id": "task-test",
        "task": "测试任务内容",
        "trace_ids": ["trace-1"],
    }


def _valid_change(
    component: str = "prompt",
    target: str = "runner_skill.md",
    priority: str = "P0",
) -> dict:
    return {
        "priority": priority,
        "component": component,
        "target": target,
        "title": "测试修改",
        "error_refs": ["ERR1"],
        "problem": "测试问题说明",
        "implementation": "测试实施方案",
        "acceptance_criteria": ["相同输入必须通过验证。"],
        "expected_impact": "测试预期影响",
    }


def _valid_evidence() -> dict:
    return {
        "source": "TRACE",
        "reference": "span-1",
        "tool": "test_tool",
        "fact": "测试工具调用发生错误。",
        "error": "test error",
    }


def _valid_error() -> dict:
    return {
        "error_id": "ERR1",
        "priority": "P0",
        "category": "TOOL_FAILURE",
        "title": "测试工具失败",
        "occurrence_count": 1,
        "summary": "测试工具调用没有成功完成。",
        "evidence": [_valid_evidence()],
        "root_cause": "测试工具参数与运行环境不兼容。",
        "recovery_status": "UNRECOVERED",
        "impact": "测试任务无法完成预期操作。",
    }


# ----------------------------------------------------------------------
# Fixture: generate the demo dataset once
# ----------------------------------------------------------------------

TMPDIR = tempfile.mkdtemp(prefix="halo_rlm_tests_")
TRACE_PATH = os.path.join(TMPDIR, "traces.jsonl")


def build_dataset() -> TraceStore:
    demo_make_traces.main(["demo_make_traces.py", TRACE_PATH])
    return TraceStore(TRACE_PATH)


# ----------------------------------------------------------------------
# 1) Demo dataset shape
# ----------------------------------------------------------------------


def test_dataset_shape(store: TraceStore) -> None:
    section("demo dataset shape")
    check(len(store.trace_ids) >= 6, ">= 6 traces generated")
    check(store.total_spans >= 10, ">= 10 spans generated")
    ov = store.get_overview()
    check(ov["error_trace_count"] >= 2, "has OTel-error traces")
    check(
        store.count_traces(TraceFilters(regex_pattern="success=false"))["total"] >= 1,
        "has semantic-failure (success=false) trace",
    )
    check(
        any(t == "trace-big-003" for t in store.trace_ids),
        "has oversized-budget trace",
    )
    check(os.path.exists(store.index_cache_path), "sidecar trace index written")
    cached = TraceStore(TRACE_PATH)
    check(
        cached.trace_ids == store.trace_ids and cached.total_spans == store.total_spans,
        "sidecar trace index reload preserves dataset",
    )


# ----------------------------------------------------------------------
# 2) TraceStore
# ----------------------------------------------------------------------


def test_overview(store: TraceStore) -> None:
    section("TraceStore.get_overview")
    ov = store.get_overview()
    check(ov["total_traces"] == 6, "overview total_traces == 6")
    check(ov["total_spans"] == store.total_spans, "overview total_spans matches index")
    check(ov["error_trace_count"] == 3, "overview error_trace_count == 3 (err-002, big-003, err-006)")
    check("shopping-app" in ov["service_names"], "service_names includes shopping-app")
    check("gpt-4o" in ov["model_names"], "model_names includes gpt-4o")
    check("payment-agent" in ov["agent_names"], "agent_names includes payment-agent")
    check(
        "gpt-inference-only" in ov["model_names"],
        "model_names recognizes inference.llm.model_name",
    )
    check(
        "inference-shopping-agent" in ov["agent_names"],
        "agent_names recognizes inference.agent_name",
    )
    check(
        ov["earliest_start_time"] == "2024-06-01T10:00:00Z",
        "overview earliest_start_time is dataset min",
    )
    check(
        ov["latest_end_time"] > ov["earliest_start_time"],
        "overview latest_end_time after earliest_start_time",
    )
    check(ov["total_input_tokens"] > 0 and ov["total_output_tokens"] > 0, "token totals positive")
    check(ov["raw_jsonl_bytes"] == os.path.getsize(TRACE_PATH), "raw_jsonl_bytes == file size")
    check(len(ov["sample_trace_ids"]) <= 20, "sample_trace_ids capped at 20")
    check(set(ov["sample_trace_ids"]) == set(store.trace_ids), "sample ids cover all 6 traces")


def test_filters(store: TraceStore) -> None:
    section("TraceStore filters")
    check(store.count_traces(TraceFilters(has_errors=True))["total"] == 3, "has_errors=True -> 3")
    check(store.count_traces(TraceFilters(has_errors=False))["total"] == 3, "has_errors=False -> 3")
    check(store.count_traces(TraceFilters(model_names=["gpt-4o-mini"]))["total"] == 2, "model filter")
    check(store.count_traces(TraceFilters(service_names=["refund-service"]))["total"] == 1, "service filter")
    check(store.count_traces(TraceFilters(agent_names=["payment-agent"]))["total"] == 1, "agent filter")
    check(
        store.count_traces(TraceFilters(project_id="demo-project"))["total"] == 1,
        "inference.project_id filter",
    )
    check(store.count_traces(TraceFilters(regex_pattern="MaxTurnsExceeded"))["total"] == 1, "regex filter (lazy raw scan)")
    check(store.count_traces(TraceFilters(start_time_gte="2024-06-01T10:04:00Z"))["total"] == 2, "start_time_gte filter")
    check(store.count_traces(TraceFilters(end_time_lte="2024-06-01T10:01:00Z"))["total"] == 1, "end_time_lte filter")
    check(
        store.count_traces(TraceFilters(has_errors=True, model_names=["gpt-4o"]))["total"] == 2,
        "combined filters intersect",
    )


def test_query_pagination(store: TraceStore) -> None:
    section("TraceStore.query_traces pagination")
    page1 = store.query_traces(limit=2, offset=0)
    page2 = store.query_traces(limit=2, offset=2)
    page3 = store.query_traces(limit=2, offset=4)
    check(page1["total"] == 6, "query total == 6")
    ids = [t["trace_id"] for p in (page1, page2, page3) for t in p["traces"]]
    check(len(ids) == 6 and len(set(ids)) == 6, "pages partition all traces without overlap")
    t0 = page1["traces"][0]
    for field in (
        "trace_id", "span_count", "start_time", "end_time", "has_errors",
        "service_names", "model_names", "agent_names", "total_input_tokens",
        "total_output_tokens", "raw_jsonl_bytes",
    ):
        check(field in t0, f"TraceSummary has {field}")
    err = store.query_traces(TraceFilters(has_errors=True), limit=10)["traces"]
    check(all(t["has_errors"] for t in err), "filtered summaries all has_errors")


def test_view_trace_and_truncation(store: TraceStore) -> None:
    section("TraceStore.view_trace + truncation")
    v = store.view_trace("trace-ok-001")
    check(
        v["oversized"] is None and len(v["spans"]) == 3,
        "view_trace returns HALO TraceView shape",
    )
    attrs = v["spans"][0]["attributes"]
    check("__halo_dropped_flat_projections" in attrs, "flat projection drop marker present")
    check("llm.input_messages.0.role" not in attrs, "flat projection key dropped")
    check("mcp.tools.0.name" not in attrs, "mcp.tools flat key dropped")

    v5 = store.view_trace("trace-ok-005")
    long_ctx = v5["spans"][0]["attributes"]["long_context"]
    check(
        long_ctx.endswith("... [HALO truncated: original 5000 chars]"),
        "4KB discovery truncation marker exact",
    )
    check(len(long_ctx) < 5000, "truncated value shorter than original")
    check(
        long_ctx[: _DISCOVERY_ATTR_TRUNCATION_CHARS].startswith("CTX-"),
        "head slice preserved",
    )


def test_view_trace_oversized(store: TraceStore) -> None:
    section("TraceStore.view_trace oversized budget")
    big = store.view_trace("trace-big-003")
    check(big["spans"] == [], "oversized: spans withheld")
    check(isinstance(big["oversized"], dict), "oversized summary nested")
    summary = big["oversized"]
    check(summary["span_count"] == 60, "oversized span_count == 60")
    check(summary["truncated_response_bytes"] > summary["response_bytes_budget"], "response exceeded budget")
    check(summary["response_bytes_budget"] == 150_000, "budget == 150KB")
    check(
        summary["span_response_bytes_min"] <= summary["span_response_bytes_median"] <= summary["span_response_bytes_max"],
        "span byte stats ordered",
    )
    check(len(summary["top_span_names"]) <= 10, "top_span_names <= 10")
    check(summary["top_span_names"][0][1] == 12, "top span name count correct")
    check(summary["error_span_count"] == 1, "oversized error_span_count == 1")
    check("search_trace" in summary["recommendation"], "recommendation points to search_trace")


def test_view_spans(store: TraceStore) -> None:
    section("TraceStore.view_spans surgical reads")
    vs = store.view_spans("trace-big-003", ["span-003-00"])
    check(len(vs["spans"]) == 1, "view_spans returns requested span")
    huge = vs["spans"][0]["attributes"]["huge_single_attr"]
    check(
        huge.endswith("... [HALO truncated: original 20000 chars]"),
        "16KB surgical truncation marker exact",
    )
    check(len(huge) > 4096 + 100, "surgical cap higher than discovery cap")
    check(
        huge[: _SURGICAL_ATTR_TRUNCATION_CHARS].startswith("HUGE-"),
        "surgical head slice preserved",
    )
    vs2 = store.view_spans("trace-ok-001", ["span-001-a", "span-001-nope"])
    check(len(vs2["spans"]) == 1, "unknown span ids are silently skipped like HALO")
    try:
        store.view_spans("trace-ok-001", [f"s{i}" for i in range(201)])
        check(False, "view_spans >200 ids raises")
    except ValueError:
        check(True, "view_spans >200 ids raises")


def test_search(store: TraceStore) -> None:
    section("TraceStore.search_trace / search_span")
    s = store.search_trace("trace-err-002", "MaxTurnsExceeded", context_buffer_chars=60)
    check(s["match_count"] == 2, "search match_count == 2 (attr + status message)")
    check(s["returned_match_count"] == 2 and not s["has_more"], "all matches returned")
    m = s["matches"][0]
    for field in (
        "trace_id", "span_id", "span_index", "span_name", "kind", "status_code",
        "parent_span_id", "raw_jsonl_bytes", "match_text", "matched_context",
        "match_start_char", "match_end_char",
    ):
        check(field in m, f"SpanMatchRecord has {field}")
    check(m["match_text"] == "MaxTurnsExceeded", "match_text correct")
    check("MaxTurnsExceeded" in m["matched_context"], "context contains match")

    s2 = store.search_trace("trace-big-003", "large_payload", max_matches=5)
    check(s2["match_count"] == 60, "search counts all matches")
    check(s2["returned_match_count"] == 5 and s2["has_more"], "has_more when capped")

    s3 = store.search_span("trace-big-003", "span-003-00", r"HUGE-y+", context_buffer_chars=10)
    check(s3["match_count"] == 1, "search_span single-span match")
    check(s3["matches"][0]["span_id"] == "span-003-00", "search_span span id")

    try:
        store.search_trace("trace-ok-001", "([invalid")
        check(False, "invalid regex raises")
    except ValueError:
        check(True, "invalid regex raises ValueError")


def test_render_trace(store: TraceStore) -> None:
    section("TraceStore.render_trace")
    text = store.render_trace("trace-ok-001")
    check("trace_id: trace-ok-001" in text, "render includes trace id")
    check("agent.run" in text and "llm.chat_completion" in text, "render includes spans")
    check("gpt-4o-mini" in text, "render includes model names")
    truncated = store.render_trace("trace-big-003", budget=300)
    check(truncated.endswith("... [truncated]"), "render budget truncation marker")
    check(len(truncated) < 400, "render respects budget")


def test_unknown_ids(store: TraceStore) -> None:
    section("TraceStore unknown ids")
    for fn in (
        lambda: store.view_trace("nope"),
        lambda: store.view_spans("nope", ["x"]),
        lambda: store.search_trace("nope", "x"),
        lambda: store.render_trace("nope"),
    ):
        try:
            fn()
            check(False, "unknown trace_id raises KeyError")
        except KeyError:
            check(True, "unknown trace_id raises KeyError")


# ----------------------------------------------------------------------
# 3) AgentContext compaction
# ----------------------------------------------------------------------


def _build_compaction_context() -> AgentContext:
    ctx = AgentContext(compaction_model="mock-model", keep_last_messages=12, keep_last_turns=3)
    ctx.append(ContextItem(item_id="", role="system", content="SYS"))
    for i in range(15):  # > keep_last_messages=12
        role = "user" if i % 2 == 0 else "assistant"
        ctx.append(ContextItem(item_id="", role=role, content=f"plain message {i} " + "x" * 80))
    for g in range(5):  # > keep_last_turns=3
        ctx.append(
            ContextItem(
                item_id="",
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": f"call-{g}",
                        "type": "function",
                        "function": {"name": f"tool_{g}", "arguments": "{}"},
                    }
                ],
            )
        )
        ctx.append(
            ContextItem(
                item_id="",
                role="tool",
                content=f"result {g} " + "y" * 200,
                tool_call_id=f"call-{g}",
                name=f"tool_{g}",
            )
        )
    return ctx


def test_context_compaction() -> None:
    section("AgentContext compaction")
    client = LLMClient(mock_script=[{"content": "unused"}])  # compaction is canned in mock mode
    ctx = _build_compaction_context()
    ctx.compact_old_items(client)

    check(not ctx.items[0].is_compacted, "system item never compacted")

    plain = [
        it for it in ctx.items
        if it.role in ("user", "assistant") and not it.tool_calls and not it.is_compacted
    ]
    check(len(plain) == 12, "exactly keep_last_messages plain messages stay live")
    compacted_plain = [
        it for it in ctx.items
        if it.role in ("user", "assistant") and not it.tool_calls and it.is_compacted
    ]
    check(len(compacted_plain) == 3, "oldest 3 plain messages compacted")
    check(
        compacted_plain[0].content.startswith("plain message 0"),
        "original content retained in store",
    )

    # Tool turn groups: oldest 2 compacted, newest 3 live.
    live_groups = 0
    i = 0
    while i < len(ctx.items):
        it = ctx.items[i]
        if it.role == "assistant" and it.tool_calls:
            group = [it]
            j = i + 1
            while j < len(ctx.items) and ctx.items[j].role == "tool":
                group.append(ctx.items[j])
                j += 1
            compacted_flags = {x.is_compacted for x in group}
            check(len(compacted_flags) == 1, "tool turn group compacted atomically (no half compaction)")
            if not it.is_compacted:
                live_groups += 1
            i = j
        else:
            i += 1
    check(live_groups == 3, "exactly keep_last_turns tool turn groups stay live")

    # Rendering legality: no orphan tool messages.
    messages = ctx.to_messages()
    pending: set[str] = set()
    orphans = 0
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            pending = {tc["id"] for tc in msg["tool_calls"]}
        elif msg["role"] == "tool":
            if msg["tool_call_id"] not in pending:
                orphans += 1
            pending.discard(msg["tool_call_id"])
        elif msg["role"] == "assistant":
            pending = set()
    check(orphans == 0, "rendered messages have no orphan tool messages")

    compacted_tool_msgs = [
        m for m in messages if m["role"] == "assistant" and "Compacted tool result" in m.get("content", "")
    ]
    check(len(compacted_tool_msgs) == 2, "compacted tool results render as assistant messages")
    check(
        any("Compacted tool calls (id:" in m.get("content", "") for m in messages),
        "compacted assistant tool_calls render with summary",
    )
    check(
        any(m["role"] == "user" and "Compacted message (id:" in m.get("content", "") for m in messages),
        "compacted user message renders with summary",
    )

    # get_item retrieves the original + summary.
    target = compacted_plain[0]
    got = ctx.get_item(target.item_id)
    check(got is not None and got.content == target.content, "get_item returns original content")
    check(got.compaction_summary is not None and len(got.compaction_summary) > 0, "get_item returns compaction summary")


def test_context_compaction_failure_resilience() -> None:
    section("AgentContext compaction failure resilience")

    class BoomClient:
        def chat(self, **kwargs):
            raise RuntimeError("boom")

    ctx = _build_compaction_context()
    ctx.compact_old_items(BoomClient())  # must not raise
    check(
        all(not it.is_compacted for it in ctx.items),
        "failed compaction leaves items uncompacted and run intact",
    )
    # Rendering still valid with everything live.
    msgs = ctx.to_messages()
    check(len(msgs) == len(ctx.items), "all-live context renders 1:1")


def test_subagent_context_boundary(store: TraceStore) -> None:
    section("subagent context boundary")
    client = LLMClient(mock_script=[])
    engine = _Engine(
        store,
        client,
        EngineConfig(model="m", dataset_context="ROOT-ONLY-CONTEXT"),
    )
    check(
        "ROOT-ONLY-CONTEXT" in engine._render_system_prompt(0),
        "root receives caller dataset context",
    )
    check(
        "ROOT-ONLY-CONTEXT" not in engine._render_system_prompt(1),
        "subagent receives only delegated input and generic instructions",
    )


# ----------------------------------------------------------------------
# 4) Tool layer
# ----------------------------------------------------------------------


def test_tools(store: TraceStore) -> None:
    section("ToolRegistry")
    ctx = AgentContext(compaction_model="m")
    ctx.append(ContextItem(item_id="", role="user", content="hello"))
    reg = ToolRegistry(
        store=store,
        llm_client=LLMClient(mock_script=[{"content": "synth summary"}]),
        synthesis_model="mock-synth",
        context=ctx,
        depth=0,
        maximum_depth=2,
        subagent_handler=lambda text: {"child_agent_id": "sub-x", "answer": "ok", "turns_used": 1, "tool_calls_made": 0},
    )
    names = {t["function"]["name"] for t in reg.schemas()}
    expected = {
        "get_dataset_overview", "query_traces", "count_traces", "view_trace",
        "view_spans", "search_trace", "search_span", "synthesize_traces",
        "get_context_item", "call_subagent",
    }
    check(names == expected, "HALO tools registered; unsafe run_code disabled by default")

    reg_deep = ToolRegistry(
        store=store, llm_client=reg.llm_client, synthesis_model="m",
        context=ctx, depth=2, maximum_depth=2,
    )
    deep_names = {t["function"]["name"] for t in reg_deep.schemas()}
    check("call_subagent" not in deep_names, "call_subagent NOT registered at max depth")
    out = json.loads(reg_deep.execute("call_subagent", {"input": "x"}))
    check("error" in out, "defensive call_subagent check returns error at max depth")

    # Error results instead of exceptions.
    out = json.loads(reg.execute("view_trace", {"trace_id": "nope"}))
    check("error" in out, "unknown trace_id -> error result")
    out = json.loads(reg.execute("search_trace", {"trace_id": "trace-ok-001", "regex_pattern": "([bad"}))
    check("error" in out, "invalid regex -> error result")
    out = json.loads(reg.execute("no_such_tool", {}))
    check("error" in out, "unknown tool -> error result")

    # Normal calls.
    out = json.loads(reg.execute("count_traces", {"filters": {"has_errors": True}}))
    check(out == {"result": {"total": 3}}, "count_traces uses HALO result envelope")
    out = json.loads(reg.execute("get_dataset_overview", {}))
    check(out["result"]["total_traces"] == 6, "overview uses HALO result envelope")

    # synthesize_traces (mock LLM).
    out = json.loads(reg.execute("synthesize_traces", {"trace_ids": ["trace-ok-001", "trace-err-002"], "focus": "errors"}))
    check(out.get("summary") == "synth summary", "synthesize_traces returns model summary")

    # get_context_item.
    item_id = ctx.items[0].item_id
    out = json.loads(reg.execute("get_context_item", {"item_id": item_id}))
    check(out["content"] == "hello" and out["role"] == "user", "get_context_item returns stored item")
    out = json.loads(reg.execute("get_context_item", {"item_id": "missing"}))
    check("error" in out, "get_context_item unknown id -> error")

    # call_subagent passthrough.
    out = json.loads(reg.execute("call_subagent", {"input": "do thing"}))
    check(out["child_agent_id"] == "sub-x" and out["answer"] == "ok", "call_subagent returns child JSON")

    # Unsafe compatibility run_code requires explicit opt-in.
    reg_unsafe = ToolRegistry(
        store=store,
        llm_client=reg.llm_client,
        synthesis_model="m",
        context=ctx,
        depth=2,
        maximum_depth=2,
        enable_unsafe_run_code=True,
    )
    out = json.loads(reg_unsafe.execute("run_code", {"code": "print('hello sandbox')"}))
    check(out["stdout"].strip() == "hello sandbox" and out["exit_code"] == 0, "run_code executes and captures stdout")
    out = json.loads(reg_unsafe.execute("run_code", {"code": "import sys; sys.stderr.write('oops'); sys.exit(3)"}))
    check(out["exit_code"] == 3 and "oops" in out["stderr"], "run_code captures stderr + exit code")
    out = json.loads(reg_unsafe.execute("run_code", {"code": "print('x' * 50000)"}))
    check(len(out["stdout"]) <= 10_000, "run_code stdout truncated to 10000 chars")


# ----------------------------------------------------------------------
# 5) Full mock-demo engine run
# ----------------------------------------------------------------------


def test_mock_demo_engine(store: TraceStore) -> None:
    section("mock-demo engine run (root -> subagents -> report)")
    events: list[dict] = []
    report = run_engine(
        TRACE_PATH,
        "Diagnose errors you find and suggest fixes",
        config=EngineConfig(
            model="mock-model",
            mock_script=scripted_mock_for_demo(store.trace_ids[:2]),
        ),
        on_event=lambda e: events.append(e),
    )
    check(bool(report.strip()), "report is non-empty")
    check(FINAL_SENTINEL not in report, "<final/> sentinel stripped from report")
    report_obj = json.loads(report)
    check(report_obj["schema_version"] == REPORT_SCHEMA_VERSION, "report contains sectioned JSON synthesis")
    check(
        bool(json.loads(normalize_json_report(
            report,
            allowed_components=BETTER_HARNESS_COMPONENTS,
            allowed_targets=DEFAULT_EDITABLE_SURFACES,
        ))),
        "mock report satisfies the strict Chinese narrative contract",
    )
    check(
        "error_findings" in report_obj["diagnosis"]
        and "primary_failure_mode" in report_obj["diagnosis"]
        and "errors" not in report_obj,
        "error findings are nested under diagnosis",
    )

    agent_starts = [e for e in events if e["type"] == "agent_start"]
    check(any(e["agent_id"] == "root" and e["depth"] == 0 for e in agent_starts), "root agent started at depth 0")
    sub_starts = [e for e in agent_starts if e["depth"] == 1]
    check(len(sub_starts) == 2, "2 subagents spawned at depth 1")

    tool_calls = [(e["agent_id"], e["tool"]) for e in events if e["type"] == "tool_call"]
    check(("root", "get_dataset_overview") in tool_calls, "root called get_dataset_overview first")
    sub_view = [t for t in tool_calls if t[0].startswith("subagent") and t[1] == "view_trace"]
    sub_search = [t for t in tool_calls if t[0].startswith("subagent") and t[1] == "search_trace"]
    check(len(sub_view) == 1, "subagent A viewed a trace")
    check(len(sub_search) == 1, "subagent B searched a trace")
    check(sum(1 for t in tool_calls if t[1] == "call_subagent") == 2, "root issued 2 call_subagent calls")

    ends = {e["agent_id"]: e for e in events if e["type"] == "agent_end"}
    check(ends["root"]["turns_used"] == 5, "root used 5 turns")
    check(all(e["type"] != "max_turns_reached" for e in events), "no max-turns exhaustion")


def test_mock_demo_depth0() -> None:
    section("mock-demo with maximum_depth=0 (no subagents structurally)")
    script = [
        {"content": "", "tool_calls": [{"id": "c1", "name": "count_traces", "arguments": {}}]},
        {"content": f"Done. 6 traces.{chr(10)}{FINAL_SENTINEL}"},
    ]
    events: list[dict] = []
    report = run_engine(
        TRACE_PATH,
        "Count traces",
        config=EngineConfig(model="m", maximum_depth=0, mock_script=script),
        on_event=lambda e: events.append(e),
    )
    check(report.startswith("Done. 6 traces."), "depth-0 root completes")
    check(not any(e["type"] == "agent_start" and e["depth"] == 1 for e in events), "no subagent at maximum_depth=0")


def test_root_continuation_prompt() -> None:
    section("root continuation when no sentinel")
    script = [
        {"content": "thinking out loud, no sentinel"},  # root must be nudged
        {"content": f"final answer\n{FINAL_SENTINEL}"},
    ]
    report = run_engine(
        TRACE_PATH,
        "Say something",
        config=EngineConfig(model="m", maximum_depth=0, mock_script=script),
    )
    check(report == "final answer", "root nudged until <final/>; sentinel stripped")


def test_trace_only_outcome_prompt() -> None:
    section("trace-only outcome classification prompt")
    check(
        "identify the root AGENT span" in SYSTEM_PROMPT,
        "system prompt requires root AGENT classification first",
    )
    check(
        "unrelated later OK span is not recovery evidence" in SYSTEM_PROMPT,
        "system prompt rejects unrelated OK spans as recovery evidence",
    )
    check(
        "BOTH `span_count <= 40` AND `raw_jsonl_bytes <= 40_000`" in SYSTEM_PROMPT
        and "EITHER `span_count > 40` OR `raw_jsonl_bytes > 40_000`" in SYSTEM_PROMPT,
        "system prompt requires both small-trace thresholds before view_trace",
    )
    check(
        "report execution success, never task/judge success" in SYSTEM_PROMPT,
        "trace-only prompt separates execution success from task success",
    )
    prompt = build_trace_only_prompt("inspect retries")
    check(
        f'"schema_version":{REPORT_SCHEMA_VERSION}' in prompt
        and '"diagnosis"' in prompt
        and '"primary_failure_mode"' in prompt
        and '"error_findings"' in prompt
        and '"proposed_changes"' in prompt,
        "trace-only prompt includes the requested sectioned JSON report contract",
    )
    check(
        "inspect retries" in prompt,
        "trace-only prompt appends an optional diagnostic request",
    )
    check(
        "judge.score: MISSING (not supplied; trace-only context)" in prompt
        and "task_id: MISSING (not supplied; trace-only context)" in prompt
        and "editable_surfaces: runner_skill.md, workspace_bench_tools.ts" in prompt
        and "UPPER_SNAKE_CASE category" in prompt,
        "trace-only prompt marks missing context and uses unified constraints",
    )
    check(
        "TRAJECTORY EFFICIENCY" in prompt
        and "Distinguish necessary" in prompt
        and "Feed trace-supported trajectory inefficiencies into `proposed_changes`" in prompt
        and "Do not force an efficiency proposal" in prompt,
        "trace-only prompt diagnoses material path inefficiency and maps it to changes",
    )


def test_max_turns() -> None:
    section("maximum_turns termination")
    script = [{"content": "still working"} for _ in range(50)]  # never finalizes
    report = run_engine(
        TRACE_PATH,
        "Loop forever",
        config=EngineConfig(model="m", maximum_depth=0, maximum_turns=5, mock_script=script),
    )
    check("still working" in report, "max_turns returns last content")


def test_better_harness_component_validation() -> None:
    section("Unified v6 error and change validation")

    def expect_error(candidate: dict, expected: str, label: str) -> None:
        try:
            normalize_json_report(
                json.dumps(candidate),
                allowed_components=BETTER_HARNESS_COMPONENTS,
                allowed_targets=DEFAULT_EDITABLE_SURFACES,
            )
        except ValueError as exc:
            check(expected in str(exc), label)
            return
        check(False, label)

    report_example = render_report_example(
        BETTER_HARNESS_COMPONENTS,
        include_evaluator_context=True,
    )
    check(
        '"service_names"' not in report_example
        and "do not add ad-hoc fields" in REPORT_STRUCTURE_GUIDANCE,
        "strict report example does not solicit uncontracted metadata",
    )
    check(
        '"task_id":"task15"' in report_example
        and '"expected_output_files":["output.xlsx"]' in report_example
        and "Copy the resolved task_id and task from Context" in REPORT_STRUCTURE_GUIDANCE,
        "report example preserves task identity and expected output files",
    )
    check(
        '"error_findings"' in report_example
        and '"error_refs"' in report_example
        and '"acceptance_criteria"' in report_example,
        "report example uses error-finding-centered v6 structure",
    )
    report = build_report(
        report_summary=_valid_report_summary(),
        execution_classification="UNKNOWN",
        primary_failure_mode="测试工具参数与运行环境不兼容。",
        error_findings=[_valid_error()],
        proposed_changes=[
            _valid_change(),
            _valid_change("tool_impl", "workspace_bench_tools.ts", "P1"),
            _valid_change("tool_definition", "workspace_bench_tools.ts", "P2"),
        ],
    )
    valid = normalize_json_report(
        json.dumps(report),
        allowed_components=BETTER_HARNESS_COMPONENTS,
        allowed_targets=DEFAULT_EDITABLE_SURFACES,
    )
    check(
        list(json.loads(valid)["diagnosis"]) == [
            "execution_classification",
            "primary_failure_mode",
            "error_findings",
        ],
        "strict validator normalizes the requested diagnosis field order",
    )
    check(
        bool(json.loads(valid)),
        "unified report validator accepts allowed component and target",
    )

    report["proposed_changes"] = [
        _valid_change("harness", "runner_skill.md"),
        _valid_change(),
        _valid_change("tool_impl", "workspace_bench_tools.ts"),
    ]
    expect_error(
        report,
        "component must be one of",
        "unified report validator rejects an unsupported component",
    )

    report["proposed_changes"] = [
        _valid_change("prompt", "trace instrumentation"),
        _valid_change(),
        _valid_change("tool_impl", "workspace_bench_tools.ts"),
    ]
    expect_error(
        report,
        "target must be one of",
        "unified report validator rejects a non-editable target",
    )

    report["proposed_changes"] = [
        _valid_change(),
        _valid_change("tool_impl", "workspace_bench_tools.ts", "P1"),
        _valid_change("tool_definition", "workspace_bench_tools.ts", "P2"),
    ]

    malformed = json.loads(json.dumps(report))
    malformed["diagnosis"]["execution_classification"] = "SUCCESS"
    expect_error(
        malformed,
        "execution_classification must be one of",
        "strict validator rejects an unknown execution classification",
    )

    malformed = json.loads(json.dumps(report))
    malformed["diagnosis"]["error_findings"][0]["summary"] = "English-only summary"
    expect_error(
        malformed,
        "must contain Simplified Chinese narrative text",
        "strict validator rejects English-only narrative content",
    )

    malformed = json.loads(json.dumps(report))
    malformed["diagnosis"]["primary_failure_mode"] = "English-only root cause"
    expect_error(
        malformed,
        "must contain Simplified Chinese narrative text",
        "strict validator requires a Chinese primary failure mode",
    )

    malformed = json.loads(json.dumps(report))
    del malformed["diagnosis"]["error_findings"][0]["evidence"][0]["error"]
    expect_error(
        malformed,
        "missing fields: error",
        "strict validator rejects incomplete evidence items",
    )

    malformed = json.loads(json.dumps(report))
    malformed["proposed_changes"][0]["error_refs"] = ["ERR99"]
    expect_error(
        malformed,
        "error_refs references unknown error ids",
        "strict validator binds every change to existing error findings",
    )

    malformed = json.loads(json.dumps(report))
    malformed["diagnosis"]["error_findings"][0]["evidence"][0]["source"] = "LOG"
    expect_error(
        malformed,
        "source must be one of",
        "strict validator rejects an unknown evidence source",
    )

    malformed = json.loads(json.dumps(report))
    malformed["unexpected"] = True
    expect_error(
        malformed,
        "root has unsupported fields: unexpected",
        "strict validator rejects unknown top-level fields",
    )


def test_agent_cli() -> None:
    section("No-API host-agent prompt and report helpers")
    agent_dir = os.path.join(TMPDIR, "agent-native")
    os.makedirs(agent_dir, exist_ok=True)
    task_path = os.path.join(agent_dir, "task.json")
    judge_path = os.path.join(agent_dir, "judge_result.json")
    prompt_path = os.path.join(agent_dir, "halo_prompt.txt")
    report_path = os.path.join(agent_dir, "halo_report.json")
    with open(task_path, "w", encoding="utf-8") as f:
        json.dump({"id": 15, "task": "Create result.txt", "output_files": ["result.txt"]}, f)
    with open(judge_path, "w", encoding="utf-8") as f:
        json.dump({"passed": False, "score": 0.25, "feedback": "incomplete"}, f)

    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)

    def run_agent(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "halo_rlm.agent_cli", *args],
            cwd=HERE,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    built = run_agent(
        "build-prompt",
        "--output",
        prompt_path,
        "--task-json",
        task_path,
        "--judge-result",
        judge_path,
    )
    check(built.returncode == 0, "agent prompt builder runs without an API key")
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read()
    check(
        "judge.score: 0.25" in prompt
        and "task_id: task15" in prompt
        and "task: Create result.txt" in prompt
        and 'expected_output_files: ["result.txt"]' in prompt
        and '"expected_output_files":["output.xlsx"]' in prompt,
        "agent prompt builder injects task identity and output context",
    )

    report = build_report(
        report_summary=_valid_report_summary(),
        execution_classification="UNKNOWN",
        primary_failure_mode="测试工具参数与运行环境不兼容。",
        error_findings=[_valid_error()],
        proposed_changes=[
            _valid_change(),
            _valid_change("tool_impl", "workspace_bench_tools.ts", "P1"),
            _valid_change("tool_definition", "workspace_bench_tools.ts", "P2"),
        ],
    )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    validated = run_agent("validate-report", report_path)
    check(validated.returncode == 0, "agent report validator runs without an API key")
    with open(report_path, encoding="utf-8") as f:
        normalized = json.load(f)
    check(
        normalized["schema_version"] == REPORT_SCHEMA_VERSION,
        "agent report validator rewrites canonical UTF-8 JSON",
    )

    source_path = os.path.join(agent_dir, "source.jsonl")
    prepared_path = os.path.join(agent_dir, "prepared.halo.jsonl")
    manifest_path = os.path.join(agent_dir, "halo-prepared-manifest.json")
    span_line = json.dumps({"trace_id": "trace-1", "span_id": "span-1"}) + "\n"
    for path in (source_path, prepared_path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(span_line)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "schema_version": 3,
            "prepared_traces": [{
                "source": source_path,
                "selected": prepared_path,
                "prompt_path": prompt_path,
                "report_path": report_path,
                "manifest_path": manifest_path,
            }],
            "errors": [],
        }, f)
    complete_validation = run_agent(
        "validate-report", report_path, "--manifest", manifest_path
    )
    complete_result = json.loads(complete_validation.stdout)
    check(
        complete_validation.returncode == 0
        and complete_result["validation"] == "complete",
        "agent report validator performs complete manifest-aware validation",
    )

    report["report_summary"]["trace_ids"] = ["missing-trace"]
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    invalid_reference = run_agent(
        "validate-report", report_path, "--manifest", manifest_path
    )
    check(
        invalid_reference.returncode == 2
        and "trace ids absent from the prepared trace" in invalid_reference.stdout,
        "complete validator rejects fabricated trace references",
    )
    report["report_summary"]["trace_ids"] = ["trace-1"]

    report["diagnosis"]["execution_classification"] = "FAILED"
    report["proposed_changes"] = [
        _valid_change()
    ]
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    rejected = run_agent("validate-report", report_path)
    check(
        rejected.returncode == 2
        and "FAILED diagnostic reports must contain exactly 3-5 proposed_changes"
        in rejected.stdout,
        "host-agent report validator enforces 3-5 changes for FAILED",
    )

    report["diagnosis"]["execution_classification"] = "SUCCEEDED_CLEANLY"
    report["proposed_changes"] = []
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    clean = run_agent("validate-report", report_path)
    check(
        clean.returncode == 0,
        "host-agent report validator allows zero changes for clean success",
    )


# ----------------------------------------------------------------------
# 6) LLMClient HTTP path against a local stub server (429 retry + parsing)
# ----------------------------------------------------------------------


def test_llm_client_http() -> None:
    section("LLMClient HTTP path (stub server: 429 retry, tools, parsing)")
    import http.server
    import threading

    requests: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            requests.append(body)
            if len(requests) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": {"message": "rate limited"}}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "count_traces",
                                                "arguments": '{"filters": {"has_errors": true}}',
                                            },
                                        },
                                        {
                                            "id": "call_2",
                                            "type": "function",
                                            "function": {"name": "get_dataset_overview", "arguments": "{}"},
                                        },
                                    ],
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    }
                ).encode("utf-8")
            )

        def log_message(self, *args):  # silence
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LLMClient(api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1")
        result = client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="test-model",
            tools=[{"type": "function", "function": {"name": "count_traces", "description": "x", "parameters": {"type": "object", "properties": {}}}}],
            temperature=0.3,
            max_tokens=123,
        )
        check(len(requests) == 2, "retried after HTTP 429 exactly once")
        body = requests[0]
        check(body["model"] == "test-model", "model sent in body")
        check(body.get("tools"), "tools sent in body")
        check(body.get("parallel_tool_calls") is True, "parallel_tool_calls enabled")
        check(body.get("temperature") == 0.3, "temperature sent")
        check(body.get("max_tokens") == 123, "max_tokens sent")
        check(len(result.tool_calls) == 2, "parallel tool_calls parsed")
        check(result.tool_calls[0].name == "count_traces", "tool call name parsed")
        check(result.tool_calls[0].arguments() == {"filters": {"has_errors": True}}, "tool call arguments parsed")
        check(result.finish_reason == "tool_calls", "finish_reason parsed")
    finally:
        server.shutdown()
        server.server_close()

    # Non-retryable error raises LLMError with body excerpt.
    from halo_rlm.llm_client import LLMError

    class Handler400(Handler):
        def do_POST(self):  # noqa: N802
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "bad request"}}')

    server2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler400)
    threading.Thread(target=server2.serve_forever, daemon=True).start()
    try:
        client2 = LLMClient(api_key="k", base_url=f"http://127.0.0.1:{server2.server_address[1]}/v1")
        try:
            client2.chat(messages=[{"role": "user", "content": "x"}], model="m")
            check(False, "HTTP 400 raises LLMError")
        except LLMError as e:
            check("bad request" in str(e), "LLMError carries response body excerpt")
    finally:
        server2.shutdown()
        server2.server_close()

    no_key_client = LLMClient(api_key="")
    try:
        no_key_client.chat(messages=[{"role": "user", "content": "x"}], model="m")
        check(False, "missing API key does not silently enter mock mode")
    except LLMError:
        check(True, "missing API key fails unless mock mode is explicit")


# ----------------------------------------------------------------------
# 7) Agent-driven tool CLI (subprocess)
# ----------------------------------------------------------------------


def _run_tool_cli(*cli_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "halo_rlm.tool_cli", *cli_args],
        cwd=HERE, capture_output=True, text=True, timeout=120,
    )


def test_tool_cli() -> None:
    section("agent-driven tool CLI")

    proc = _run_tool_cli(TRACE_PATH, "--list")
    check(proc.returncode == 0, "--list exit 0")
    names = {t["function"]["name"] for t in json.loads(proc.stdout)["tools"]}
    expected = {
        "get_dataset_overview", "query_traces", "count_traces", "view_trace",
        "view_spans", "search_trace", "search_span", "render_trace",
    }
    check(names == expected, f"--list exposes exactly the agent-driven tools (got {sorted(names)})")
    check("call_subagent" not in names and "synthesize_traces" not in names,
          "host-replaced tools not exposed")

    proc = _run_tool_cli(TRACE_PATH, "get_dataset_overview")
    check(proc.returncode == 0, "overview exit 0")
    ov = json.loads(proc.stdout)["result"]
    check(ov["total_traces"] == 6 and ov["total_spans"] == 71, "overview counts via CLI")
    check(len(ov["sample_trace_ids"]) > 0, "overview sample ids via CLI")

    proc = _run_tool_cli(TRACE_PATH, "view_trace", "--trace-id", "trace-ok-001")
    check(
        json.loads(proc.stdout)["result"]["trace_id"] == "trace-ok-001",
        "view_trace accepts the quote-safe --trace-id flag",
    )

    proc = _run_tool_cli(
        TRACE_PATH,
        "search_trace",
        "--trace-id",
        "trace-err-002",
        "--regex-pattern",
        "STATUS_CODE_ERROR",
    )
    named_search = json.loads(proc.stdout)["result"]
    check(
        named_search["match_count"] >= 1,
        "search_trace accepts named flags without nested JSON",
    )

    proc = _run_tool_cli(
        TRACE_PATH,
        "view_spans",
        "--trace-id",
        "trace-ok-001",
        "--span-id",
        "span-001-a",
        "--span-id",
        "span-001-b",
    )
    check(
        len(json.loads(proc.stdout)["result"]["spans"]) == 2,
        "view_spans maps repeated --span-id flags to span_ids",
    )

    proc = _run_tool_cli(TRACE_PATH, "count_traces", "--args", '{"filters": {"has_errors": true}}')
    check(json.loads(proc.stdout)["result"]["total"] == 3, "filtered count via CLI")

    proc = _run_tool_cli(TRACE_PATH, "search_trace", "--args",
                         '{"trace_id": "trace-err-002", "regex_pattern": "STATUS_CODE_ERROR"}')
    res = json.loads(proc.stdout)["result"]
    check(res["match_count"] >= 1 and res["matches"][0]["trace_id"] == "trace-err-002",
          "search_trace via CLI")

    proc = _run_tool_cli(TRACE_PATH, "view_trace", "--args", '{"trace_id": "no-such-trace"}')
    check(proc.returncode == 0 and "error" in json.loads(proc.stdout),
          "tool-level error returned as JSON with exit 0")

    proc = _run_tool_cli(TRACE_PATH, "synthesize_traces", "--args", "{}")
    check(proc.returncode == 2 and "error" in json.loads(proc.stdout),
          "host-replaced tool rejected with exit 2")

    proc = _run_tool_cli(TRACE_PATH, "view_trace", "--args", "not-json")
    check(proc.returncode == 2 and "error" in json.loads(proc.stdout),
          "invalid --args rejected with exit 2")

    proc = _run_tool_cli(
        TRACE_PATH,
        "view_trace",
        "--trace-id",
        "trace-ok-001",
        "--args",
        '{"trace_id":"trace-ok-001"}',
    )
    check(
        proc.returncode == 2
        and "do not combine" in json.loads(proc.stdout)["error"],
        "named flags cannot be mixed with --args",
    )

    proc = _run_tool_cli(
        TRACE_PATH,
        "render_trace",
        "--trace-id",
        "trace-err-002",
        "--budget",
        "400",
    )
    check(proc.returncode == 0 and proc.stdout.startswith("trace_id: trace-err-002"),
          "render_trace prints bounded plain text")
    check(len(proc.stdout) <= 500, "render_trace budget honored")


def test_model_cli_removed() -> None:
    section("external model CLI removal")
    check(
        not os.path.exists(os.path.join(HERE, "halo_rlm", "cli.py")),
        "model-backed halo_rlm.cli entry is absent",
    )
    check(
        not os.path.exists(os.path.join(HERE, "halo-rlm")),
        "legacy halo-rlm shim is absent",
    )


def test_converter_preserves_tool_failure() -> None:
    section("trace preparation preserves tool failures")
    input_dir = os.path.join(TMPDIR, "converter-regression")
    os.makedirs(input_dir, exist_ok=True)
    raw_path = os.path.join(input_dir, "case.jsonl")
    companion_path = os.path.join(input_dir, "case.halo.jsonl")
    rows = [
        {
            "event": "tool_call",
            "timestamp": "2026-07-24T09:05:25.856+08:00",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "bash",
                "args": {"command": "rm file.txt"},
            },
        },
        {
            "event": "tool_result",
            "timestamp": "2026-07-24T09:05:25.883+08:00",
            "payload": {
                "tool_call_id": "call-1",
                "tool_name": "bash",
                "is_error": False,
                "details": {
                    "backend": "os_api",
                    "ok": False,
                    "raw": {
                        "data": {
                            "success": False,
                            "error": "use TrashTool",
                        }
                    },
                },
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "status": "failed",
                                "errCode": "tool_failed",
                                "errMsg": "use TrashTool",
                            }
                        ),
                    }
                ],
            },
        },
    ]
    with open(raw_path, "w", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in rows))

    # A newer stale conversion must not override authoritative raw events.
    stale_span = {
        "trace_id": "stale",
        "span_id": "stale",
        "attributes": {"inference.observation_kind": "TOOL", "tool.name": "bash"},
        "status": {"code": "STATUS_CODE_OK", "message": ""},
    }
    with open(companion_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(stale_span) + "\n")
    os.utime(companion_path, (2_000_000_000, 2_000_000_000))

    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "prepare_trace.py"), input_dir],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(proc.returncode == 0, "prepare_trace accepts mixed raw and stale HALO pair")
    manifest = json.loads(proc.stdout)
    check(
        len(manifest["prepared_traces"]) == 1,
        "raw and stale HALO pair produce one diagnostic trace",
    )
    prompt_path = manifest["prepared_traces"][0]["prompt_path"]
    manifest_path = manifest["prepared_traces"][0]["manifest_path"]
    check(
        not os.path.exists(prompt_path),
        "preparation reserves prompt_path without creating a default prompt",
    )
    check(
        os.path.exists(manifest_path)
        and os.path.dirname(manifest_path) == os.path.dirname(prompt_path),
        "preparation writes the manifest beside the reserved prompt path",
    )

    task_path = os.path.join(TMPDIR, "prepare-task.json")
    judge_path = os.path.join(TMPDIR, "prepare-judge.json")
    with open(task_path, "w", encoding="utf-8") as stream:
        json.dump(
            {"task": "Create the prepared output", "output_files": ["result.txt"]},
            stream,
        )
    with open(judge_path, "w", encoding="utf-8") as stream:
        json.dump(
            {"passed": False, "score": 0.0, "feedback": "prepared output missing"},
            stream,
        )
    prompt_build = subprocess.run(
        [
            sys.executable,
            "-m",
            "halo_rlm.agent_cli",
            "build-prompt",
            "--output",
            prompt_path,
            "--task-json",
            task_path,
            "--judge-result",
            judge_path,
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        prompt_build.returncode == 0 and os.path.exists(prompt_path),
        "build-prompt creates the authoritative prompt after context is resolved",
    )
    with open(prompt_path, encoding="utf-8") as stream:
        prepared_prompt = stream.read()
    check(
        "Create the prepared output" in prepared_prompt
        and "prepared output missing" in prepared_prompt,
        "authoritative prompt includes finalized Task and Judge context",
    )
    with open(manifest["prepared_traces"][0]["selected"], encoding="utf-8") as stream:
        spans = [json.loads(line) for line in stream if line.strip()]
    check(
        all(
            "service.name"
            not in span.get("resource", {}).get("attributes", {})
            for span in spans
        ),
        "converter does not invent pi-agent as the task service",
    )
    tool = next(span for span in spans if span["attributes"].get("tool.name") == "bash")
    check(
        tool["status"]["code"] == "STATUS_CODE_ERROR",
        "details.ok=false becomes STATUS_CODE_ERROR",
    )
    check(
        "TrashTool" in tool["status"]["message"],
        "structured error status preserves the tool failure message",
    )


def test_converter_interleaved_sessions() -> None:
    section("converter interleaved main/subagent sessions")

    def event(
        timestamp: str,
        event_name: str,
        session_id: str | None,
        payload: dict[str, object] | None = None,
        *,
        role: str = "main",
        parent_session_id: str | None = None,
    ) -> dict[str, object]:
        row: dict[str, object] = {
            "agent_role": role,
            "event": event_name,
            "payload": payload or {},
            "timestamp": timestamp,
        }
        if session_id is not None:
            row["session_id"] = session_id
        if parent_session_id is not None:
            row["parent_session_id"] = parent_session_id
        return row

    rows = [
        event("2026-07-31T12:00:00+08:00", "agent_start", "main-session", {"run_id": "main-run"}),
        event(
            "2026-07-31T12:00:01+08:00",
            "tool_call",
            "main-session",
            {
                "tool_call_id": "delegate-1",
                "tool_name": "run_subagent",
                "args": {"prompt": "inspect the template"},
            },
        ),
        event(
            "2026-07-31T12:00:02+08:00",
            "session_started",
            "child-session",
            {"run_id": "child-run"},
            role="subagent",
            parent_session_id="main-session",
        ),
        event(
            "2026-07-31T12:00:03+08:00",
            "tool_call",
            "child-session",
            {"tool_call_id": "child-tool", "tool_name": "bash", "args": {}},
            role="subagent",
            parent_session_id="main-session",
        ),
        event(
            "2026-07-31T12:00:04+08:00",
            "session_lifecycle",
            None,
            {"parent_session_id": "main-session", "state": "child-running"},
        ),
        event(
            "2026-07-31T12:00:05+08:00",
            "tool_result",
            "child-session",
            {
                "tool_call_id": "child-tool",
                "tool_name": "bash",
                "is_error": True,
                "content": [{"type": "text", "text": "failed"}],
            },
            role="subagent",
            parent_session_id="main-session",
        ),
        event(
            "2026-07-31T12:00:06+08:00",
            "session_ended",
            "child-session",
            {"run_id": "child-run", "status": "failed"},
            role="subagent",
            parent_session_id="main-session",
        ),
        event(
            "2026-07-31T12:00:07+08:00",
            "subagent_completed",
            "child-session",
            {
                "child_session_id": "child-session",
                "parent_session_id": "main-session",
                "tool_call_id": "delegate-1",
            },
            role="subagent",
        ),
        event(
            "2026-07-31T12:00:08+08:00",
            "tool_result",
            "main-session",
            {
                "tool_call_id": "delegate-1",
                "tool_name": "run_subagent",
                "is_error": True,
                "content": [{"type": "text", "text": "child failed"}],
            },
        ),
        event(
            "2026-07-31T12:00:09+08:00",
            "agent_end",
            "main-session",
            {"run_id": "main-run", "status": "completed"},
        ),
    ]
    spans = convert_events(rows, "test-project", "fallback")
    roots = [span for span in spans if not span["parent_span_id"]]
    check(len(roots) == 2, "one interleaved file produces exactly two AGENT roots")
    roots_by_name = {root["name"]: root for root in roots}
    main_root = roots_by_name["agent.main"]
    child_root = roots_by_name["agent.subagent"]
    check(main_root["trace_id"] == "main-run", "main run_id remains the main trace id")
    check(child_root["trace_id"] == "child-run", "child run_id remains the child trace id")
    check(main_root["status"]["code"] == "STATUS_CODE_OK", "main terminal status remains successful")
    check(child_root["status"]["code"] == "STATUS_CODE_ERROR", "session_ended failure marks child root error")
    check(child_root["attributes"]["session.id"] == "child-session", "child session id is searchable")
    check(child_root["attributes"]["session.parent_id"] == "main-session", "child parent session id is searchable")
    check(child_root["attributes"]["agent.run_id"] == "child-run", "child run id is searchable")
    check(
        sum(1 for span in spans if span["name"] == "function.run_subagent") == 1,
        "main run_subagent call/result remains one TOOL span",
    )
    check(
        sum(1 for span in spans if span["name"] == "function.bash") == 1,
        "child tool call/result remains on the child trace",
    )
    check(
        "session_lifecycle" in main_root["attributes"].get("source.events", ""),
        "unassigned lifecycle metadata attaches to main without a pseudo trace",
    )

    repeated_rows = [
        event("2026-07-31T13:00:00+08:00", "session_started", "resumed-session", {}, role="subagent"),
        event(
            "2026-07-31T13:00:01+08:00",
            "tool_call",
            "resumed-session",
            {"tool_call_id": "first", "tool_name": "read", "args": {}},
            role="subagent",
        ),
        event(
            "2026-07-31T13:00:02+08:00",
            "tool_result",
            "resumed-session",
            {"tool_call_id": "first", "tool_name": "read", "is_error": False},
            role="subagent",
        ),
        event("2026-07-31T13:00:03+08:00", "session_ended", "resumed-session", {"status": "completed"}, role="subagent"),
        event("2026-07-31T13:01:00+08:00", "session_started", "resumed-session", {}, role="subagent"),
        event(
            "2026-07-31T13:01:01+08:00",
            "tool_call",
            "resumed-session",
            {"tool_call_id": "second", "tool_name": "write", "args": {}},
            role="subagent",
        ),
        event(
            "2026-07-31T13:01:02+08:00",
            "tool_result",
            "resumed-session",
            {"tool_call_id": "second", "tool_name": "write", "is_error": False},
            role="subagent",
        ),
        event("2026-07-31T13:01:03+08:00", "session_ended", "resumed-session", {"status": "completed"}, role="subagent"),
    ]
    resumed_spans = convert_events(repeated_rows, "test-project", "fallback")
    resumed_roots = [span for span in resumed_spans if not span["parent_span_id"]]
    check(len(resumed_roots) == 2, "two lifecycle runs in one session remain distinct traces")
    check(len({root["trace_id"] for root in resumed_roots}) == 2, "same-session resumed runs receive collision-free trace ids")
    check(
        all(root["attributes"]["session.id"] == "resumed-session" for root in resumed_roots),
        "resumed runs preserve the shared source session id",
    )


def test_prepare_trace_mirrors_sibling_output_tree() -> None:
    section("prepare_trace sibling output tree layout")
    input_dir = os.path.join(TMPDIR, "arbitrary-trace-source")
    output_dir = os.path.join(TMPDIR, "arbitrary-diagnostic-output")
    task_dir = os.path.join(input_dir, "task13")
    os.makedirs(task_dir, exist_ok=True)
    source_path = os.path.join(task_dir, "task13.jsonl")
    flat_source_path = os.path.join(input_dir, "flat.jsonl")
    span = {
        "trace_id": "layout-trace",
        "span_id": "layout-span",
        "attributes": {},
    }
    with open(source_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(span) + "\n")
    flat_span = dict(span, trace_id="flat-trace", span_id="flat-span")
    with open(flat_source_path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(flat_span) + "\n")

    proc = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "prepare_trace.py"),
            input_dir,
            "--output-root",
            output_dir,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(proc.returncode == 0, "prepare_trace accepts directory input")
    manifest = json.loads(proc.stdout)
    check(
        len(manifest["prepared_traces"]) == 2,
        "nested and flat task inputs produce two prepared traces",
    )
    check(
        manifest["output_directory"] == os.path.abspath(output_dir),
        "explicit output root is used without directory-name conventions",
    )
    entries = {entry["source"]: entry for entry in manifest["prepared_traces"]}
    entry = entries[os.path.abspath(source_path)]
    flat_entry = entries[os.path.abspath(flat_source_path)]
    expected_dir = os.path.abspath(os.path.join(output_dir, "task13_halo"))
    expected_flat_dir = os.path.abspath(os.path.join(output_dir, "flat_halo"))
    check(
        os.path.dirname(entry["selected"]) == expected_dir,
        "nested task maps to OUTPUT_ROOT/task13_halo",
    )
    check(
        entry["selected"] == os.path.join(expected_dir, "task13.halo.jsonl"),
        "prepared trace uses the logical task name",
    )
    check(
        entry["prompt_path"] == os.path.join(expected_dir, "halo_prompt.txt"),
        "reserved prompt path is inside task13_halo",
    )
    check(
        not os.path.exists(entry["prompt_path"])
        and not os.path.exists(flat_entry["prompt_path"]),
        "preparation does not create default prompts",
    )
    check(
        entry["report_path"] == os.path.join(expected_dir, "halo_report.json"),
        "report is written inside task13_halo",
    )
    check(
        entry["manifest_path"]
        == os.path.join(expected_dir, "halo-prepared-manifest.json"),
        "manifest is written inside task13_halo",
    )
    check(
        os.path.dirname(flat_entry["selected"]) == expected_flat_dir
        and flat_entry["selected"]
        == os.path.join(expected_flat_dir, "flat.halo.jsonl"),
        "flat trace maps to OUTPUT_ROOT/flat_halo",
    )
    check(
        set(manifest["manifest_paths"])
        == {entry["manifest_path"], flat_entry["manifest_path"]},
        "directory result lists both per-trace manifests",
    )
    check(
        not os.path.exists(os.path.join(task_dir, "task13_halo")),
        "source tree remains free of generated artifact directories",
    )

    file_run = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "prepare_trace.py"),
            source_path,
            "--output-root",
            output_dir,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    file_result = json.loads(file_run.stdout)
    check(
        file_run.returncode == 0
        and file_result["artifact_dir"] == expected_dir
        and file_result["trace_path"]
        == os.path.join(expected_dir, "task13.halo.jsonl"),
        "file mode returns the converted trace only from task13_halo",
    )

    custom_output_dir = os.path.join(TMPDIR, "custom_halo")
    custom_run = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "prepare_trace.py"),
            flat_source_path,
            "--output-root",
            custom_output_dir,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    custom_result = json.loads(custom_run.stdout)
    check(
        custom_run.returncode == 0
        and custom_result["artifact_dir"]
        == os.path.abspath(os.path.join(custom_output_dir, "flat_halo")),
        "--output-root is honored for single-file input",
    )

    rerun = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "prepare_trace.py"),
            input_dir,
            "--output-root",
            output_dir,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(rerun.returncode == 0, "prepare_trace can rescan after artifacts exist")
    rerun_manifest = json.loads(rerun.stdout)
    check(
        rerun_manifest["snapshot_jsonl_count"] == 2,
        "external output traces are excluded from later scans",
    )
    check(
        len(rerun_manifest["prepared_traces"]) == 2,
        "rescan still prepares exactly two logical traces",
    )

    rejected_output_run = subprocess.run(
        [
            sys.executable,
            os.path.join(HERE, "prepare_trace.py"),
            input_dir,
            "--output",
            input_dir,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        rejected_output_run.returncode == 2,
        "prepare_trace rejects the ambiguous --output option",
    )
    check(
        "unrecognized arguments: --output" in rejected_output_run.stderr,
        "prepare_trace exposes only the explicit --output-root option",
    )



# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------


def main() -> int:
    store = build_dataset()
    test_dataset_shape(store)
    test_overview(store)
    test_filters(store)
    test_query_pagination(store)
    test_view_trace_and_truncation(store)
    test_view_trace_oversized(store)
    test_view_spans(store)
    test_search(store)
    test_render_trace(store)
    test_unknown_ids(store)
    test_context_compaction()
    test_context_compaction_failure_resilience()
    test_subagent_context_boundary(store)
    test_tools(store)
    test_better_harness_component_validation()
    test_agent_cli()
    test_llm_client_http()
    test_mock_demo_engine(store)
    test_mock_demo_depth0()
    test_root_continuation_prompt()
    test_trace_only_outcome_prompt()
    test_max_turns()
    test_tool_cli()
    test_model_cli_removed()
    test_converter_preserves_tool_failure()
    test_converter_interleaved_sessions()
    test_prepare_trace_mirrors_sibling_output_tree()
    print(f"\nALL {_PASSED} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
