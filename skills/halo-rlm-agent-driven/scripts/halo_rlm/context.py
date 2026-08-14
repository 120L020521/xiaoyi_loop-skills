"""Compaction-aware agent context.

An AgentContext stores the full-fidelity conversation (ContextItems) and
renders it to OpenAI messages for each LLM call. Old items are compacted in
place (replaced by a summary in the rendered view) while the original content
stays retrievable via ``get_item`` / the ``get_context_item`` tool.

Compaction policy (per spec):
- ``system`` items are never compacted.
- Plain text messages (user / assistant without tool_calls) beyond
  ``keep_last_messages`` are compacted oldest-first, one at a time. A failed
  compaction leaves the item untouched and never interrupts the run.
- Tool turns — one assistant message with tool_calls plus its following tool
  result items — form atomic groups. Beyond ``keep_last_turns`` groups, the
  oldest groups are compacted wholesale: every item in the group is compacted
  or none is (no half-compacted turns).
- Compacted ``tool`` items render as ``assistant`` messages so the rendered
  transcript never contains orphan ``tool`` messages.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from .prompts import COMPACTION_SYSTEM_PROMPT


@dataclass
class ContextItem:
    item_id: str
    role: str  # system | user | assistant | tool
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None  # OpenAI-shaped dicts
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    is_compacted: bool = False
    compaction_summary: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "is_compacted": self.is_compacted,
            "compaction_summary": self.compaction_summary,
        }


class AgentContext:
    def __init__(
        self,
        items: Optional[list[ContextItem]] = None,
        compaction_model: str = "gpt-4o-mini",
        keep_last_messages: int = 12,
        keep_last_turns: int = 3,
    ) -> None:
        self.items: list[ContextItem] = list(items) if items else []
        self.compaction_model = compaction_model
        self.keep_last_messages = keep_last_messages
        self.keep_last_turns = keep_last_turns
        self._id_counter = itertools.count(1)
        for item in self.items:
            if not item.item_id:
                item.item_id = self._next_id()

    # ------------------------------------------------------------------
    # Basic storage
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        return f"item-{next(self._id_counter)}"

    def append(self, item: ContextItem) -> ContextItem:
        if not item.item_id:
            item.item_id = self._next_id()
        self.items.append(item)
        return item

    def get_item(self, item_id: str) -> Optional[ContextItem]:
        for item in self.items:
            if item.item_id == item_id:
                return item
        return None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_compacted(self, item: ContextItem) -> dict[str, Any]:
        summary = item.compaction_summary or ""
        if item.role == "user":
            return {
                "role": "user",
                "content": f"Compacted message (id: {item.item_id}): {summary}",
            }
        if item.role == "assistant" and not item.tool_calls:
            return {
                "role": "assistant",
                "content": f"Compacted message (id: {item.item_id}): {summary}",
            }
        if item.role == "assistant":
            return {
                "role": "assistant",
                "content": f"Compacted tool calls (id: {item.item_id}): {summary}",
            }
        if item.role == "tool":
            # Render as assistant: a compacted tool result has no live
            # assistant tool_calls parent, so a "tool" role would be an orphan.
            return {
                "role": "assistant",
                "content": (
                    f"Compacted tool result (id: {item.item_id}, "
                    f"tool: {item.name}): {summary}"
                ),
            }
        # system items are never compacted; defensive fallback:
        return {"role": item.role, "content": summary}

    def to_messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        pending_tool_call_ids: set[str] = set()
        for item in self.items:
            if item.is_compacted:
                messages.append(self._render_compacted(item))
                if item.role in ("assistant", "tool"):
                    # A compacted assistant/tool breaks the adjacency between a
                    # live tool_calls message and any later live tool results.
                    pending_tool_call_ids = set()
                continue

            if item.role == "assistant":
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": item.content or "",
                }
                if item.tool_calls:
                    msg["tool_calls"] = item.tool_calls
                    pending_tool_call_ids = {
                        tc.get("id", "") for tc in item.tool_calls if isinstance(tc, dict)
                    }
                else:
                    pending_tool_call_ids = set()
                messages.append(msg)
            elif item.role == "tool":
                if item.tool_call_id and item.tool_call_id in pending_tool_call_ids:
                    pending_tool_call_ids.discard(item.tool_call_id)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.tool_call_id,
                            "content": item.content or "",
                        }
                    )
                else:
                    # Orphan guard: never emit a tool message without a live
                    # parent tool_calls message.
                    messages.append(
                        {
                            "role": "assistant",
                            "content": (
                                f"Tool result (tool: {item.name}, "
                                f"call: {item.tool_call_id}): {item.content or ''}"
                            ),
                        }
                    )
            else:
                pending_tool_call_ids = set()
                messages.append({"role": item.role, "content": item.content or ""})
        return messages

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    @staticmethod
    def _compaction_source_text(item: ContextItem) -> str:
        if item.role == "user":
            return f"USER MESSAGE:\n{item.content or ''}"
        if item.role == "assistant" and not item.tool_calls:
            return f"ASSISTANT MESSAGE:\n{item.content or ''}"
        if item.role == "assistant":
            lines = ["ASSISTANT TOOL CALLS:"]
            for tc in item.tool_calls or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                lines.append(f"- {fn.get('name', '?')}({fn.get('arguments', '')})")
            if item.content:
                lines.append(f"ASSISTANT MESSAGE:\n{item.content}")
            return "\n".join(lines)
        if item.role == "tool":
            return (
                f"TOOL RESULT (tool={item.name}, call={item.tool_call_id}):\n"
                f"{item.content or ''}"
            )
        return f"{item.role.upper()} MESSAGE:\n{item.content or ''}"

    def _compact_one(self, client: Any, item: ContextItem) -> str:
        """Summarize one item via the compaction model. May raise."""
        result = client.chat(
            messages=[
                {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": self._compaction_source_text(item)},
            ],
            model=self.compaction_model,
        )
        summary = (result.content or "").strip()
        if not summary:
            raise ValueError("empty compaction summary")
        return summary

    def _is_plain_message(self, item: ContextItem) -> bool:
        return (
            item.role in ("user", "assistant")
            and not item.tool_calls
            and item.role != "system"
        )

    def _tool_turn_groups(self) -> list[list[ContextItem]]:
        """Group items into atomic tool turns: an assistant message carrying
        tool_calls plus the consecutive tool result items that follow it."""
        groups: list[list[ContextItem]] = []
        i = 0
        items = self.items
        while i < len(items):
            item = items[i]
            if item.role == "assistant" and item.tool_calls:
                group = [item]
                j = i + 1
                while j < len(items) and items[j].role == "tool":
                    group.append(items[j])
                    j += 1
                groups.append(group)
                i = j
            else:
                i += 1
        return groups

    def compact_old_items(self, client: Any) -> None:
        """Run one compaction pass. Never raises: failures skip the item/group
        and are retried on the next pass."""
        # 1) Plain text messages beyond keep_last_messages, oldest first.
        plain = [
            it for it in self.items if self._is_plain_message(it) and not it.is_compacted
        ]
        excess = len(plain) - self.keep_last_messages
        if excess > 0:
            for item in plain[:excess]:
                try:
                    item.compaction_summary = self._compact_one(client, item)
                    item.is_compacted = True
                except Exception:
                    continue  # retry next pass; never interrupt the loop

        # 2) Tool turn groups beyond keep_last_turns, oldest group first.
        groups = [
            g
            for g in self._tool_turn_groups()
            if not all(it.is_compacted for it in g)
        ]
        group_excess = len(groups) - self.keep_last_turns
        if group_excess > 0:
            for group in groups[:group_excess]:
                summaries: list[str] = []
                ok = True
                for item in group:
                    if item.is_compacted:
                        summaries.append(item.compaction_summary or "")
                        continue
                    try:
                        summaries.append(self._compact_one(client, item))
                    except Exception:
                        ok = False
                        break
                if not ok:
                    continue  # whole group skipped; retry next pass
                for item, summary in zip(group, summaries):
                    item.compaction_summary = summary
                    item.is_compacted = True
