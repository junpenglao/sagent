"""Restart-notice runtime observer.

Why this exists
---------------

Sagent's ``_AnthropicCLIModel`` respawns its ``claude --print``
subprocess when the API streams an aborted response
(``aborted_streaming`` / ``ede_diagnostic`` — both seen repeatedly on
2026-06-02). On respawn, sagent re-feeds the full ``agent.history``
to the new subprocess. The conversation state isn't lost.

What IS lost is the model's implicit "I'm in the middle of responding
to msg B" pointer. The new subprocess re-reads the full history and
makes a fresh choice about what to do next. With multiple
accumulated peer messages and an incomplete-looking trailing
assistant turn (the one that died mid-stream), opus has a tendency
to anchor on the LARGEST/EARLIEST identifiable user task and redo
its prior work — re-issuing delegations, re-running tool searches,
etc. Observed live 2026-06-02 12:53-12:58 (TL re-sent two delegation
messages to SWE and statistician after three consecutive
``ModelResponseError`` events).

This observer interposes: when ``ModelResponseError`` fires, it
pushes a synthetic :class:`UserMessage` with an orienting body into
the agent's inbox. The next drain cycle picks it up, and with
``coalesce_inbox=False`` it arrives as a discrete inbound the model
can't ignore.

Wording goals for the restart prompt
------------------------------------

- Tell the model EXPLICITLY that a respawn happened.
- Anchor it to the most recent peer-side message instead of letting it
  pick across all accumulated inbounds.
- Tell it NOT to re-issue prior structured tool calls — those have
  already executed.
- Keep it short so it doesn't bloat the context that the model has to
  re-read on every error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)


# Body of the synthetic UserMessage pushed after a ModelResponseError.
# Tuned to be: short, unambiguous, instruction-shaped. Tested first
# 2026-06-02 against the TL-restart-confusion scenario.
_RESTART_NOTICE = (
    "[runtime restart notice — read carefully before acting]\n\n"
    "Your claude subprocess just restarted because the previous turn's "
    "API streaming errored out. The conversation above is your full "
    "context — sagent re-fed it for you. Before taking any action:\n\n"
    "1. Identify the MOST RECENT peer-side message in your history "
    "(the inbound immediately above this notice, or above the synthetic "
    "'(runtime: discrete-inbound boundary)' marker if one is present). "
    "That is the message you should respond to.\n\n"
    "2. Look at the assistant turns ABOVE that message. If you can see "
    "evidence that you already issued structured tool calls "
    "(`mcp__sagent_chat__sagent_send` etc.) or shell commands in "
    "response to earlier user/peer messages, DO NOT re-issue those. "
    "They have already been executed. The runtime preserves the audit "
    "trail; assume what's in history actually happened.\n\n"
    "3. If you cannot identify any in-flight task that needs your "
    "response right now, briefly acknowledge to the most recent sender "
    "via `mcp__sagent_chat__sagent_send` that you're back online and "
    "waiting for direction.\n\n"
    "4. Do NOT re-do worklog reads, file searches, or repo inspections "
    "that you can see evidence of in your history. That work happened "
    "in the prior subprocess; the results are already integrated.\n\n"
    "Act ONLY on the most recent peer message. Resume now."
)


@dataclass
class RestartNoticeObserver:
    """Watch for ``ModelResponseError`` and push an orienting UserMessage.

    Attach via ``agent.runtime.observers.append(observer)``. The
    observer reads each event and acts only on ``ModelResponseError``.
    """

    agent_label: str
    """Label of the agent we observe (used for diagnostic logging only)."""

    def __call__(self, event) -> None:
        from sagent.types.runtime import ModelResponseError, UserMessage

        if not isinstance(event, ModelResponseError):
            return

        # Push the restart notice into THIS agent's own inbox. The
        # runtime's drain loop picks it up as a UserMessage and the
        # model treats it as a discrete inbound (with
        # coalesce_inbox=False, it doesn't get merged into the prior
        # peer message). It will be the LAST user-side entry in
        # history when the new subprocess starts reading.
        from sagent.tools.core import agent_registry

        target = agent_registry.get(self.agent_label)
        if target is None:
            _LOG.warning(
                "RestartNoticeObserver: agent %r not in registry; "
                "cannot push restart notice",
                self.agent_label,
            )
            return

        _LOG.info(
            "RestartNoticeObserver: ModelResponseError on @%s — "
            "injecting restart-orient UserMessage",
            self.agent_label,
        )
        target.runtime.inbox.push_back(UserMessage(text=_RESTART_NOTICE))


def install_on(agent, agent_label: str) -> RestartNoticeObserver:
    """Attach a fresh observer to ``agent.runtime.observers``."""
    observer = RestartNoticeObserver(agent_label=agent_label)
    agent.runtime.observers.append(observer)
    return observer
