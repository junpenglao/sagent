"""Tests for the in-process delivery helpers used by the MCP server."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_sagent import delivery


class _StubInbox:
    def __init__(self):
        self.pushed = []

    def push_back(self, item):
        self.pushed.append(item)


class _StubRuntime:
    def __init__(self):
        self.inbox = _StubInbox()


class _StubAgent:
    def __init__(self):
        self.runtime = _StubRuntime()


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    log_path = tmp_path / "main.jsonl"
    monkeypatch.setattr(delivery, "MAIN_JSONL_PATH", log_path)
    return log_path


def test_append_record_writes_chat_compatible_line(isolated_log):
    delivery.append_record(from_role="tl", to=["swe"], body="hello")
    line = isolated_log.read_text().strip()
    rec = json.loads(line)
    assert rec["from"] == "tl"
    assert rec["to"] == ["swe"]
    assert rec["body"] == "hello"
    assert rec["ts"].endswith("Z")


def test_route_send_pushes_AgentSendMessage(isolated_log, monkeypatch):
    from sagent.tools.core import agent_registry
    swe = _StubAgent()
    agent_registry["swe"] = swe
    try:
        ok, status = delivery.route_send(from_role="tl", to="swe", content="X")
        assert ok is True, status
        assert len(swe.runtime.inbox.pushed) == 1
        msg = swe.runtime.inbox.pushed[0]
        # The pushed object should be an AgentSendMessage carrying source+text.
        assert msg.source == "tl"
        assert msg.text == "X"
        # Audit log was written.
        line = isolated_log.read_text().strip()
        rec = json.loads(line)
        assert rec["from"] == "tl"
        assert rec["to"] == ["swe"]
    finally:
        agent_registry.pop("swe", None)


def test_route_send_unknown_recipient(isolated_log):
    ok, status = delivery.route_send(from_role="tl", to="nobody", content="X")
    assert ok is False
    assert "Unknown peer" in status
    # No audit record on failure.
    assert not isolated_log.exists() or isolated_log.read_text() == ""


def test_route_send_to_user_skips_inbox_push_but_writes_audit(isolated_log):
    """The ``user`` mailbox is a FakeAgent without a listener — pushing the
    AgentSendMessage is a no-op, but the audit log MUST still record the
    operator-visible message so the web UI renders it."""
    from sagent.tools.core import agent_registry
    user_stub = _StubAgent()
    agent_registry["user"] = user_stub
    try:
        ok, _ = delivery.route_send(from_role="tl", to="user", content="hello user")
        assert ok is True
        # ``user`` is special — no inbox push expected.
        assert user_stub.runtime.inbox.pushed == []
        rec = json.loads(isolated_log.read_text().strip())
        assert rec["from"] == "tl"
        assert rec["to"] == ["user"]
        assert rec["body"] == "hello user"
    finally:
        agent_registry.pop("user", None)


def test_route_send_suppress_audit(isolated_log):
    from sagent.tools.core import agent_registry
    swe = _StubAgent()
    agent_registry["swe"] = swe
    try:
        ok, _ = delivery.route_send(
            from_role="tl", to="swe", content="X", suppress_audit=True,
        )
        assert ok is True
        # Audit log NOT written; inbox push NOT skipped.
        assert not isolated_log.exists() or isolated_log.read_text() == ""
        assert len(swe.runtime.inbox.pushed) == 1
    finally:
        agent_registry.pop("swe", None)


def test_schedule_defer_validates_range(isolated_log):
    ok, status = delivery.schedule_defer(sender="tl", delay_s=0, body="x")
    assert ok is False
    assert "delay_s must be in" in status
    ok, status = delivery.schedule_defer(sender="tl", delay_s=999999, body="x")
    assert ok is False
    assert "delay_s must be in" in status


def test_schedule_defer_fires_after_delay(isolated_log):
    from sagent.tools.core import agent_registry
    tl = _StubAgent()
    agent_registry["tl"] = tl

    async def go():
        ok, status = delivery.schedule_defer(
            sender="tl", delay_s=1, body="poll CI",
        )
        assert ok, status
        # Audit record written immediately at schedule time.
        rec = json.loads(isolated_log.read_text().splitlines()[0])
        assert "scheduled" in rec["body"]
        # Nothing in the inbox yet.
        assert tl.runtime.inbox.pushed == []
        # Wait a bit longer than the delay.
        await asyncio.sleep(1.2)
        # Wake-up landed.
        assert len(tl.runtime.inbox.pushed) == 1
        msg = tl.runtime.inbox.pushed[0]
        assert msg.source == "tl"
        assert "[defer +1s]" in msg.text
        assert "poll CI" in msg.text

    try:
        asyncio.run(go())
    finally:
        agent_registry.pop("tl", None)
        delivery.cancel_all_deferred()
