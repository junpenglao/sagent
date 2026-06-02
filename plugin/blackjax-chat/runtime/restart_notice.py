"""Restart-notice runtime observer.

Why this exists
---------------

Sagent's ``_AnthropicCLIModel`` respawns its ``claude --print``
subprocess when the API streams an aborted response
(``aborted_streaming`` / ``ede_diagnostic`` — both seen repeatedly on
2026-06-02). On respawn, sagent re-feeds the full ``agent.history``
to the new subprocess … EXCEPT the ``AssistantMessage`` entries:
``providers/anthropic_cli.py:537`` explicitly filters them out before
writing to stdin. The new subprocess sees ONLY ``UserMessage`` and
``AgentSendMessage`` entries.

That means the model's own prior outputs — text, ``sagent_send`` tool
calls, internal ``Bash``/``Read``/``Glob`` tool calls — are
**invisible** to the respawned subprocess. Peer replies look like
they arrived "out of nowhere" with no triggering delegation. The
fresh subprocess then has to guess what's going on from user-side
messages alone, and the typical guess is "the original user task
hasn't been started; let me delegate" — even when delegations have
already happened and peer replies are already in.

What this observer does
-----------------------

When ``ModelResponseError`` fires, it walks ``runtime.tape``
forward, tracking outbound ``sagent_send`` arguments from each
``AssistantMessage`` it sees (mapping ``target → content``). For
every ``AgentSendMessage`` it encounters from a peer whose source
matches a known outbound, it ``runtime.append_splice``-es a
synthetic ``UserMessage`` immediately AFTER the peer message
reconstructing the prior outbound:

    "[from sagent runtime] You previously sent to @{src}: \"<content>\""

These splices land in ``runtime.context().messages`` at the right
chronological position (right after the peer reply), so the
respawned CLI subprocess sees a clean alternation:

    user: "<original task>"
    user (from @swe): "<swe's reply>"
    user: "[from sagent runtime] You previously sent to @swe: …"
    user (from @statistician): "<stat's reply>"
    user: "[from sagent runtime] You previously sent to @statistician: …"

The model is no longer guessing what triggered the peer replies —
it sees the (reconstructed) outbound right next to each one and
naturally pairs them.

The observer ALSO pushes a short ``UserMessage`` notice onto the
inbox tagged ``[handoff from previous session]``. Because the
conversation reconstruction is already in history, the notice's
only job is to remind the respawned subprocess that the splices
above are recovered context rather than fresh user input, and to
point at the next action (synthesize, not re-delegate).

Duplicate splices are avoided via ``_reconstructed_peer_refs`` —
a per-observer set of peer ``TapeRef``s we've already spliced
after. Subsequent ``ModelResponseError`` fires re-walk the tape
but only splice new pairs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)


# Tagged on the inbox UserMessage notice + on each splice payload so
# we can identify them in history without false positives.
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
    """Label of the agent we observe (used for diagnostic logging only)."""

    _reconstructed_peer_refs: set = field(default_factory=set)
    """``TapeRef`` of every ``AgentSendMessage`` we've already
    spliced a reconstruction after. Prevents duplicate splices when
    multiple ``ModelResponseError`` events fire in one session."""

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
        tape = getattr(runtime, "tape", None)
        if not tape:
            _LOG.info(
                "RestartNoticeObserver: @%s tape empty; nothing to "
                "reconstruct",
                self.agent_label,
            )
            return

        # Walk the tape forward, tracking the most recent outbound
        # ``sagent_send`` content per target. When we hit an
        # ``AgentSendMessage`` whose source matches a tracked outbound,
        # splice a reconstruction note after it.
        #
        # We dispatch on ``hasattr`` rather than ``isinstance`` to
        # tolerate the tape having both ``ReferrableTapeEvent``
        # (which has ``.event``) and ``ContextSplice`` (which has
        # ``.payload``) records — we only care about the former.
        last_outbound_per_peer: dict[str, str] = {}
        splices_added = 0
        already_reconstructed = 0
        for record in tape:
            entry = getattr(record, "event", None)
            if entry is None:
                # ContextSplice or another non-event tape record; skip.
                continue

            if isinstance(entry, AssistantMessage):
                for tc in getattr(entry, "tool_calls", None) or ():
                    if getattr(tc, "name", "") != "sagent_send":
                        continue
                    args = getattr(tc, "args", None) or {}
                    to = args.get("to")
                    content = args.get("content", "") or ""
                    if not isinstance(content, str) or not content.strip():
                        continue
                    if isinstance(to, list):
                        for label in to:
                            if isinstance(label, str) and label:
                                last_outbound_per_peer[label] = content
                    elif isinstance(to, str) and to:
                        last_outbound_per_peer[to] = content
                continue

            if isinstance(entry, AgentSendMessage):
                src = getattr(entry, "source", None)
                if not src or src == self.agent_label:
                    continue
                # Skip if we've already spliced this peer ref. We still
                # consume the outbound (pop) so it doesn't show up as an
                # "orphan" in the notice on every re-fire.
                ref = getattr(record, "ref", None)
                if ref is not None and ref in self._reconstructed_peer_refs:
                    last_outbound_per_peer.pop(src, None)
                    already_reconstructed += 1
                    continue
                outbound = last_outbound_per_peer.pop(src, None)
                if outbound is None:
                    continue

                # Trim very long outbound bodies so the splice doesn't
                # bloat the context window. The full content is still in
                # the (stripped) AssistantMessage's tool_calls; this is
                # an aide-memoire, not the source of truth.
                snippet = outbound if len(outbound) <= 800 else outbound[:800] + "…"
                note = UserMessage(
                    text=f'{_OUTBOUND_RECON_TAG} to @{src}: "{snippet}"',
                )
                try:
                    runtime.append_splice(
                        insert_after=ref,
                        payload=(note,),
                        strategy="restart_notice.outbound_reconstruction",
                        fallback_reason=(
                            "reconstruct stripped AssistantMessage's "
                            "sagent_send tool call for respawned subprocess"
                        ),
                    )
                    if ref is not None:
                        self._reconstructed_peer_refs.add(ref)
                    splices_added += 1
                except Exception as exc:  # noqa: BLE001 -- best-effort
                    _LOG.warning(
                        "RestartNoticeObserver: @%s splice failed for "
                        "peer @%s: %s",
                        self.agent_label,
                        src,
                        exc,
                    )

        # Outbounds that never matched an inbound are still "in flight"
        # (peer hasn't replied yet). We don't splice for those — there's
        # no peer message to anchor after.
        orphan_outbounds = list(last_outbound_per_peer.items())

        # Always push a short orienting notice onto the inbox so the
        # respawned subprocess KNOWS the splices in history are
        # reconstructed context rather than fresh user input.
        if splices_added == 0 and not orphan_outbounds:
            _LOG.info(
                "RestartNoticeObserver: @%s nothing to reconstruct; "
                "silent restart",
                self.agent_label,
            )
            return

        parts = [
            _NOTICE_TAG,
            "",
            "Your prior session aborted mid-turn. A fresh subprocess "
            "is now active.",
            "",
            "Important: sagent strips your prior AssistantMessages "
            "when re-feeding history to a respawned subprocess "
            "(providers/anthropic_cli.py:537), so your prior text and "
            "tool calls are NOT visible to you in this session.",
            "",
        ]
        if splices_added > 0:
            parts.append(
                f"To compensate, sagent has spliced {splices_added} "
                f"\"{_OUTBOUND_RECON_TAG} to @<peer>: …\" UserMessages "
                "into history above. Each one appears right after the "
                "peer reply it triggered. Treat these as RECOVERED "
                "context, not fresh instructions."
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
            for label, content in orphan_outbounds:
                snippet = content if len(content) <= 240 else content[:240] + "…"
                parts.append(f"  - @{label}: \"{snippet}\"")

        body = "\n".join(parts)
        _LOG.info(
            "RestartNoticeObserver: @%s ModelResponseError — spliced "
            "%d outbound reconstruction(s) (%d already-done skipped, "
            "%d orphan outbound(s)); pushing handoff notice",
            self.agent_label,
            splices_added,
            already_reconstructed,
            len(orphan_outbounds),
        )
        runtime.inbox.push_back(UserMessage(text=body))


def install_on(agent, agent_label: str) -> RestartNoticeObserver:
    """Attach a fresh observer to ``agent.runtime.observers``."""
    observer = RestartNoticeObserver(agent_label=agent_label)
    agent.runtime.observers.append(observer)
    return observer
