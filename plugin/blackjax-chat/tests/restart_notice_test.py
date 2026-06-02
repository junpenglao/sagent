"""Tests for the restart-notice observer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _StubInbox:
    pushed: list = field(default_factory=list)

    def push_back(self, item):
        self.pushed.append(item)


class _StubTapeRecord:
    """Stand-in for sagent's ``ReferrableTapeEvent``: has ``ref`` and ``event``."""

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
    """Minimal runtime: an observable inbox, an observer list, a tape, and
    a captured-splice-calls list (so tests can assert on what we'd splice).
    """

    def __init__(self):
        self.inbox = _StubInbox()
        self.observers = []
        self.tape: list = []
        self.splices: list[_StubSpliceCall] = []

    def append_splice(self, *, insert_after, payload, strategy,
                      fallback_reason="", **_kwargs):
        self.splices.append(
            _StubSpliceCall(insert_after, payload, strategy, fallback_reason),
        )
        return ("synthetic_ref", len(self.splices))


class _StubAgent:
    """Stand-in matching ``sagent.agent.Agent``'s public surface enough for
    the observer.

    The observer reads ``target.runtime`` (for tape + inbox) and
    ``target.runtime.tape`` for the conversation walk. It no longer
    consults ``agent.history``.
    """

    def __init__(self):
        self.runtime = _StubRuntime()


def _seed(agent: _StubAgent, *events) -> None:
    """Append synthetic tape records (ref = sequential int) for each event."""
    for i, ev in enumerate(events, start=len(agent.runtime.tape) + 1):
        agent.runtime.tape.append(_StubTapeRecord(ref=i, event=ev))


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


def test_observer_silent_when_tape_empty():
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError

    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert agent.runtime.inbox.pushed == []
        assert agent.runtime.splices == []
    finally:
        agent_registry.pop("tl", None)


def test_observer_silent_when_no_outbound_in_tape():
    """No outbound sagent_send to pair → no reconstruction → no notice."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="hello"),
        AssistantMessage(text="hi back", tool_calls=()),
        AgentSendMessage(source="swe", text="unsolicited"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        # No outbound was ever recorded, so the peer reply has nothing
        # to pair with — silent restart.
        assert agent.runtime.splices == []
        assert agent.runtime.inbox.pushed == []
    finally:
        agent_registry.pop("tl", None)


def test_observer_splices_outbound_reconstruction_after_peer_reply():
    """Core: agent delegated to swe → swe replied → observer splices the
    outbound content after swe's reply, anchored on swe's tape ref."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="plan a benchmark"),
        AssistantMessage(
            text="Delegating now.",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": "swe",
                               "content": "PLAN ONLY — design the file."}),
            ),
        ),
        AgentSendMessage(source="swe", text="## Implementation plan ..."),
    )
    swe_ref = agent.runtime.tape[2].ref  # the AgentSendMessage record

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.splices) == 1
        splice = agent.runtime.splices[0]
        assert splice.insert_after == swe_ref
        assert splice.strategy == "restart_notice.outbound_reconstruction"
        assert len(splice.payload) == 1
        recon = splice.payload[0]
        assert isinstance(recon, UserMessage)
        assert "[from sagent runtime] You previously sent" in recon.text
        assert "@swe" in recon.text
        assert "PLAN ONLY — design the file." in recon.text

        # And the inbox notice should be present too.
        assert len(agent.runtime.inbox.pushed) == 1
        notice = agent.runtime.inbox.pushed[0].text
        assert "[handoff from previous session]" in notice
        assert "spliced 1" in notice  # one reconstruction summarised
        assert "synthesize" in notice.lower()
    finally:
        agent_registry.pop("tl", None)


def test_observer_splices_multi_target_send():
    """A single AssistantMessage with two sagent_send tool calls → both
    peers' replies get paired splices."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="plan + pick pairs"),
        AssistantMessage(
            text="Delegating.",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": "swe", "content": "design the file"}),
                ToolCall(id="t2", name="sagent_send",
                         args={"to": "statistician", "content": "pick 3 pairs"}),
            ),
        ),
        AgentSendMessage(source="swe", text="here's the plan"),
        AgentSendMessage(source="statistician", text="here are 3 pairs"),
    )
    swe_ref = agent.runtime.tape[2].ref
    stat_ref = agent.runtime.tape[3].ref

    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.splices) == 2
        # Order matches tape order.
        assert agent.runtime.splices[0].insert_after == swe_ref
        assert "design the file" in agent.runtime.splices[0].payload[0].text
        assert agent.runtime.splices[1].insert_after == stat_ref
        assert "pick 3 pairs" in agent.runtime.splices[1].payload[0].text
        # Notice mentions both.
        notice = agent.runtime.inbox.pushed[0].text
        assert "spliced 2" in notice
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_already_reconstructed_refs_on_refire():
    """Multiple ModelResponseError firings shouldn't double-splice peers
    we've already reconstructed."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="task"),
        AssistantMessage(
            text="delegating",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": "swe", "content": "do thing"}),
            ),
        ),
        AgentSendMessage(source="swe", text="reply"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("first")))
        assert len(agent.runtime.splices) == 1
        # Re-fire: same tape, no new pairs, so no new splice.
        observer(ModelResponseError(exception=RuntimeError("second")))
        assert len(agent.runtime.splices) == 1, (
            "duplicate splice on re-fire — observer must track "
            "already-reconstructed peer refs"
        )
        # The second fire produces a no-op notice path; no new inbox push.
        assert len(agent.runtime.inbox.pushed) == 1
    finally:
        agent_registry.pop("tl", None)


def test_observer_lists_pending_orphan_outbounds_in_notice():
    """An outbound with no peer reply yet should appear in the notice as
    a pending delegation — the model needs to know it's waiting on
    that peer, not re-issue."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="delegate"),
        AssistantMessage(
            text="delegating",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": "swe", "content": "do the thing"}),
            ),
        ),
        # No swe reply yet.
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        # No splice because there's no peer reply to anchor on.
        assert agent.runtime.splices == []
        # But the notice should surface the orphan.
        assert len(agent.runtime.inbox.pushed) == 1
        notice = agent.runtime.inbox.pushed[0].text
        assert "Pending delegations" in notice
        assert "@swe" in notice
        assert "do the thing" in notice
    finally:
        agent_registry.pop("tl", None)


def test_observer_handles_list_target_in_sagent_send_args():
    """``args.to`` can be a list of labels (multi-cast). Each label
    becomes its own outbound entry."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="multicast"),
        AssistantMessage(
            text="sending to both",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": ["swe", "statistician"],
                               "content": "you both pick this up"}),
            ),
        ),
        AgentSendMessage(source="swe", text="got it"),
        AgentSendMessage(source="statistician", text="me too"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.splices) == 2
    finally:
        agent_registry.pop("tl", None)


def test_observer_ignores_non_sagent_send_tool_calls():
    """``Bash``, ``Read``, ``Glob`` tool calls must NOT be treated as
    outbounds. They never trigger peer replies."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    _seed(
        agent,
        UserMessage(text="plz read"),
        AssistantMessage(
            text="checking",
            tool_calls=(
                ToolCall(id="t1", name="Bash", args={"command": "ls"}),
                ToolCall(id="t2", name="Read", args={"path": "/tmp/x"}),
            ),
        ),
        AgentSendMessage(source="swe", text="unrelated reply"),
    )
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        # Nothing to pair — no splice, no notice.
        assert agent.runtime.splices == []
        assert agent.runtime.inbox.pushed == []
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
