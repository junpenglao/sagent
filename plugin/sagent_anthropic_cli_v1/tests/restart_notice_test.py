"""Tests for the restart-notice observer.

These tests model the REAL sagent CLI data flow:

* ``AssistantMessage.tool_calls`` is always ``()`` (anthropic_cli.py:924
  — the CLI provider runs MCP tool round-trips opaquely; tool_use
  blocks don't propagate to sagent).
* The only observable record of an outbound ``sagent_send`` is at
  the HTTP entry point. ``serve.py`` populates
  ``agent.runtime.outbound_log`` from ``/api/post`` on the SENDER's
  runtime.
* Peer replies arrive on the recipient's runtime as
  ``AgentSendMessage`` events in the tape, with ``source`` = the
  sender's role label.

The observer reads the SENDER's ``outbound_log`` and pairs each
entry FIFO-by-target with the corresponding ``AgentSendMessage``
in the tape.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _StubInbox:
    pushed: list = field(default_factory=list)

    def push_back(self, item):
        self.pushed.append(item)


class _StubTapeRecord:
    """``ReferrableTapeEvent`` stand-in: has ``ref`` and ``event``."""

    def __init__(self, ref, event):
        self.ref = ref
        self.event = event


class _StubSpliceCall:
    """Capture an ``append_splice`` invocation for assertions."""

    def __init__(self, insert_after, payload, strategy, fallback_reason):
        self.insert_after = insert_after
        self.payload = payload
        self.strategy = strategy
        self.fallback_reason = fallback_reason


class _StubRuntime:
    """Minimal runtime exposing what the observer touches:
    inbox, observers, tape, outbound_log, and ``append_splice``."""

    def __init__(self):
        self.inbox = _StubInbox()
        self.observers = []
        self.tape: list = []
        self.outbound_log: list[dict] = []
        self.splices: list[_StubSpliceCall] = []

    def append_splice(self, *, insert_after, payload, strategy,
                      fallback_reason="", **_kwargs):
        self.splices.append(
            _StubSpliceCall(insert_after, payload, strategy, fallback_reason),
        )
        return ("synthetic_ref", len(self.splices))


class _StubAgent:
    def __init__(self):
        self.runtime = _StubRuntime()


def _seed_tape(agent: _StubAgent, *events) -> list:
    """Append synthetic tape records and return the list of refs."""
    refs = []
    for ev in events:
        ref = len(agent.runtime.tape) + 1
        agent.runtime.tape.append(_StubTapeRecord(ref=ref, event=ev))
        refs.append(ref)
    return refs


def _outbound(to: str, body: str, ts: str = "2026-06-02T00:00:00Z") -> dict:
    return {"ts": ts, "to": to, "body": body}


def test_install_attaches_observer():
    from runtime import restart_notice
    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    assert observer in agent.runtime.observers


def test_observer_ignores_non_error_events():
    from runtime import restart_notice
    from sagent.types.runtime import AgentIdle, ModelCallStarted

    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelCallStarted())
        observer(AgentIdle())
        assert agent.runtime.inbox.pushed == []
        assert agent.runtime.splices == []
    finally:
        agent_registry.pop("tl", None)


def test_observer_silent_when_outbound_log_empty():
    """No outbounds recorded → nothing to reconstruct → silent."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    _seed_tape(
        agent,
        UserMessage(text="hi"),
        # Peer message but we never sent anything → no reconstruction.
        AgentSendMessage(source="swe", text="unsolicited reply"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert agent.runtime.inbox.pushed == []
        assert agent.runtime.splices == []
    finally:
        agent_registry.pop("tl", None)


def test_observer_splices_after_paired_peer_reply():
    """The core: outbound recorded in log + peer reply in tape →
    splice a (boundary AssistantMessage, reconstruction UserMessage)
    pair after the peer reply.

    Payload is a PAIR rather than a bare UserMessage because
    AgentSendMessage is user-side; inserting a UserMessage straight
    after it would violate role alternation and trigger sagent's
    rescue-barrier code path. The boundary AssistantMessage keeps the
    alternation clean."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.runtime.outbound_log = [
        _outbound("swe", "PLAN ONLY — design the file."),
    ]
    refs = _seed_tape(
        agent,
        UserMessage(text="plan a benchmark"),
        AgentSendMessage(source="swe", text="## Implementation plan ..."),
    )
    swe_ref = refs[1]

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.splices) == 1
        splice = agent.runtime.splices[0]
        assert splice.insert_after == swe_ref
        assert splice.strategy == "restart_notice.outbound_reconstruction"
        # Two-entry payload: boundary AssistantMessage, then the
        # reconstruction UserMessage.
        assert len(splice.payload) == 2
        boundary, recon = splice.payload
        assert isinstance(boundary, AssistantMessage)
        assert "outbound-reconstruction boundary" in boundary.text
        assert isinstance(recon, UserMessage)
        assert "[from sagent runtime] You previously sent" in recon.text
        assert "@swe" in recon.text
        assert "PLAN ONLY — design the file." in recon.text
        # Inbox notice mentions the splice + synthesize directive.
        notice = agent.runtime.inbox.pushed[0].text
        assert "[handoff from previous session]" in notice
        assert "spliced 1" in notice
        assert "synthesize" in notice.lower()
    finally:
        agent_registry.pop("tl", None)


def test_observer_pairs_multiple_outbounds_fifo_by_target():
    """Two outbounds to @swe, then two replies from @swe → pair in order."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        ModelResponseError,
    )

    agent = _StubAgent()
    agent.runtime.outbound_log = [
        _outbound("swe", "first task", ts="2026-06-02T10:00:00Z"),
        _outbound("statistician", "pick pairs", ts="2026-06-02T10:00:05Z"),
        _outbound("swe", "second task", ts="2026-06-02T10:01:00Z"),
    ]
    refs = _seed_tape(
        agent,
        AgentSendMessage(source="swe", text="first reply"),
        AgentSendMessage(source="statistician", text="pairs picked"),
        AgentSendMessage(source="swe", text="second reply"),
    )

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.splices) == 3
        # FIFO-by-target. Each splice payload is (boundary, recon); the
        # recon UserMessage at payload[1] carries the outbound content.
        assert agent.runtime.splices[0].insert_after == refs[0]
        assert "first task" in agent.runtime.splices[0].payload[1].text
        assert agent.runtime.splices[1].insert_after == refs[1]
        assert "pick pairs" in agent.runtime.splices[1].payload[1].text
        assert agent.runtime.splices[2].insert_after == refs[2]
        assert "second task" in agent.runtime.splices[2].payload[1].text
    finally:
        agent_registry.pop("tl", None)


def test_observer_surfaces_orphan_outbound_no_reply_yet():
    """Outbound recorded but peer hasn't replied → no splice, but
    notice mentions as pending delegation."""
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError, UserMessage

    agent = _StubAgent()
    agent.runtime.outbound_log = [
        _outbound("swe", "do the thing"),
    ]
    _seed_tape(agent, UserMessage(text="delegate"))

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert agent.runtime.splices == []
        notice = agent.runtime.inbox.pushed[0].text
        assert "Pending delegations" in notice
        assert "@swe" in notice
        assert "do the thing" in notice
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_unsolicited_peer_message():
    """Peer sends a message with no matching outbound in log → no
    splice for that peer (we don't fabricate an outbound that never
    happened)."""
    from runtime import restart_notice
    from sagent.types.runtime import AgentSendMessage, ModelResponseError

    agent = _StubAgent()
    agent.runtime.outbound_log = [_outbound("swe", "do X")]
    refs = _seed_tape(
        agent,
        AgentSendMessage(source="swe", text="swe reply"),
        AgentSendMessage(source="statistician", text="unsolicited"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        # Only swe gets paired; statistician's unsolicited message
        # has no outbound to anchor on → no splice for it.
        assert len(agent.runtime.splices) == 1
        assert agent.runtime.splices[0].insert_after == refs[0]
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_already_reconstructed_refs_on_refire():
    """Re-fire after more outbounds + replies arrive: only the new
    peer ref gets a splice; old one stays put with its old outbound."""
    from runtime import restart_notice
    from sagent.types.runtime import AgentSendMessage, ModelResponseError

    agent = _StubAgent()
    agent.runtime.outbound_log = [_outbound("swe", "first")]
    refs = _seed_tape(agent, AgentSendMessage(source="swe", text="first reply"))

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("first fire")))
        assert len(agent.runtime.splices) == 1
        assert "first" in agent.runtime.splices[0].payload[1].text

        # New activity between fires: another outbound + reply.
        agent.runtime.outbound_log.append(_outbound("swe", "second"))
        new_refs = _seed_tape(
            agent, AgentSendMessage(source="swe", text="second reply")
        )

        observer(ModelResponseError(exception=RuntimeError("second fire")))
        assert len(agent.runtime.splices) == 2
        # The NEW splice pairs the NEW reply with the NEW outbound.
        assert agent.runtime.splices[1].insert_after == new_refs[0]
        assert "second" in agent.runtime.splices[1].payload[1].text
        # The OLD ref isn't double-spliced.
        old_splice_count_after_refire = sum(
            1 for s in agent.runtime.splices if s.insert_after == refs[0]
        )
        assert old_splice_count_after_refire == 1
    finally:
        agent_registry.pop("tl", None)


def test_observer_handles_outbound_to_user_as_orphan():
    """Outbounds to ``user`` never get a paired AgentSendMessage
    reply (user doesn't send via the peer channel). They should
    surface as orphan rather than crash."""
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError, UserMessage

    agent = _StubAgent()
    agent.runtime.outbound_log = [_outbound("user", "here's the answer")]
    _seed_tape(agent, UserMessage(text="ask"))

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert agent.runtime.splices == []
        notice = agent.runtime.inbox.pushed[0].text
        assert "@user" in notice
        assert "here's the answer" in notice
    finally:
        agent_registry.pop("tl", None)


def test_observer_swallows_when_agent_gone_from_registry():
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError

    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry.pop("tl", None)
    observer(ModelResponseError(exception=RuntimeError("test")))
    assert agent.runtime.inbox.pushed == []
    assert agent.runtime.splices == []
