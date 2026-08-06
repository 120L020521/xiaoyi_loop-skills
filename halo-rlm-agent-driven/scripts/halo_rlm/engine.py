"""Recursive HALO agent loop (threading implementation).

Architecture (semantics copied from the reference RLM engine):
- Per-depth semaphores: ``{d: threading.Semaphore(max_parallel) for d in
  1..maximum_depth}``. Every depth has its own independent pool so a parent
  waiting on a child can never deadlock the pool. The depth-0 root is
  unrestricted.
- Synchronous agent loops on threads. When an agent's tool loop hits
  ``call_subagent`` it acquires the depth+1 semaphore, runs the child loop on
  a new thread, and returns the child's JSON result.
- Parallel tool_calls in a single assistant message are executed concurrently
  with a ThreadPoolExecutor.
- Every agent (root or sub) owns a compaction-aware AgentContext. A subagent's
  initial context is ``[system(rendered template), user(delegated input)]``.
- Root termination uses the ``<final/>`` sentinel protocol; subagents return
  their last assistant text directly.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .context import AgentContext, ContextItem
from .llm_client import LLMClient
from .models import ToolCall
from .prompts import (
    DATASET_CONTEXT_PROMPT_SECTION_TEMPLATE,
    FINAL_SENTINEL,
    ROOT_SYSTEM_PROMPT_TEMPLATE,
    SUBAGENT_SYSTEM_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
)
from .report_contract import build_report
from .tools import ToolRegistry
from .trace_store import TraceStore


@dataclass
class EngineConfig:
    model: str = "gpt-4o-mini"
    synthesis_model: Optional[str] = None
    compaction_model: Optional[str] = None
    maximum_depth: int = 2
    maximum_turns: int = 20
    maximum_parallel_subagents: int = 4
    keep_last_messages: int = 12
    keep_last_turns: int = 3
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    dataset_context: Optional[str] = None
    enable_unsafe_run_code: bool = False
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    mock_script: Optional[list[dict[str, Any]]] = None

    def __post_init__(self) -> None:
        if self.synthesis_model is None:
            self.synthesis_model = self.model
        if self.compaction_model is None:
            self.compaction_model = self.model


def _strip_final_sentinel(text: str) -> str:
    """Remove the <final/> sentinel (its own line and any inline occurrence)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip() != FINAL_SENTINEL]
    return "\n".join(lines).replace(FINAL_SENTINEL, "").strip()


def _dataset_context_section(config: EngineConfig) -> str:
    if not config.dataset_context:
        return ""
    return DATASET_CONTEXT_PROMPT_SECTION_TEMPLATE.format(
        dataset_context=config.dataset_context
    )


class _Engine:
    def __init__(
        self,
        store: TraceStore,
        client: LLMClient,
        config: EngineConfig,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.store = store
        self.client = client
        self.config = config
        self.on_event = on_event
        # Per-depth pools: depth d in 1..maximum_depth gets its own semaphore.
        self._semaphores = {
            d: threading.Semaphore(config.maximum_parallel_subagents)
            for d in range(1, config.maximum_depth + 1)
        }
        self._id_lock = threading.Lock()
        self._id_counter = 0

    # ------------------------------------------------------------------
    # Events / ids
    # ------------------------------------------------------------------

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass  # progress reporting must never break the run

    def _next_agent_id(self) -> str:
        with self._id_lock:
            self._id_counter += 1
            return f"subagent-{self._id_counter}"

    # ------------------------------------------------------------------
    # Subagent spawning
    # ------------------------------------------------------------------

    def _spawn_subagent(self, parent_agent_id: str, parent_depth: int, input_text: str) -> dict[str, Any]:
        child_depth = parent_depth + 1
        child_id = self._next_agent_id()
        semaphore = self._semaphores.get(child_depth)
        if semaphore is None:
            # Defensive: call_subagent is not registered at max depth.
            return {
                "child_agent_id": child_id,
                "answer": (
                    f"error: cannot spawn subagent at depth {child_depth} "
                    f"(maximum_depth={self.config.maximum_depth})"
                ),
                "turns_used": 0,
                "tool_calls_made": 0,
            }

        box: dict[str, Any] = {}

        def _target() -> None:
            try:
                box["result"] = self._run_agent(
                    agent_id=child_id,
                    depth=child_depth,
                    user_input=input_text,
                    parent_agent_id=parent_agent_id,
                )
            except Exception as e:  # noqa: BLE001 - child failure is data, not control flow
                box["error"] = e

        semaphore.acquire()
        thread = threading.Thread(target=_target, name=f"halo-{child_id}", daemon=True)
        try:
            thread.start()
            thread.join()
        finally:
            semaphore.release()

        if "error" in box:
            return {
                "child_agent_id": child_id,
                "answer": f"subagent {child_id} failed: {type(box['error']).__name__}: {box['error']}",
                "turns_used": 0,
                "tool_calls_made": 0,
            }
        result = box.get("result") or {}
        return {
            "child_agent_id": child_id,
            "answer": result.get("answer", ""),
            "turns_used": result.get("turns_used", 0),
            "tool_calls_made": result.get("tool_calls_made", 0),
        }

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool_calls(
        self, registry: ToolRegistry, tool_calls: list[ToolCall]
    ) -> list[str]:
        """Execute the tool calls of one assistant message (parallel if >1),
        returning result strings in the same order as the calls."""
        if len(tool_calls) == 1:
            call = tool_calls[0]
            return [registry.execute(call.name, call.arguments())]
        with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
            return list(
                pool.map(lambda c: registry.execute(c.name, c.arguments()), tool_calls)
            )

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    def _render_system_prompt(self, depth: int) -> str:
        # Match HALOAgent's context boundary: only the root receives caller
        # context. A child sees the generic subagent prompt plus the exact
        # delegated input supplied by its parent.
        section = _dataset_context_section(self.config) if depth == 0 else ""
        if depth == 0:
            return ROOT_SYSTEM_PROMPT_TEMPLATE.format(
                maximum_depth=self.config.maximum_depth,
                maximum_parallel_subagents=self.config.maximum_parallel_subagents,
                system_prompt=SYSTEM_PROMPT,
                dataset_context_section=section,
            )
        return SUBAGENT_SYSTEM_PROMPT_TEMPLATE.format(
            depth=depth,
            maximum_depth=self.config.maximum_depth,
            maximum_parallel_subagents=self.config.maximum_parallel_subagents,
            system_prompt=SYSTEM_PROMPT,
            dataset_context_section=section,
        )

    def _run_agent(
        self,
        agent_id: str,
        depth: int,
        user_input: str,
        parent_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        config = self.config
        context = AgentContext(
            items=[
                ContextItem(
                    item_id="", role="system", content=self._render_system_prompt(depth)
                ),
                ContextItem(item_id="", role="user", content=user_input),
            ],
            compaction_model=config.compaction_model or config.model,
            keep_last_messages=config.keep_last_messages,
            keep_last_turns=config.keep_last_turns,
        )
        registry = ToolRegistry(
            store=self.store,
            llm_client=self.client,
            synthesis_model=config.synthesis_model or config.model,
            context=context,
            depth=depth,
            maximum_depth=config.maximum_depth,
            subagent_handler=(
                lambda text: self._spawn_subagent(agent_id, depth, text)
            ),
            enable_unsafe_run_code=config.enable_unsafe_run_code,
        )
        tool_schemas = registry.schemas()

        self._emit(
            {
                "type": "agent_start",
                "agent_id": agent_id,
                "parent_agent_id": parent_agent_id,
                "depth": depth,
                "input_preview": (user_input or "")[:200],
            }
        )

        turns_used = 0
        tool_calls_made = 0
        last_content = ""
        while turns_used < config.maximum_turns:
            turns_used += 1
            self._emit(
                {"type": "turn", "agent_id": agent_id, "depth": depth, "turn": turns_used}
            )
            result = self.client.chat(
                messages=context.to_messages(),
                model=config.model,
                tools=tool_schemas,
                temperature=config.temperature,
                max_tokens=config.max_output_tokens,
            )
            last_content = result.content or ""
            context.append(
                ContextItem(
                    item_id="",
                    role="assistant",
                    content=result.content or "",
                    tool_calls=[tc.to_openai_dict() for tc in result.tool_calls] or None,
                )
            )

            if result.tool_calls:
                tool_calls_made += len(result.tool_calls)
                for call in result.tool_calls:
                    self._emit(
                        {
                            "type": "tool_call",
                            "agent_id": agent_id,
                            "depth": depth,
                            "turn": turns_used,
                            "tool": call.name,
                            "arguments_preview": (call.arguments_json or "")[:200],
                        }
                    )
                results = self._execute_tool_calls(registry, result.tool_calls)
                for call, result_str in zip(result.tool_calls, results):
                    context.append(
                        ContextItem(
                            item_id="",
                            role="tool",
                            content=result_str,
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    self._emit(
                        {
                            "type": "tool_result",
                            "agent_id": agent_id,
                            "depth": depth,
                            "turn": turns_used,
                            "tool": call.name,
                            "result_bytes": len(result_str.encode("utf-8")),
                        }
                    )
                try:
                    context.compact_old_items(self.client)
                except Exception:
                    pass  # compaction must never interrupt the loop
                continue

            # No tool calls.
            if depth == 0:
                if FINAL_SENTINEL in last_content:
                    answer = _strip_final_sentinel(last_content)
                    self._emit_end(agent_id, depth, turns_used, tool_calls_made)
                    return {
                        "answer": answer,
                        "turns_used": turns_used,
                        "tool_calls_made": tool_calls_made,
                    }
                context.append(
                    ContextItem(
                        item_id="",
                        role="user",
                        content=(
                            "You have not finished yet. Continue working with the "
                            "tools, or produce your final answer now and end that "
                            f"message with a single line containing only: {FINAL_SENTINEL}"
                        ),
                    )
                )
                continue
            # Subagent: the last assistant text is the answer.
            self._emit_end(agent_id, depth, turns_used, tool_calls_made)
            return {
                "answer": last_content,
                "turns_used": turns_used,
                "tool_calls_made": tool_calls_made,
            }

        # maximum_turns exhausted.
        self._emit(
            {
                "type": "max_turns_reached",
                "agent_id": agent_id,
                "depth": depth,
                "turns_used": turns_used,
            }
        )
        self._emit_end(agent_id, depth, turns_used, tool_calls_made)
        return {
            "answer": _strip_final_sentinel(last_content)
            or "[agent stopped: maximum_turns reached without a final answer]",
            "turns_used": turns_used,
            "tool_calls_made": tool_calls_made,
        }

    def _emit_end(self, agent_id: str, depth: int, turns: int, tool_calls: int) -> None:
        self._emit(
            {
                "type": "agent_end",
                "agent_id": agent_id,
                "depth": depth,
                "turns_used": turns,
                "tool_calls_made": tool_calls,
            }
        )


# ----------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------


def run_engine(
    trace_path: str,
    prompt: str,
    config: Optional[EngineConfig] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
) -> str:
    """Run the recursive HALO analysis loop; returns the final report string."""
    config = config or EngineConfig()
    store = TraceStore(trace_path)
    client = LLMClient(
        api_key=config.api_key,
        base_url=config.base_url,
        mock_script=config.mock_script,
    )
    engine = _Engine(store, client, config, on_event)
    engine._emit({"type": "engine_start", "trace_path": trace_path, "prompt": prompt})
    result = engine._run_agent(agent_id="root", depth=0, user_input=prompt)
    report = result["answer"]
    engine._emit({"type": "engine_end", "report_chars": len(report)})
    return report


# ----------------------------------------------------------------------
# Scripted mock for demos / tests (no API key required)
# ----------------------------------------------------------------------


def scripted_mock_for_demo(trace_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Build a deterministic mock script simulating a full recursive run:
    root -> get_dataset_overview -> (parallel leaf calls) -> spawn subagent A
    -> A inspects trace A -> spawn subagent B -> B inspects trace B -> root
    aggregates and emits <final/>.

    Only one agent consumes the mock queue at a time (root waits for each
    subagent), so the FIFO script is fully deterministic. Root turn 2 still
    exercises the parallel tool_calls path with two leaf calls.
    """
    tids = list(trace_ids or []) or ["trace-ok-001", "trace-err-002"]
    tid_a = tids[0]
    tid_b = tids[1] if len(tids) > 1 else tids[0]

    def _tc(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        return {"id": call_id, "name": name, "arguments": arguments}

    sub_answer_a = (
        "Subagent finding A: I inspected the delegated trace. It contains "
        "application spans with token counts; no OTel error status, but I "
        "checked for semantic failure markers as instructed."
    )
    sub_answer_b = (
        "Subagent finding B: I inspected the delegated trace. I searched for "
        "STATUS_CODE_ERROR and semantic markers (success=false) and report the "
        "matching spans with their surrounding context."
    )

    return [
        # Root turn 1: discovery.
        {
            "content": "",
            "tool_calls": [_tc("get_dataset_overview", {}, "root-call-1")],
        },
        # Root turn 2: two parallel leaf calls (exercises ThreadPoolExecutor).
        {
            "content": "",
            "tool_calls": [
                _tc("query_traces", {"limit": 5}, "root-call-2a"),
                _tc(
                    "count_traces",
                    {"filters": {"has_errors": True}},
                    "root-call-2b",
                ),
            ],
        },
        # Root turn 3: delegate trace A inspection.
        {
            "content": "",
            "tool_calls": [
                _tc(
                    "call_subagent",
                    {
                        "input": (
                            f"Inspect trace {tid_a}: view it, look for OTel "
                            "errors and semantic failure markers, and report."
                        )
                    },
                    "root-call-3",
                )
            ],
        },
        # Subagent A: view the trace, then answer.
        {
            "content": "",
            "tool_calls": [_tc("view_trace", {"trace_id": tid_a}, "sub-call-a")],
        },
        {"content": sub_answer_a},
        # Root turn 4: delegate trace B inspection.
        {
            "content": "",
            "tool_calls": [
                _tc(
                    "call_subagent",
                    {
                        "input": (
                            f"Inspect trace {tid_b}: search for STATUS_CODE_ERROR "
                            "and semantic failure markers, and report."
                        )
                    },
                    "root-call-4",
                )
            ],
        },
        # Subagent B: search the trace, then answer.
        {
            "content": "",
            "tool_calls": [
                _tc(
                    "search_trace",
                    {"trace_id": tid_b, "regex_pattern": "STATUS_CODE_ERROR"},
                    "sub-call-b",
                )
            ],
        },
        {"content": sub_answer_b},
        # Root turn 5: aggregate and finish with the sentinel.
        {
            "content": (
                json.dumps(
                    build_report(
                        report_summary={
                            "title": "HALO RLM DIAGNOSTIC REPORT",
                            "protocol": "HALO RLM agent-driven",
                            "trace_ids": [tid_a, tid_b],
                        },
                        execution_classification="UNKNOWN",
                        primary_failure_mode="模拟数据缺少明确的根执行终止证据",
                        conclusion="已完成确定性的递归诊断演示，但模拟证据不足以确定最终执行状态。",
                        evidence_chain=[{
                            "priority": "P0",
                            "trace_id": tid_a,
                            "span_id": "",
                            "timestamp": "",
                            "operation": "recursive inspection",
                            "tool_name": "call_subagent",
                            "arguments": f"trace_ids={tid_a},{tid_b}",
                            "result": f"{sub_answer_a}\n{sub_answer_b}",
                            "error": "",
                            "recovery": "",
                            "impact": "两个子代理均返回了检查结果，但模拟数据未提供充分的终止证据。",
                            "occurrence_count": 2,
                        }],
                        proposed_changes=[
                            {
                                "component": "prompt",
                                "priority": "P0",
                                "title": "要求记录根执行终止证据",
                                "problem": "模拟诊断缺少可用于确定最终状态的根执行终止信息。",
                                "implementation": "在诊断提示中要求优先读取并引用根 AGENT span 的终止状态。",
                                "expected_impact": "减少因终止证据不足而产生的 UNKNOWN 分类。",
                                "target": "runner_skill.md",
                            },
                            {
                                "component": "tool_impl",
                                "priority": "P1",
                                "title": "补充根 span 摘要",
                                "problem": "当前模拟工具结果没有直接提供根 span 的关键终止字段。",
                                "implementation": "在数据概览结果中加入根 span 状态和结束时间摘要。",
                                "expected_impact": "降低判断执行结果所需的额外检索次数。",
                                "target": "workspace_bench_tools.ts",
                            },
                            {
                                "component": "tool_definition",
                                "priority": "P2",
                                "title": "明确终止字段语义",
                                "problem": "工具定义没有清楚说明哪些字段能够证明根执行终止。",
                                "implementation": "在工具 schema 描述中明确根状态、结束时间和终止原因字段。",
                                "expected_impact": "使不同代理采用一致的执行分类依据。",
                                "target": "workspace_bench_tools.ts",
                            },
                        ],
                    ),
                    ensure_ascii=False,
                )
                + "\n"
                f"{FINAL_SENTINEL}"
            )
        },
    ]
