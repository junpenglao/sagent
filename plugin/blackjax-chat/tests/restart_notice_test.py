"""Tests for the restart-notice observer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _StubInbox:
    pushed: list = field(default_factory=list)

    def push_back(self, item):
        self.pushed.append(item)


@dataclass
class _StubRuntime:
    inbox: _StubInbox = field(default_factory=_StubInbox)
    observers: list = field(default_factory=list)


class _StubAgent:
    """Stand-in matching ``sagent.agent.Agent``'s public surface.

    ``Agent.history`` is a @property over ``runtime.context().messages``
    (agent.py:619). The observer reads ``target.history``, so the stub
    exposes it as a plain attribute set by the test.
    """

    def __init__(self):
        self.runtime = _StubRuntime()
        self.history = []


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
        assert agent.runtime.inbox.pushed == [], (
            f"non-error events must not push to inbox; "
            f"got {agent.runtime.inbox.pushed!r}"
        )
    finally:
        agent_registry.pop("tl", None)


def test_observer_silent_when_history_empty():
    """Empty history → no catch-up zone → no notice pushed."""
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError

    agent = _StubAgent()  # history=[] by default
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert agent.runtime.inbox.pushed == [], (
            "empty history must produce silent restart; "
            f"got {agent.runtime.inbox.pushed!r}"
        )
    finally:
        agent_registry.pop("tl", None)


def test_observer_quotes_single_unaddressed_message():
    """One inbound after the last AssistantMessage → quote it
    verbatim in the 'most recent message' form."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="hello"),
        AssistantMessage(text="hi back"),
        UserMessage(text="please answer this question"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.inbox.pushed) == 1
        pushed = agent.runtime.inbox.pushed[0]
        assert isinstance(pushed, UserMessage)
        body = pushed.text
        assert "[handoff from previous session]" in body
        assert "The most recent message" in body
        assert "@user" in body
        assert '"please answer this question"' in body
        # Should NOT enumerate (only one inbound).
        assert "1. @" not in body
    finally:
        agent_registry.pop("tl", None)


def test_observer_enumerates_multiple_unaddressed_messages():
    """Multiple inbounds after the last AssistantMessage → numbered list."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="first task"),
        AssistantMessage(text="working on it"),
        AgentSendMessage(source="swe", text="here's my reply"),
        AgentSendMessage(source="statistician", text="and mine"),
        UserMessage(text="follow-up question"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.inbox.pushed) == 1
        body = agent.runtime.inbox.pushed[0].text
        assert "[handoff from previous session]" in body
        assert "1. @swe:" in body
        assert "2. @statistician:" in body
        assert "3. @user:" in body
        assert "here's my reply" in body
        assert "and mine" in body
        assert "follow-up question" in body
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_prior_restart_notices():
    """A prior restart notice in history must NOT be cited as
    'unaddressed', else successive errors cascade."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="original prompt"),
        AssistantMessage(text="working"),
        UserMessage(
            text=(
                "[handoff from previous session]\n\nleftover notice from a "
                "prior error — must be skipped"
            ),
        ),
        UserMessage(text="real new question"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        body = agent.runtime.inbox.pushed[0].text
        # Should cite ONLY the real new question, not the prior notice.
        assert "real new question" in body
        assert "leftover notice" not in body
    finally:
        agent_registry.pop("tl", None)


def test_observer_stops_at_assistant_with_sagent_send():
    """An AssistantMessage with a sagent_send tool call counts as
    a productive activity boundary."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="please delegate"),
        AssistantMessage(
            text="",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send", args={"to": "swe", "content": "..."}),
            ),
        ),
        UserMessage(text="new inbound after the send"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        body = agent.runtime.inbox.pushed[0].text
        assert "new inbound after the send" in body
        assert "please delegate" not in body  # before the boundary
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_intermediate_bash_tool_call():
    """An AssistantMessage with non-``sagent_send`` tool calls (Bash, Read,
    Glob…) is intermediate work, NOT a productive boundary.

    Repro of the 2026-06-02 17:58 silent-restart false positive: after
    receiving swe + statistician replies, TL ran a ``Bash find`` tool
    call and went idle without consolidating + replying. The error fired
    next. The catch-up zone MUST still include the swe/statistician
    inbounds — they aren't addressed yet just because TL ran one Bash.
    """
    from runtime import restart_notice
    from sagent.types.runtime import (
        AgentSendMessage,
        AssistantMessage,
        ModelResponseError,
        ToolCall,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="plan a benchmark"),
        AssistantMessage(
            text="Delegating to swe and statistician.",
            tool_calls=(
                ToolCall(id="t1", name="sagent_send",
                         args={"to": "swe", "content": "..."}),
                ToolCall(id="t2", name="sagent_send",
                         args={"to": "statistician", "content": "..."}),
            ),
        ),
        AgentSendMessage(source="swe", text="swe's implementation plan"),
        AgentSendMessage(source="statistician", text="statistician's pairs"),
        # Intermediate Bash tool call — MUST NOT count as productive.
        AssistantMessage(
            text="Let me check the catalog structure.",
            tool_calls=(
                ToolCall(id="t3", name="Bash",
                         args={"command": "find tuningfork/catalog -name '*.json'"}),
            ),
        ),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.inbox.pushed) == 1, (
            "observer must push exactly one notice; "
            f"got {agent.runtime.inbox.pushed!r}"
        )
        body = agent.runtime.inbox.pushed[0].text
        assert "[handoff from previous session]" in body
        assert "swe's implementation plan" in body
        assert "statistician's pairs" in body
        # The intermediate AssistantMessage must NOT have been treated as
        # a productive boundary, so we DON'T skip past the peer replies.
        assert "@swe" in body
        assert "@statistician" in body


    finally:
        agent_registry.pop("tl", None)


def test_observer_stops_at_text_only_assistant_reply():
    """An AssistantMessage with text AND no tool calls IS a productive
    boundary — that's the model's turn-ending text reply."""
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="explain X"),
        # Text-only final reply: counts as productive.
        AssistantMessage(text="X is …", tool_calls=()),
        UserMessage(text="follow-up Y"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        body = agent.runtime.inbox.pushed[0].text
        assert "follow-up Y" in body
        assert "explain X" not in body
    finally:
        agent_registry.pop("tl", None)


def test_observer_skips_runtime_error_user_message():
    """When ``ModelResponseError`` is handled by the runtime, it appends
    a synthetic ``UserMessage("[Error: …]")`` BEFORE publishing the event
    (sagent/agent/runtime.py:1650). That synthetic message must NOT be
    cited as an unaddressed inbound — it isn't a real peer/user message
    and citing it would inject a self-reference loop.
    """
    from runtime import restart_notice
    from sagent.types.runtime import (
        AssistantMessage,
        ModelResponseError,
        UserMessage,
    )

    agent = _StubAgent()
    agent.history = [
        UserMessage(text="please answer"),
        AssistantMessage(text="working on it"),
        UserMessage(text="real new question"),
        # Runtime-synthesised "[Error: …]" appended by runtime.py:1650
        # right before our observer fires.
        UserMessage(text="[Error: SubprocessTransportError: aborted_streaming]"),
    ]
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.inbox.pushed) == 1
        body = agent.runtime.inbox.pushed[0].text
        assert "real new question" in body
        # The synthetic error pseudo-inbound must not appear in the notice.
        assert "[Error:" not in body
        assert "SubprocessTransportError" not in body
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
