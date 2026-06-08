"""Tests for the structural-diff tripwire.

The diff is the load-bearing primitive that the v2.1-β startup canary
will act on. Wrong-positive risk: a real format change goes undetected
and materialization corrupts the session. Wrong-negative risk: the
tripwire flags a benign difference and we never enable materialization.
These tests pin both sides.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pickle

import pytest

from sagent.providers.anthropic_cli_session import (
    materialize_session,
    parse_jsonl_to_messages,
)
from sagent.providers.anthropic_cli_session.tripwire import (
    DiffFinding,
    is_safe_to_enable,
    run_canary_against_live_cli,
    structural_diff,
)
from sagent.types.model import ModelRequest
from sagent.types.runtime import (
    AssistantMessage,
    ModelContextEvent,
    ToolCall,
    ToolResult,
    UserMessage,
)


def _msgs(*messages: object) -> list[ModelContextEvent]:
    """Cast helper to silence the variance complaint on heterogeneous lists."""
    return cast(list[ModelContextEvent], list(messages))


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_identical_messages_no_findings() -> None:
    """Two identical message lists yield zero findings + ``is_safe=True``."""
    a = _msgs(UserMessage(text="ping"), AssistantMessage(text="pong"))
    findings = structural_diff(a, a)
    assert findings == []
    assert is_safe_to_enable(findings) is True


def test_length_mismatch_reported() -> None:
    """A length difference surfaces as the first finding."""
    a = _msgs(UserMessage(text="a"), UserMessage(text="b"))
    b = _msgs(UserMessage(text="a"))
    findings = structural_diff(a, b)
    assert any("length mismatch" in f.detail for f in findings)
    assert is_safe_to_enable(findings) is False


def test_text_difference_in_user_message() -> None:
    """A text change in a UserMessage surfaces with location `[i].text`."""
    a = _msgs(UserMessage(text="hello"))
    b = _msgs(UserMessage(text="world"))
    findings = structural_diff(a, b)
    assert len(findings) == 1
    assert findings[0].location == "[0].text"


def test_type_mismatch_reported() -> None:
    """A different message type at the same position surfaces as a finding."""
    a = _msgs(UserMessage(text="hello"))
    b = _msgs(AssistantMessage(text="hello"))
    findings = structural_diff(a, b)
    assert len(findings) == 1
    assert "type mismatch" in findings[0].detail


def test_tool_call_args_difference_reported() -> None:
    """A diff in ``ToolCall.args`` surfaces with the nested location."""
    a = _msgs(
        AssistantMessage(
            tool_calls=(ToolCall(id="t1", name="Bash", args={"cmd": "ls"}),),
        ),
    )
    b = _msgs(
        AssistantMessage(
            tool_calls=(ToolCall(id="t1", name="Bash", args={"cmd": "pwd"}),),
        ),
    )
    findings = structural_diff(a, b)
    assert any(f.location == "[0].tool_calls[0].args" for f in findings)


def test_tool_result_content_difference_reported() -> None:
    """``ToolResult.content`` differences surface."""
    a = _msgs(ToolResult(call_id="t1", content="42 passed"))
    b = _msgs(ToolResult(call_id="t1", content="42 failed"))
    findings = structural_diff(a, b)
    assert any(f.location == "[0].content" for f in findings)


def test_thinking_blocks_not_compared() -> None:
    """Differences in ``thinking_blocks`` are intentionally ignored.

    The thinking ``signature`` is opaque/volatile and claude's
    line-splitting groups them differently from the materializer.
    Comparing them would generate noise without catching real drift.
    """
    a = _msgs(
        AssistantMessage(
            text="ok",
            thinking_blocks=(
                {"type": "thinking", "thinking": "A", "signature": "sigA"},
            ),
        )
    )
    b = _msgs(
        AssistantMessage(
            text="ok",
            thinking_blocks=(
                {"type": "thinking", "thinking": "B", "signature": "sigB"},
            ),
        )
    )
    assert structural_diff(a, b) == []


def test_materializer_round_trip_yields_no_findings(tmp_home: Path) -> None:
    """The canonical use case: tape → materialize → re-parse → diff vs tape.

    If this ever fails, the materializer is losing information that
    the tripwire considers meaningful.
    """
    original = _msgs(
        UserMessage(text="run the suite"),
        AssistantMessage(
            text="I will run pytest.",
            tool_calls=(
                ToolCall(id="toolu_001", name="Bash", args={"command": "pytest -q"}),
            ),
        ),
        ToolResult(call_id="toolu_001", content="42 passed"),
    )
    path, _ = materialize_session(
        ModelRequest(messages=original),
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        cwd=tmp_home,
    )
    reparsed = parse_jsonl_to_messages(path)
    findings = structural_diff(cast(list[object], list(original)), reparsed)
    assert findings == [], f"materializer round-trip drifted: {findings}"


def test_path_inputs_supported(tmp_home: Path) -> None:
    """``Path`` arguments are parsed transparently.

    The boot path uses this signature directly: pass a claude-written
    JSONL path and the materializer-written one, ask for the verdict.
    """
    original = _msgs(UserMessage(text="ping"))
    path, _ = materialize_session(
        ModelRequest(messages=original),
        session_id="11111111-2222-3333-4444-555555555555",
        cwd=tmp_home,
    )
    # Compare the file against the in-memory original.
    findings = structural_diff(path, cast(list[object], list(original)))
    assert findings == []


def test_canary_runner_is_a_stub() -> None:
    """v2.1-β placeholder: the live-spawn path raises ``NotImplementedError``.

    The boot integration in v2.1-β should treat this as "tripwire
    unavailable, fall back to materialization-off".
    """
    with pytest.raises(NotImplementedError):
        run_canary_against_live_cli(
            session_id="11111111-2222-3333-4444-555555555555",
            cwd=Path("/tmp"),  # noqa: S108 -- canary, never written
        )


def test_diff_finding_is_picklable() -> None:
    """``DiffFinding`` instances cross process boundaries cleanly.

    The boot path may report findings from a child process; ensure
    they serialize without surprises.
    """
    f = DiffFinding(location="[0].text", detail="x vs y")
    assert pickle.loads(pickle.dumps(f)) == f
