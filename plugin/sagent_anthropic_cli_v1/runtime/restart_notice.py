"""Restart-notice runtime observer.

Why this exists
---------------

Sagent's ``_AnthropicCLIModel`` respawns its ``claude --print``
subprocess when the API streams an aborted response
(``aborted_streaming`` / ``ede_diagnostic``). On respawn, sagent
re-feeds the full ``agent.history`` to the new subprocess … EXCEPT
the ``AssistantMessage`` entries: ``providers/anthropic_cli.py:537``
explicitly filters them out. The new subprocess sees only
``UserMessage`` and ``AgentSendMessage`` entries.

That means the model's own prior outputs — text, ``sagent_send``
tool calls, internal ``Bash`` / ``Read`` / ``Glob`` tool calls — are
**invisible** to the respawned subprocess. Peer replies look like
they arrived "out of nowhere" with no triggering delegation, and
the typical model guess is "the original task hasn't been started;
let me delegate."

The double whammy
-----------------

``providers/anthropic_cli.py:924`` ALWAYS returns
``AssistantMessage(tool_calls=())``. The CLI runs the entire MCP
tool round-trip internally and only the final assistant text comes
back to sagent. So there is NO record in ``agent.history`` of any
``sagent_send`` tool calls the model made — the
``tool_calls`` attribute is structurally empty for every
AssistantMessage in the tape.

That means we cannot recover outbounds from the tape itself, no
matter how aggressively we walk it. The earlier "walk-back to last
``sagent_send`` boundary" logic was theatre: it queried a field
that the CLI provider guarantees to be empty.

The only place outbounds are observable from sagent's side is at
the HTTP entry point. The plugin's ``mcp_sagent/server.py``
subprocess POSTs ``{from, to, body}`` to ``serve.py:/api/post``
every time the model calls ``mcp__sagent_chat__sagent_send``. We
hook that handler to append each outbound to
``agent.runtime.outbound_log`` on the SENDER's runtime — a
``list[{"ts", "to", "body"}]`` that this observer consults to
reconstruct the conversation.

What this observer does
-----------------------

On ``ModelResponseError``:

1. Read ``agent.runtime.outbound_log`` (the per-agent record of
   what this agent has sent via ``/api/post``).
2. Walk ``runtime.tape`` forward. For each ``AgentSendMessage``
   from a peer, find the oldest still-unconsumed outbound to that
   peer in the log (FIFO by target). Pair them and
   ``runtime.append_splice`` a synthetic ``UserMessage``
   immediately AFTER the peer reply:

       "[from sagent runtime] You previously sent to @{src}: \"<content>\""

   The splice lands in ``runtime.context().messages`` right after
   the peer reply, so the respawned CLI subprocess sees a clean
   alternation in its stdin feed.
3. Outbounds in the log with no matching inbound (the peer hasn't
   replied yet) get surfaced in a handoff notice pushed to the
   inbox as "pending delegations".

Duplicate splices on re-fire are prevented via
``_reconstructed_peer_refs`` (per-observer set of peer
``TapeRef``s already spliced) and ``_used_outbound_count_per_target``
(per-target counter that survives across multiple
``ModelResponseError`` events in one session).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)


_NOTICE_TAG = "[handoff from previous session]"
_OUTBOUND_RECON_TAG = "[from sagent runtime] You previously sent"


@dataclass
class RestartNoticeObserver:
    """Watch for ``ModelResponseError`` and reconstruct outbound context.

    Attach via ``agent.runtime.observers.append(observer)``. The
    observer reads each event and acts only on
    ``ModelResponseError``.
    """

    agent_label: str
    """Label of the agent we observe."""

    _reconstructed_peer_refs: set = field(default_factory=set)
    """``TapeRef`` of every ``AgentSendMessage`` we've already
    spliced. Skipped on re-fire to prevent duplicates."""

    _used_outbound_count_per_target: dict = field(default_factory=dict)
    """``target → int`` count of outbound-log entries already paired
    with a peer reply for that target. Advances even on already-
    reconstructed refs so re-fire pairs new replies with the right
    (newer) outbound, not a stale one."""

    def __call__(self, event) -> None:
        from sagent.types.runtime import (
            AgentSendMessage,
            AssistantMessage,
            ModelResponseError,
            UserMessage,
        )

        if not isinstance(event, ModelResponseError):
            return

        from sagent.tools.core import agent_registry

        target = agent_registry.get(self.agent_label)
        if target is None:
            _LOG.warning(
                "RestartNoticeObserver: agent %r not in registry; "
                "cannot push restart notice",
                self.agent_label,
            )
            return

        runtime = target.runtime
        outbound_log = list(getattr(runtime, "outbound_log", None) or [])
        tape = getattr(runtime, "tape", None) or []

        if not outbound_log:
            _LOG.info(
                "RestartNoticeObserver: @%s outbound_log empty; "
                "silent restart (no outbounds to reconstruct)",
                self.agent_label,
            )
            return

        # Group outbounds by target, preserving submission order.
        outbounds_by_target: dict[str, list[dict]] = defaultdict(list)
        for entry in outbound_log:
            tgt = entry.get("to")
            if isinstance(tgt, str) and tgt:
                outbounds_by_target[tgt].append(entry)

        # Walk the tape forward. For each AgentSendMessage we haven't
        # paired yet, take the next unconsumed outbound to that source
        # and splice a reconstruction note after the peer reply.
        splices_added = 0
        skipped_already_done = 0
        used_per_target = self._used_outbound_count_per_target

        for record in tape:
            entry = getattr(record, "event", None)
            if entry is None:
                continue
            if not isinstance(entry, AgentSendMessage):
                continue
            src = getattr(entry, "source", None)
            if not src or src == self.agent_label:
                continue

            ref = getattr(record, "ref", None)
            if ref is not None and ref in self._reconstructed_peer_refs:
                # Already spliced on a prior fire. Don't advance the
                # outbound counter — the counter was already advanced
                # when we did the original splice.
                skipped_already_done += 1
                continue

            consumed = used_per_target.get(src, 0)
            candidates = outbounds_by_target.get(src, [])
            if consumed >= len(candidates):
                # No outbound to pair (peer sent unsolicited message,
                # or we've already used all our outbounds to this
                # target). Leave the peer reply un-augmented.
                continue
            outbound = candidates[consumed]
            content = outbound.get("body", "") or ""
            if not content.strip():
                used_per_target[src] = consumed + 1
                continue

            # Trim outbound bodies so the splice doesn't bloat context.
            snippet = content if len(content) <= 800 else content[:800] + "…"
            # Splice payload is a (boundary AssistantMessage, recon
            # UserMessage) pair rather than a bare UserMessage:
            # ``AgentSendMessage`` is user-side, and inserting our
            # UserMessage straight after it would produce two
            # consecutive user-side entries — a role-alternation
            # violation that the runtime then has to repair via a
            # rescue barrier. The boundary AssistantMessage satisfies
            # alternation cleanly (mirrors the convention sagent's
            # ``coalesce_inbox=False`` override uses for the same
            # reason).
            boundary = AssistantMessage(
                text="(runtime: outbound-reconstruction boundary)",
                tool_calls=(),
            )
            note = UserMessage(
                text=f'{_OUTBOUND_RECON_TAG} to @{src}: "{snippet}"',
            )
            try:
                runtime.append_splice(
                    insert_after=ref,
                    payload=(boundary, note),
                    strategy="restart_notice.outbound_reconstruction",
                    fallback_reason=(
                        "reconstruct outbound from agent.runtime."
                        "outbound_log (CLI provider strips tool_use)"
                    ),
                )
                if ref is not None:
                    self._reconstructed_peer_refs.add(ref)
                used_per_target[src] = consumed + 1
                splices_added += 1
            except Exception as exc:  # noqa: BLE001 -- best-effort
                _LOG.warning(
                    "RestartNoticeObserver: @%s splice failed for "
                    "peer @%s: %s",
                    self.agent_label,
                    src,
                    exc,
                )

        # Any unconsumed outbounds after the walk are orphans (peer
        # hasn't replied yet). Surface in the notice so the model
        # knows it's waiting rather than rerunning.
        orphan_outbounds: list[tuple[str, str]] = []
        for tgt, candidates in outbounds_by_target.items():
            consumed = used_per_target.get(tgt, 0)
            for o in candidates[consumed:]:
                body = o.get("body", "") or ""
                if body.strip():
                    orphan_outbounds.append((tgt, body))

        if splices_added == 0 and not orphan_outbounds:
            _LOG.info(
                "RestartNoticeObserver: @%s nothing to reconstruct "
                "(%d already-done skipped); silent restart",
                self.agent_label,
                skipped_already_done,
            )
            return

        # Build orienting inbox notice.
        parts = [
            _NOTICE_TAG,
            "",
            "Your prior session aborted mid-turn. A fresh subprocess is now active.",
            "",
            "Important: sagent strips your prior AssistantMessages when re-feeding "
            "history (providers/anthropic_cli.py:537). Your text and tool calls are "
            "NOT visible to this respawned subprocess.",
            "",
        ]
        if splices_added > 0:
            parts.append(
                f"To compensate, sagent has spliced {splices_added} "
                f"\"{_OUTBOUND_RECON_TAG} to @<peer>: …\" UserMessages "
                "into history above. Each appears right after the peer "
                "reply it triggered. Treat them as RECOVERED context, "
                "not fresh instructions."
            )
            parts.append("")
            parts.append(
                "Your next action: synthesize the peer replies into a "
                "single response to the original user request. Do NOT "
                "re-issue any sagent_send to peers you've already "
                "delegated to — their replies are above."
            )
        if orphan_outbounds:
            parts.append("")
            parts.append("Pending delegations (no peer reply yet):")
            for tgt, body in orphan_outbounds:
                snippet = body if len(body) <= 240 else body[:240] + "…"
                parts.append(f"  - @{tgt}: \"{snippet}\"")

        body_str = "\n".join(parts)
        _LOG.info(
            "RestartNoticeObserver: @%s ModelResponseError — spliced "
            "%d outbound reconstruction(s) (%d already-done skipped, "
            "%d orphan outbound(s)); pushing handoff notice",
            self.agent_label,
            splices_added,
            skipped_already_done,
            len(orphan_outbounds),
        )
        runtime.inbox.push_back(UserMessage(text=body_str))


def install_on(agent, agent_label: str) -> RestartNoticeObserver:
    """Attach a fresh observer to ``agent.runtime.observers``."""
    observer = RestartNoticeObserver(agent_label=agent_label)
    agent.runtime.observers.append(observer)
    return observer
