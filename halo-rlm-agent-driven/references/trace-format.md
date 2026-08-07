# Trace format reference — halo-rlm

The engine reads one JSONL file: **one OTel span per line** (OpenInference
compatible). Both flat exports and OTLP-ish exports are accepted; the loader
is tolerant — missing fields get defaults, unparsable lines are skipped.

## Top-level span fields

| Field | Accepted keys | Notes |
| --- | --- | --- |
| trace id | `trace_id`, `traceId`, `context.trace_id` | required (line skipped otherwise) |
| span id | `span_id`, `spanId`, `context.span_id` | default `""` |
| parent | `parent_span_id`, `parentSpanId`, `parent.span_id`, `parent_id` | optional |
| name | `name` | default `""` |
| kind | `kind` | string (`SPAN_KIND_CLIENT`) or int (rendered `SPAN_KIND_<n>`) |
| start | `start_time`, `startTime`, else `startTimeUnixNano` / `start_time_unix_nano` | ISO 8601 string, or unix nanos (converted to ISO `Z`) |
| end | `end_time`, `endTime`, else `endTimeUnixNano` / `end_time_unix_nano` | same |
| status | `status.code` / `status_code`, `status.message` / `status_message` | code e.g. `STATUS_CODE_OK` / `STATUS_CODE_ERROR` |
| attributes | `attributes` | flat dict, see below |
| resource | `resource.attributes` | `service.name` read from here (fallback: `attributes["service.name"]`); this is an OTel service identity, not an Agent name |

## Recognized attribute keys

- Model: `inference.llm.model_name` (fallbacks: `llm.model_name`,
  `gen_ai.request.model`, `model_name`)
- Tokens: `inference.llm.input_tokens` / `inference.llm.output_tokens`, then
  `llm.token_count.prompt` / `llm.token_count.input` and
  `llm.token_count.completion` / `llm.token_count.output`
- Project: `inference.project_id`
- Agent names (tolerant): `inference.agent_name`, `agent.id`, `agent.name`,
  `agent_id`, `agent_name`,
  plus any string-valued `openinference.agent.*` key
- Common payloads: `input.value`, `output.value`, `openinference.span.kind`

Semantic health markers worth searching (no fixed schema): `success=false`,
`completed=false`, `finalized=false`, `agent.outcome`, `agent.stop_reason`,
`tool.result.missing`, `timeout`, `rate_limit`, `provider_attempt`,
`validation`, `rejected`, `quota`, `max_turns`, `max_steps`, `budget`,
`exceeded`.

## Memory model

- At index time each line is parsed once for lightweight metadata (ids, name,
  status, times, tokens, model, service, agent names) and its
  `(trace_id, byte offset, byte length)` is recorded.
- The metadata index is cached beside the trace as
  `<traces.jsonl>.halo-rlm-index.json`. Source size and modification time
  invalidate stale caches. Read-only directories remain supported without a
  cache.
- Keep sidecars as reusable generated cache. Do not delete them to refresh a
  diagnosis; stale cache contents are rejected and rebuilt automatically.
- Full attributes are parsed **on demand** by seeking back to the raw line —
  the file is never loaded wholesale into memory.
- `raw_jsonl_bytes` of a trace = sum of its line byte lengths; the overview's
  `raw_jsonl_bytes` sums the filtered traces. Use it to judge scan cost.

## View truncation rules (constants in `trace_store.py`)

| Constant | Value | Applies to |
| --- | --- | --- |
| `_DISCOVERY_ATTR_TRUNCATION_CHARS` | 4096 | `view_trace` per-attribute cap |
| `_SURGICAL_ATTR_TRUNCATION_CHARS` | 16384 | `view_spans` per-attribute cap |
| `_VIEW_RESPONSE_BYTES_BUDGET` | 150_000 | `view_trace` / `view_spans` total response budget |

- A string value longer than the cap keeps its **head** and gets the marker
  `... [HALO truncated: original N chars]`. Non-string values are JSON
  serialized first, then capped the same way.
- OpenInference flat projection keys matching
  `^(?:llm\.(?:input|output)_messages|mcp\.tools)\.\d+\.` are dropped from
  views; a `__halo_dropped_flat_projections` attribute records the drop.
- If a view response would exceed the 150KB budget, spans are withheld and an
  **oversized summary** is returned instead:
  `{trace_id, oversized, span_count, truncated_response_bytes,
  response_bytes_budget, span_response_bytes_min/median/max,
  top_span_names (<=10 [name, count]), error_span_count, recommendation}`.
  Follow the recommendation: `search_trace` → `view_spans` on a small set.

## Search records

`search_trace` / `search_span` run `re.finditer` over the **raw JSONL line
text** of each span and return bounded `SpanMatchRecord`s:

```
trace_id, span_id, span_index, span_name, kind, status_code, parent_span_id,
raw_jsonl_bytes, match_text, matched_context, match_start_char, match_end_char
```

`matched_context` = the match plus up to `context_buffer_chars` (default 500)
on each side. `has_more=true` means `match_count > returned_match_count` —
refine the regex rather than blindly raising `max_matches`.

## `has_errors` semantics

Strictly OTel: a trace has errors iff at least one of its spans has
`status.code == "STATUS_CODE_ERROR"`. `has_errors=false` does **not** prove
the run succeeded — always also probe the semantic markers above for
reliability questions.

The converse also matters: `has_errors=true` does **not** prove the run
ultimately failed. It may contain a failed attempt followed by a successful
retry. Determine final execution status from the root AGENT span, then correlate
each intermediate error with later spans by time, tool/subagent identity,
compatible arguments, explicit success values, and result verification.

Evidence strength:

1. Root AGENT terminal status and root-level semantic outcome determine whether
   the agent execution completed or failed.
2. A later matching retry with `STATUS_CODE_OK` and a non-error result, or an
   explicit `success=true`, `ok=true`, `completed=true`, or `exitCode=0`, can
   prove operation-level recovery.
3. Verification of the expected artifact/result is stronger supporting
   evidence.
4. An unrelated later OK span never proves recovery.
5. Without task/judge context, trace evidence can establish execution outcome,
   not whether the external task passed evaluation.

## Tool result envelopes

Core trace tools return the same outer envelope used by HALOAgent:

```json
{"result": {"total": 3}}
```

This applies to overview, query, count, view, and search tools. Internal
`TraceStore` Python methods return the inner object directly.

## Example line (flat format)

```json
{"trace_id": "trace-err-002", "span_id": "span-002-b", "parent_span_id": "span-002-a",
 "name": "llm.chat_completion", "kind": "SPAN_KIND_CLIENT",
 "start_time": "2024-06-01T10:01:02Z", "end_time": "2024-06-01T10:01:29Z",
 "status": {"code": "STATUS_CODE_ERROR", "message": "MaxTurnsExceeded: agent exceeded maximum of 20 turns"},
 "attributes": {"openinference.span.kind": "LLM", "llm.model_name": "gpt-4o",
                "llm.token_count.prompt": 21000, "llm.token_count.completion": 4000,
                "error.type": "MaxTurnsExceeded"},
 "resource": {"attributes": {"service.name": "payment-service"}}}
```

OTLP-ish variant also accepted (`traceId`/`spanId`/`parentSpanId`,
`startTimeUnixNano`/`endTimeUnixNano` as strings, integer `kind`).
