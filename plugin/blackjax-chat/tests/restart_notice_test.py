"""Tests for the restart-notice observer."""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class _StubInbox:
    pushed: list = None

    def __post_init__(self):
        if self.pushed is None:
            self.pushed = []

    def push_back(self, item):
        self.pushed.append(item)


@dataclass
class _StubRuntime:
    inbox: _StubInbox = None
    observers: list = None

    def __post_init__(self):
        if self.inbox is None:
            self.inbox = _StubInbox()
        if self.observers is None:
            self.observers = []


@dataclass
class _StubAgent:
    runtime: _StubRuntime = None

    def __post_init__(self):
        if self.runtime is None:
            self.runtime = _StubRuntime()


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
    # Stub the registry lookup so we can fire arbitrary events without
    # depending on a live ``agent_registry``.
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


def test_observer_pushes_restart_notice_on_error():
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError, UserMessage

    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    from sagent.tools.core import agent_registry
    agent_registry["tl"] = agent
    try:
        observer(ModelResponseError(exception=RuntimeError("test")))
        assert len(agent.runtime.inbox.pushed) == 1
        pushed = agent.runtime.inbox.pushed[0]
        assert isinstance(pushed, UserMessage)
        body = pushed.text
        # Check the body has the orienting language we expect.
        assert "[runtime restart notice" in body
        assert "MOST RECENT peer-side message" in body
        assert "DO NOT re-issue" in body
    finally:
        agent_registry.pop("tl", None)


def test_observer_swallows_when_agent_gone_from_registry():
    """If the agent was unregistered between observer install and the
    error event firing (e.g. role was removed at runtime), the
    observer should warn but not crash."""
    from runtime import restart_notice
    from sagent.types.runtime import ModelResponseError

    agent = _StubAgent()
    observer = restart_notice.install_on(agent, "tl")
    # Do NOT add agent to registry; observer should warn and return.
    from sagent.tools.core import agent_registry
    agent_registry.pop("tl", None)
    # Should not raise.
    observer(ModelResponseError(exception=RuntimeError("test")))
    assert agent.runtime.inbox.pushed == []
