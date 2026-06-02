"""Restart-notice runtime observer.

Why this exists
---------------

Sagent's ``_AnthropicCLIModel`` respawns its ``claude --print``
subprocess when the API streams an aborted response
(``aborted_streaming`` / ``ede_diagnostic`` — both seen repeatedly on
2026-06-02). On respawn, sagent re-feeds the full ``agent.history``
to the new subprocess. The conversation state isn't lost.

What IS lost is the model's implicit "I was in the middle of
responding to msg B" pointer. The new subprocess re-reads the full
history and makes a fresh choice about what to do next. With multiple
accumulated peer messages and an incomplete-looking trailing
assistant turn (the one that died mid-stream), opus has a tendency
to anchor on the LARGEST/EARLIEST identifiable user task and redo
its prior work — re-issuing delegations, re-running tool searches,
etc. Observed live 2026-06-02 12:53-12:58 and again 14:05-14:08.

This observer interposes: when ``ModelResponseError`` fires, it
walks ``agent.runtime.history`` backwards to find the agent's LAST
productive activity (an ``AssistantMessage`` with either a
``sagent_send`` tool call or non-empty text). Everything in history
AFTER that point is the "catch-up zone" — inbounds that arrived
since the agent was last productively engaged.

The synthetic notice it pushes lists the catch-up-zone messages
verbatim, in a "[handoff from previous session]" framing that beat
the original "your subprocess restarted" framing in
``/tmp/sagent_probe2/`` (P4 won out of 6 variants).

Why the wording matters
-----------------------

Probe at ``/tmp/sagent_probe2/`` against opus-4-8 found:

  * "Your subprocess just restarted because…" → WRONG_ANCHOR
    (opus reads as a problem state, decides to verify ground truth,
    redoes prior tool calls)
  * "[handoff from previous session] … the most recent message is
    from @<src>: \"<verbatim text>\"" → CORRECT_ANCHOR
    (opus reads as a fresh-context handoff, finds the answer source
    in the conversation above, replies directly)

The wording below replicates the winning framing, with the
catch-up-zone enumeration as a generalisation for cases where
multiple inbounds piled up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)


# Skip catch-up-zone events that ARE prior restart notices we
# injected — otherwise the observer's own notices would be cited as
# "unaddressed inbounds" by the next notice, producing a cascade.
_NOTICE_TAG = "[handoff from previous session]"


@dataclass
class RestartNoticeObserver:
    """Watch for ``ModelResponseError`` and push a catch-up summary.

    Attach via ``agent.runtime.observers.append(observer)``. The
    observer reads each event and acts only on
    ``ModelResponseError``.
    """

    agent_label: str
    """Label of the agent we observe (used for diagnostic logging only)."""

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

        # Read the full history at the moment of error. ``Agent.history`` is
        # a @property that resolves to ``runtime.context().messages`` (see
        # sagent/agent/agent.py:619). It is NOT a plain attribute on the
        # runtime — reaching for ``target.runtime.history`` returns ``None``
        # and silently degenerates to the "skip-silent-restart" path.
        history = list(getattr(target, "history", None) or [])
        if not history:
            _LOG.info(
                "RestartNoticeObserver: @%s history empty; skipping notice",
                self.agent_label,
            )
            return

        # Walk backwards to find the agent's last "productive activity".
        # An AssistantMessage counts as a productive boundary only when it
        # represents OUTBOUND COMMUNICATION:
        #
        #   (a) it contains a ``sagent_send`` tool call (peer/user message),
        #       OR
        #   (b) it contains non-empty text AND NO tool calls at all
        #       (turn-ending text reply — the model finished and addressed
        #       the inbound directly).
        #
        # An AssistantMessage with non-``sagent_send`` tool calls (``Bash``,
        # ``Read``, ``Glob``, …) is INTERMEDIATE WORK, NOT a productive
        # boundary. Treating intermediate tool-using turns as "addressed"
        # was the source of the silent-restart false positive observed
        # on 2026-06-02 17:58: after receiving swe + statistician replies,
        # TL ran a single ``Bash find`` tool call and went idle WITHOUT
        # consolidating + replying to the user — but the old walk-back
        # stopped at that AssistantMessage and decided the catch-up zone
        # was empty.
        catchup_start_idx = 0
        for i in range(len(history) - 1, -1, -1):
            entry = history[i]
            if not isinstance(entry, AssistantMessage):
                continue
            tool_calls = tuple(getattr(entry, "tool_calls", None) or ())
            had_send = any(
                getattr(tc, "name", "") == "sagent_send"
                for tc in tool_calls
            )
            had_text_only = (
                bool((getattr(entry, "text", "") or "").strip())
                and not tool_calls
            )
            if had_send or had_text_only:
                catchup_start_idx = i + 1
                break

        # Collect unaddressed peer/user inbounds in the catch-up zone.
        # Skip:
        #   * prior restart notices (cascade prevention),
        #   * the runtime-synthesised ``"[Error: …]"`` UserMessage that
        #     ``agent/runtime.py:1650`` appends when handling
        #     ModelResponseError — it sits in history right before our
        #     observer fires and isn't a real inbound the agent needs to
        #     address; citing it would inject a self-reference loop.
        unaddressed: list[tuple[str, str]] = []
        for entry in history[catchup_start_idx:]:
            if not isinstance(entry, (UserMessage, AgentSendMessage)):
                continue
            text = (getattr(entry, "text", "") or "").strip()
            if not text or _NOTICE_TAG in text:
                continue
            if text.startswith("[Error:"):
                continue
            src = getattr(entry, "source", None) or "user"
            unaddressed.append((src, text))

        if not unaddressed:
            # No new inbounds to anchor on — silent restart.
            _LOG.info(
                "RestartNoticeObserver: @%s no catch-up inbounds; "
                "silent restart",
                self.agent_label,
            )
            return

        # Format the catch-up summary. For a single inbound, quote it
        # in the "most recent message" form (P4 wording). For multiple,
        # enumerate.
        if len(unaddressed) == 1:
            src, text = unaddressed[0]
            anchor = (
                f"The most recent message in the conversation above is "
                f"from @{src}:\n\n"
                f"    \"{text}\"\n\n"
                f"Your job: answer that message using the context above. "
                f"Stay in plan mode. Don't re-do prior tool calls — "
                f"assume what's in history actually happened."
            )
        else:
            listed_lines = []
            for i, (src, text) in enumerate(unaddressed, 1):
                snippet = text if len(text) <= 400 else text[:400] + "…"
                listed_lines.append(f"  {i}. @{src}: \"{snippet}\"")
            listed = "\n".join(listed_lines)
            anchor = (
                f"The following messages arrived after your last "
                f"successful send and haven't been addressed yet:\n\n"
                f"{listed}\n\n"
                f"Address them in order, using the context above. "
                f"Don't re-do prior tool calls — they already executed."
            )

        body = (
            f"{_NOTICE_TAG}\n\n"
            f"Your prior session ran out of context mid-turn. A fresh "
            f"session is now active, still in plan mode.\n\n"
            f"{anchor}"
        )

        _LOG.info(
            "RestartNoticeObserver: @%s ModelResponseError — injecting "
            "handoff notice with %d catch-up inbound(s)",
            self.agent_label,
            len(unaddressed),
        )
        target.runtime.inbox.push_back(UserMessage(text=body))


def install_on(agent, agent_label: str) -> RestartNoticeObserver:
    """Attach a fresh observer to ``agent.runtime.observers``."""
    observer = RestartNoticeObserver(agent_label=agent_label)
    agent.runtime.observers.append(observer)
    return observer
