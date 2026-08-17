# Architecture reference — halo-rlm recursive engine

Read this when tuning depth/parallelism/compaction or modifying the engine.

## Agent tree

```
root (depth 0)  ──call_subagent──► subagent (depth 1) ──call_subagent──► subagent (depth 2)
      │                                  │                                     │ (max depth:
      └────────── final report ◄─────────┴──── JSON answers ◄──────────────────┘  no call_subagent)
```

- Every agent runs the same synchronous tool loop on its own thread and owns a
  private, compaction-aware `AgentContext`. There is no shared conversation
  state between agents — a subagent only receives the delegated `input` string
  and returns a JSON result.
- Caller-supplied dataset context is rendered only into the root system prompt.
  A subagent receives the generic subagent prompt and its delegated input.
- Subagent initial context: `[system(rendered SUBAGENT template), user(input)]`.
- Subagent result (the `call_subagent` tool result):
  `{child_agent_id, answer, turns_used, tool_calls_made}`. If the child
  crashes, `answer` carries the error description — exceptions never propagate
  into the parent loop.

## Per-depth semaphores (deadlock-free pools)

```python
{d: threading.Semaphore(maximum_parallel_subagents) for d in 1..maximum_depth}
```

- Each depth has its OWN independent pool. A depth-1 agent waiting for a
  depth-2 slot only holds its own thread, never a depth-2 slot — so circular
  waiting (the classic "parent holds the pool slot the child needs" deadlock)
  is structurally impossible. The depth-0 root is unrestricted.
- Spawning: the parent's tool executor acquires the depth+1 semaphore, starts
  the child thread, joins it, releases the semaphore.
- Parallel tool calls inside ONE assistant message run on a
  `ThreadPoolExecutor`; results are appended to the context in the original
  call order.
- `call_subagent` is only **registered** when `depth < maximum_depth`
  (structural cap), with a defensive runtime check as backstop.

## Turn loop

Each turn:
1. Render `context.to_messages()` and call the LLM (with tool schemas).
2. If the reply has `tool_calls`: execute (parallel if several), append
   `assistant` + `tool` items, run `compact_old_items()`, continue.
3. If there are no tool calls:
   - **root**: if the text contains `<final/>`, strip the sentinel and return
     the report. Otherwise append a user nudge ("continue or finish with
     `<final/>`") and continue, up to `maximum_turns`.
   - **subagent**: the last assistant text IS the answer; return immediately.
4. At `maximum_turns`, return the last assistant content (root: stripped of
   any sentinel) — the loop always terminates.

## Context compaction

`AgentContext(keep_last_messages=12, keep_last_turns=3)` compacts in place
once per turn, using `compaction_model` and `COMPACTION_SYSTEM_PROMPT`:

- **Plain messages** (non-system user/assistant without tool calls): when more
  than `keep_last_messages` are live, compact the oldest first, one by one.
- **Tool turns**: an assistant message with `tool_calls` plus its consecutive
  `tool` result items form one atomic group. When more than `keep_last_turns`
  groups are live, the oldest groups are compacted **wholesale** — every
  member is summarized before any is marked compacted, so a turn is never
  half-compacted.
- `system` items are never compacted. A failed compaction call leaves the
  item/group untouched and is retried on the next pass; it never interrupts
  the run.
- Compacted items render as summaries (`Compacted message/tool calls/tool
  result (id: X): ...`); compacted `tool` items render as **assistant**
  messages so the transcript never contains an orphan `tool` message. The
  original content stays retrievable through the `get_context_item` tool.
- Compaction source texts: `USER MESSAGE:\n...`, `ASSISTANT MESSAGE:\n...`,
  `ASSISTANT TOOL CALLS:\n- name(args)...`, `TOOL RESULT (tool=X, call=Y):\n...`.

## LLM client

- Minimal OpenAI-compatible `POST {base_url}/chat/completions` via urllib:
  `tools` + `parallel_tool_calls`, `temperature`, `max_tokens`.
- Retries HTTP 429 and 5xx (plus transient network errors) with exponential
  backoff + jitter, honoring `Retry-After`. Non-retryable HTTP errors raise
  `LLMError` with the response body excerpt.
- **Mock mode**: `LLMClient(mock_script=[{content, tool_calls}, ...])` pops
  one scripted reply per agent chat call (thread-safe FIFO). Compaction
  requests (detected via the compaction system prompt) are answered with a
  canned summary WITHOUT consuming the queue, so scripts only cover
  agent-visible turns. `scripted_mock_for_demo()` builds a deterministic
  demo: overview → parallel leaf calls → subagent A → subagent B →
  `<final/>` report.
- Mock mode is explicit. A missing API key raises an error instead of silently
  producing an empty mock report.

## Progress events

`run_engine(..., on_event=callback)` emits dicts: `engine_start`,
`agent_start`, `turn`, `tool_call`, `tool_result`, `agent_end`,
`max_turns_reached`, `engine_end`. The CLI renders them to stderr; the final
report goes to stdout/`-o`. Callback exceptions are swallowed — reporting
never breaks the run.

## Tuning guidance

- **Deep trees** (`maximum_depth >= 2`): give the root a delegating prompt
  (the default root template already prefers delegation). Keep
  `maximum_parallel_subagents` small (2–4) — each running agent is a live
  thread plus an LLM stream.
- **Long investigations**: raise `maximum_turns` before raising depth; most
  traces need breadth (more subagents) more than depth.
- **Compaction pressure**: lower `keep_last_messages` / `keep_last_turns` when
  the model's context is small; compaction failures are safe (retried), so a
  flaky compaction model degrades gracefully to a longer transcript.
- **Cost**: use a cheap `compaction_model` and `synthesis_model`; they only
  handle short summaries.
- **Timeouts**: `run_code` is capped at 30s wall clock and 10K chars of
  stdout/stderr; the LLM HTTP timeout defaults to 120s per attempt.
