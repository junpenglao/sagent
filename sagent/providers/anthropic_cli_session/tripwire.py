"""Tripwire: detect drift between claude-written and materializer-written JSONL.

Two entry points:

- :func:`structural_diff` — pure function that compares two JSONL
  payloads (either as paths or as already-parsed entry lists),
  ignoring the volatile fields documented in ``format_spec.md``
  (``timestamp``, ``requestId``, ``uuid``, ``parentUuid``,
  ``message.id``, ``message.usage``, etc.). Returns a list of
  ``DiffFinding`` entries; an empty list means "no drift, safe to
  enable materialization".

- :func:`run_canary_against_live_cli` — placeholder for the v2.1-β
  startup probe that spawns a real ``claude --print`` against a
  hard-coded ``"ping"`` prompt, reads the JSONL claude writes, runs
  the same prompt through the materializer, and reports the diff
  verdict. Not wired into ``serve.py`` yet (the production wiring
  is the v2.1-β gate per the worklog proposal).

The diff is structural, not byte-level — claude splits each content
block into its own JSONL entry (thinking → own line, then tool_use
→ own line), while the materializer coalesces them into a single
assistant entry per ``AssistantMessage``. Both are valid for
``--resume``; the diff normalizes both sides to the same canonical
linearized-message form (the result of running
``parse_jsonl_to_messages`` on each side) and compares THAT.

That same comparison runs in the round-trip test
``test_real_claude_jsonl_round_trip`` — this module just packages it
behind a structured-verdict API so the boot path can act on the
result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sagent.providers.anthropic_cli_session.parser import (
    parse_jsonl_to_messages,
)
from sagent.types.runtime import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    UserMessage,
)


@dataclass(frozen=True)
class DiffFinding:
    """One structural difference between two JSONL message lists.

    ``location`` describes WHERE the drift was found (a tape index,
    a field name); ``detail`` describes WHAT differs in a way the
    operator can read in a log line.
    """

    location: str
    detail: str


def structural_diff(
    left: Sequence[object] | Path,
    right: Sequence[object] | Path,
) -> list[DiffFinding]:
    """Compare two linearized message lists structurally.

    Both arguments may be either:

    - a ``Path`` to a JSONL file (will be parsed via
      :func:`parse_jsonl_to_messages`), or
    - an already-parsed list of ``UserMessage``/``AssistantMessage``/
      ``ToolResult``/``AgentSendMessage`` instances.

    The diff ignores volatile fields (timestamps, request IDs,
    UUIDs, model.id, model.usage, thinking signatures) per
    ``format_spec.md``. Returns ``[]`` when the two sides agree on
    the meaningful content; otherwise a non-empty list of
    findings, one per differing position.
    """
    left_msgs = _ensure_parsed(left)
    right_msgs = _ensure_parsed(right)

    findings: list[DiffFinding] = []

    if len(left_msgs) != len(right_msgs):
        findings.append(
            DiffFinding(
                location="<top-level>",
                detail=(
                    f"length mismatch: left has {len(left_msgs)} entries, "
                    f"right has {len(right_msgs)}"
                ),
            )
        )

    common = min(len(left_msgs), len(right_msgs))
    for i in range(common):
        findings.extend(_compare_one(i, left_msgs[i], right_msgs[i]))

    return findings


def is_safe_to_enable(findings: Sequence[DiffFinding]) -> bool:
    """The verdict the boot path acts on: ``True`` iff no findings."""
    return len(findings) == 0


def _ensure_parsed(source: Sequence[object] | Path) -> list[object]:
    if isinstance(source, Path):
        return parse_jsonl_to_messages(source)
    return list(source)


def _compare_one(i: int, a: object, b: object) -> list[DiffFinding]:
    """Compare two messages at the same tape position."""
    if type(a) is not type(b):
        return [
            DiffFinding(
                location=f"[{i}]",
                detail=f"type mismatch: {type(a).__name__} vs {type(b).__name__}",
            )
        ]

    if isinstance(a, UserMessage) and isinstance(b, UserMessage):
        return _compare_text(i, a.text, b.text)
    if isinstance(a, AssistantMessage) and isinstance(b, AssistantMessage):
        return _compare_assistant(i, a, b)
    if isinstance(a, ToolResult) and isinstance(b, ToolResult):
        return _compare_tool_result(i, a, b)
    # Unknown type for either side — surface as a finding rather than
    # asserting, so the operator gets a useful log line.
    return [
        DiffFinding(
            location=f"[{i}]",
            detail=f"unhandled message type {type(a).__name__}",
        )
    ]


def _compare_text(i: int, a: str, b: str) -> list[DiffFinding]:
    if a == b:
        return []
    return [
        DiffFinding(
            location=f"[{i}].text",
            detail=f"text differs: {a!r} vs {b!r}",
        )
    ]


def _compare_assistant(
    i: int, a: AssistantMessage, b: AssistantMessage
) -> list[DiffFinding]:
    out: list[DiffFinding] = []
    if a.text != b.text:
        out.append(
            DiffFinding(
                location=f"[{i}].text",
                detail=f"text differs: {a.text!r} vs {b.text!r}",
            )
        )
    if len(a.tool_calls) != len(b.tool_calls):
        out.append(
            DiffFinding(
                location=f"[{i}].tool_calls",
                detail=(
                    f"tool_call count differs: {len(a.tool_calls)} vs "
                    f"{len(b.tool_calls)}"
                ),
            )
        )
    for j, (tca, tcb) in enumerate(zip(a.tool_calls, b.tool_calls, strict=False)):
        out.extend(_compare_tool_call(i, j, tca, tcb))
    # thinking_blocks are intentionally NOT compared:
    # - signatures are opaque + provider-minted (volatile field)
    # - claude may split each thinking block to its own JSONL line
    #   while we coalesce; round-trip preserves the AssistantMessage
    #   text + tool_calls but loses thinking-block grouping fidelity.
    return out


def _compare_tool_call(i: int, j: int, a: ToolCall, b: ToolCall) -> list[DiffFinding]:
    out: list[DiffFinding] = []
    if a.id != b.id:
        out.append(
            DiffFinding(
                location=f"[{i}].tool_calls[{j}].id",
                detail=f"id differs: {a.id!r} vs {b.id!r}",
            )
        )
    if a.name != b.name:
        out.append(
            DiffFinding(
                location=f"[{i}].tool_calls[{j}].name",
                detail=f"name differs: {a.name!r} vs {b.name!r}",
            )
        )
    if dict(a.args) != dict(b.args):
        out.append(
            DiffFinding(
                location=f"[{i}].tool_calls[{j}].args",
                detail=f"args differ: {dict(a.args)!r} vs {dict(b.args)!r}",
            )
        )
    return out


def _compare_tool_result(i: int, a: ToolResult, b: ToolResult) -> list[DiffFinding]:
    out: list[DiffFinding] = []
    if a.call_id != b.call_id:
        out.append(
            DiffFinding(
                location=f"[{i}].call_id",
                detail=f"call_id differs: {a.call_id!r} vs {b.call_id!r}",
            )
        )
    if a.content != b.content:
        out.append(
            DiffFinding(
                location=f"[{i}].content",
                detail=f"content differs: {a.content!r} vs {b.content!r}",
            )
        )
    if a.is_error != b.is_error:
        out.append(
            DiffFinding(
                location=f"[{i}].is_error",
                detail=f"is_error differs: {a.is_error} vs {b.is_error}",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Live canary runner (stub for v2.1-β)
# ---------------------------------------------------------------------------


def run_canary_against_live_cli(
    *,
    session_id: str,
    cwd: Path,
    home: Path | None = None,
) -> tuple[bool, list[DiffFinding]]:
    """Stub for the v2.1-β startup canary.

    The plan is: spawn ``claude --print`` against a fixed ``"ping"``
    prompt, capture the JSONL claude writes to disk, run the same
    prompt through the materializer, structural-diff the two, return
    ``(is_safe, findings)``.

    NotImplementedError today because the live-spawn path requires
    credentials + a subprocess + a way to clean up the canary
    session, which is out of scope for v2.1-α (Phases 1 & 2). The
    boot path can call this stub and treat the NotImplementedError
    as "tripwire unavailable; default to materialization-off" until
    v2.1-β lands it.
    """
    raise NotImplementedError(
        "v2.1-β canary live spawn not implemented; use structural_diff() "
        "with a pre-captured pair of JSONL files for now",
    )
