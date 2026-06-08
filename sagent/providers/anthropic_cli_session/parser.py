"""Parse a CLI-written session JSONL back into a sagent-shaped message list.

Used by:

- The round-trip test: parse a real claude-written JSONL, hand the
  result to the materializer, byte-diff (modulo volatile fields).
- The startup tripwire: parse what claude wrote during the canary
  turn, compare structurally to what the materializer would have
  written for the same prompt.

Lossy by design — we drop UI sidecar entries (mode, permission-mode,
agent-name, last-prompt, file-history-snapshot, queue-operation,
ai-title, custom-title, agent-setting) and rebuild the linearized
message list that the provider would consume. See ``format_spec.md``
for the full list of sidecar types and which entries actually
contribute to ``--resume``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import json

from sagent.types.runtime import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    UserMessage,
)


# Entries that never contribute to the linearized message list.
_SIDECAR_TYPES = frozenset(
    {
        "attachment",
        "custom-title",
        "agent-name",
        "mode",
        "permission-mode",
        "last-prompt",
        "file-history-snapshot",
        "queue-operation",
        "ai-title",
        "agent-setting",
        # system entries are compaction boundaries we don't reconstruct
        "system",
    }
)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each non-empty line of ``path`` parsed as a JSON object."""
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            yield json.loads(stripped)


def parse_jsonl_to_messages(path: Path) -> list[object]:
    """Parse a CLI session JSONL into a sagent-shaped message list.

    Returns a flat list of ``UserMessage`` / ``AssistantMessage`` /
    ``ToolResult`` instances in tape order.

    Coalescing rule: a single ``user`` JSONL entry whose ``content`` is
    a list of ``tool_result`` blocks is split back into one
    ``ToolResult`` per block (matching the sagent tape model, where
    each tool produces its own ``ToolResult``).
    """
    return list(_parse_entries(iter_jsonl(path)))


def _parse_entries(entries: Iterable[dict[str, Any]]) -> Iterator[object]:
    for entry in entries:
        t = entry.get("type")
        if t in _SIDECAR_TYPES:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            yield from _parse_user_entry(content)
        elif role == "assistant":
            yield _parse_assistant_entry(content)


def _parse_user_entry(content: Any) -> Iterator[object]:
    """Split a user JSONL entry into UserMessage or ToolResult(s)."""
    if isinstance(content, str):
        yield UserMessage(text=content)
        return
    if not isinstance(content, list):
        return

    text_parts: list[str] = []
    tool_results: list[ToolResult] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text", "")))
        elif kind == "tool_result":
            tool_results.append(_parse_tool_result_block(block))
        # Other block kinds (image, document, tool_reference) ignored.

    if text_parts:
        yield UserMessage(text="\n".join(text_parts))
    yield from tool_results


def _parse_tool_result_block(block: dict[str, Any]) -> ToolResult:
    raw_content = block.get("content", "")
    if isinstance(raw_content, list):
        # Concatenate text sub-blocks. tool_reference / image blocks
        # we render as a marker so the diff catches them.
        text_pieces: list[str] = []
        for sub in raw_content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                text_pieces.append(str(sub.get("text", "")))
            else:
                text_pieces.append(f"[non-text block: {sub}]")
        text = "\n".join(text_pieces)
    elif isinstance(raw_content, str):
        text = raw_content
    else:
        text = ""
    return ToolResult(
        call_id=str(block.get("tool_use_id", "")),
        content=text,
        is_error=bool(block.get("is_error", False)),
    )


def _parse_assistant_entry(content: Any) -> AssistantMessage:
    if not isinstance(content, list):
        return AssistantMessage()
    text_parts: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text", "")))
        elif kind in ("thinking", "redacted_thinking"):
            thinking_blocks.append(dict(block))
        elif kind == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    args=dict(block.get("input", {})),
                )
            )
    return AssistantMessage(
        text="".join(text_parts),
        thinking_blocks=tuple(thinking_blocks),
        tool_calls=tuple(tool_calls),
    )
