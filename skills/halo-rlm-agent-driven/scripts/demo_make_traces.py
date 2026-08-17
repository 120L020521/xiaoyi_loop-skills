#!/usr/bin/env python3
"""Generate a synthetic OTel JSONL trace dataset for halo-rlm demos/tests.

Produces >= 6 traces covering:
  1. trace-ok-001      - healthy trace (with OpenInference flat projection keys)
  2. trace-err-002     - OTel error trace (STATUS_CODE_ERROR, MaxTurnsExceeded)
  3. trace-big-003     - large trace that triggers the oversized view budget
  4. trace-semfail-004 - semantic failure: success=false, no OTel error status
  5. trace-ok-005      - healthy trace with a 5000-char attribute (truncation)
  6. trace-err-006     - OTel error trace (timeout / provider attempts)

Usage:
    python demo_make_traces.py [out_path]
Default out_path: ./demo_traces.jsonl
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

def _iso(minute: int, second: int = 0) -> str:
    return f"2024-06-01T10:{minute:02d}:{second:02d}Z"


def make_span(
    trace_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    name: str,
    start_time: str,
    end_time: str,
    status_code: str = "STATUS_CODE_OK",
    status_message: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
    service_name: str = "checkout-service",
    kind: str = "SPAN_KIND_INTERNAL",
) -> dict[str, Any]:
    attrs = dict(attributes or {})
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id or "",
        "trace_state": "",
        "name": name,
        "kind": kind,
        "start_time": start_time,
        "end_time": end_time,
        "status": {"code": status_code, "message": status_message or ""},
        "attributes": attrs,
        "resource": {"attributes": {"service.name": service_name}},
        "scope": {"name": "halo-rlm-demo", "version": "1"},
    }


def build_spans() -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1) trace-ok-001: healthy 3-span agent run; includes OpenInference
    #    flat projection keys (llm.input_messages.0.*) that views must drop.
    # ------------------------------------------------------------------
    spans.append(
        make_span(
            "trace-ok-001",
            "span-001-a",
            None,
            "agent.run",
            _iso(0, 0),
            _iso(0, 9),
            attributes={
                "openinference.span.kind": "AGENT",
                "agent.name": "shopping-agent",
                "agent.id": "agent-7",
                "inference.agent_name": "inference-shopping-agent",
                "inference.project_id": "demo-project",
                "inference.llm.model_name": "gpt-inference-only",
                "inference.llm.input_tokens": 1200,
                "inference.llm.output_tokens": 340,
                "llm.model_name": "gpt-4o-mini",
                "llm.token_count.prompt": 1200,
                "llm.token_count.completion": 340,
                "llm.input_messages.0.role": "user",
                "llm.input_messages.0.content": "Buy milk and bread",
                "llm.output_messages.0.message.role": "assistant",
                "mcp.tools.0.name": "spotify__login",
                "input.value": "Buy milk and bread",
                "output.value": "Added milk and bread to cart.",
            },
            service_name="shopping-app",
        )
    )
    spans.append(
        make_span(
            "trace-ok-001",
            "span-001-b",
            "span-001-a",
            "llm.chat_completion",
            _iso(0, 1),
            _iso(0, 4),
            attributes={
                "openinference.span.kind": "LLM",
                "llm.model_name": "gpt-4o-mini",
                "llm.token_count.prompt": 800,
                "llm.token_count.completion": 120,
            },
            service_name="shopping-app",
            kind="SPAN_KIND_CLIENT",
        )
    )
    spans.append(
        make_span(
            "trace-ok-001",
            "span-001-c",
            "span-001-a",
            "tool.cart_add",
            _iso(0, 5),
            _iso(0, 8),
            attributes={"openinference.span.kind": "TOOL", "tool.name": "cart__add"},
            service_name="shopping-app",
        )
    )

    # ------------------------------------------------------------------
    # 2) trace-err-002: OTel error (MaxTurnsExceeded) in payment-service.
    # ------------------------------------------------------------------
    spans.append(
        make_span(
            "trace-err-002",
            "span-002-a",
            None,
            "agent.run",
            _iso(1, 0),
            _iso(1, 30),
            attributes={
                "openinference.span.kind": "AGENT",
                "agent.name": "payment-agent",
                "llm.model_name": "gpt-4o",
                "llm.token_count.prompt": 22000,
                "llm.token_count.completion": 4100,
            },
            service_name="payment-service",
        )
    )
    spans.append(
        make_span(
            "trace-err-002",
            "span-002-b",
            "span-002-a",
            "llm.chat_completion",
            _iso(1, 2),
            _iso(1, 29),
            status_code="STATUS_CODE_ERROR",
            status_message="MaxTurnsExceeded: agent exceeded maximum of 20 turns",
            attributes={
                "openinference.span.kind": "LLM",
                "llm.model_name": "gpt-4o",
                "error.type": "MaxTurnsExceeded",
                "llm.token_count.prompt": 21000,
                "llm.token_count.completion": 4000,
            },
            service_name="payment-service",
            kind="SPAN_KIND_CLIENT",
        )
    )

    # ------------------------------------------------------------------
    # 3) trace-big-003: 60 spans x ~6KB attribute -> view_trace response
    #    exceeds the 150KB budget -> oversized summary. One span also has a
    #    20000-char attribute (16KB surgical-cap test) and one OTel error.
    # ------------------------------------------------------------------
    big_payload = ("PAYLOAD-" + "x" * 120) * 48  # ~6KB
    huge_single = "HUGE-" + "y" * 19995  # 20000 chars
    for i in range(60):
        attrs: dict[str, Any] = {
            "openinference.span.kind": "CHAIN",
            "large_payload": big_payload,
            "step.index": i,
        }
        status_code = "STATUS_CODE_OK"
        status_message = ""
        if i == 0:
            attrs["huge_single_attr"] = huge_single
            attrs["agent.name"] = "bulk-processor"
        if i == 42:
            status_code = "STATUS_CODE_ERROR"
            status_message = "KeyError: 'cart_total'"
            attrs["error.type"] = "KeyError"
        spans.append(
            make_span(
                "trace-big-003",
                f"span-003-{i:02d}",
                "span-003-00" if i else None,
                f"bulk.step.{i % 5}",
                _iso(2, i % 60),
                _iso(2, min(i % 60 + 1, 59)),
                status_code=status_code,
                status_message=status_message,
                attributes=attrs,
                service_name="etl-service",
            )
        )

    # ------------------------------------------------------------------
    # 4) trace-semfail-004: semantic failure (success=false) with OK status.
    # ------------------------------------------------------------------
    spans.append(
        make_span(
            "trace-semfail-004",
            "span-004-a",
            None,
            "agent.run",
            _iso(3, 0),
            _iso(3, 20),
            attributes={
                "openinference.span.kind": "AGENT",
                "agent.name": "refund-agent",
                "llm.model_name": "claude-sonnet-4",
                "task.success": False,
                "agent.outcome": "success=false",
                "agent.stop_reason": "max_turns",
                "validation": "rejected",
                "output.value": "Refund workflow did not finalize.",
            },
            service_name="refund-service",
        )
    )
    spans.append(
        make_span(
            "trace-semfail-004",
            "span-004-b",
            "span-004-a",
            "tool.refund_submit",
            _iso(3, 3),
            _iso(3, 6),
            attributes={
                "openinference.span.kind": "TOOL",
                "tool.name": "refund__submit",
                "tool.result.missing": True,
            },
            service_name="refund-service",
        )
    )

    # ------------------------------------------------------------------
    # 5) trace-ok-005: healthy trace carrying a 5000-char attribute to
    #    exercise the 4KB discovery truncation marker.
    # ------------------------------------------------------------------
    spans.append(
        make_span(
            "trace-ok-005",
            "span-005-a",
            None,
            "agent.run",
            _iso(4, 0),
            _iso(4, 10),
            attributes={
                "openinference.span.kind": "AGENT",
                "agent.name": "support-agent",
                "llm.model_name": "gpt-4o-mini",
                "llm.token_count.prompt": 500,
                "llm.token_count.completion": 90,
                "long_context": "CTX-" + "z" * 4996,  # 5000 chars
            },
            service_name="support-service",
        )
    )
    spans.append(
        make_span(
            "trace-ok-005",
            "span-005-b",
            "span-005-a",
            "tool.kb_search",
            _iso(4, 2),
            _iso(4, 5),
            attributes={"openinference.span.kind": "TOOL", "tool.name": "kb__search"},
            service_name="support-service",
        )
    )

    # ------------------------------------------------------------------
    # 6) trace-err-006: timeout error after provider retries.
    # ------------------------------------------------------------------
    spans.append(
        make_span(
            "trace-err-006",
            "span-006-a",
            None,
            "agent.run",
            _iso(5, 0),
            _iso(5, 30),
            status_code="STATUS_CODE_ERROR",
            status_message="deadline exceeded",
            attributes={
                "openinference.span.kind": "AGENT",
                "agent.name": "sync-agent",
                "llm.model_name": "gpt-4o",
                "error.type": "timeout",
                "provider_attempt": 3,
                "rate_limit": False,
                "llm.token_count.prompt": 3000,
                "llm.token_count.completion": 0,
            },
            service_name="sync-service",
        )
    )
    spans.append(
        make_span(
            "trace-err-006",
            "span-006-b",
            "span-006-a",
            "http.post.provider",
            _iso(5, 2),
            _iso(5, 29),
            status_code="STATUS_CODE_ERROR",
            status_message="ETIMEDOUT after 25000ms",
            attributes={
                "openinference.span.kind": "LLM",
                "llm.model_name": "gpt-4o",
                "error.type": "timeout",
                "provider_attempt": 3,
            },
            service_name="sync-service",
            kind="SPAN_KIND_CLIENT",
        )
    )

    return spans


def main(argv: list[str]) -> int:
    out_path = argv[1] if len(argv) > 1 else "demo_traces.jsonl"
    spans = build_spans()
    with open(out_path, "w", encoding="utf-8") as f:
        for span in spans:
            f.write(json.dumps(span, ensure_ascii=False) + "\n")
    trace_ids = sorted({s["trace_id"] for s in spans})
    print(f"wrote {len(spans)} spans across {len(trace_ids)} traces to {out_path}")
    for tid in trace_ids:
        print(f"  - {tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
