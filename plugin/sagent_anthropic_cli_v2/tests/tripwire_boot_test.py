"""Tests for the v2.1-β serve.py startup tripwire gate.

The gate function is ``serve._run_materializer_tripwire`` — it reads
``SAGENT_CLI_OWN_SESSION``, calls the canary, and either keeps the
env var set (verdict clean → materialization enabled) or clears it
(verdict drift / canary unavailable / canary raised → v2 fallback).

These tests mock the canary so they don't spawn a real ``claude``
subprocess. The canary itself has its own coverage in
``sagent/providers/anthropic_cli_session/tripwire_test.py``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest


# The serve module lives in ``plugin/.../bin/serve.py`` and is
# normally invoked as a script. Tests import it as a module — add the
# bin/ dir to sys.path so the relative import works without the
# script having to be in PYTHONPATH already.
_SERVE_DIR = Path(__file__).resolve().parent.parent / "bin"
if str(_SERVE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVE_DIR))

import serve  # noqa: E402
from sagent.providers.anthropic_cli_session import (  # noqa: E402
    CanaryResult,
    DiffFinding,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with the env var unset so prior state can't leak."""
    monkeypatch.delenv(serve._MATERIALIZER_TRIPWIRE_ENV, raising=False)


@pytest.mark.asyncio
async def test_unset_env_skips_canary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No env var → skip the canary entirely, leave nothing changed.

    This is the default path for v2 deployments. The canary spawn is
    expensive (a real model round-trip) and must not fire on every
    boot just because the module is imported.
    """
    canary_called = False

    async def _fake_canary(**_kwargs: object) -> CanaryResult:
        nonlocal canary_called
        canary_called = True
        return CanaryResult(is_safe=True, findings=[], claude_jsonl_path=None)

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _fake_canary)
    with caplog.at_level(logging.INFO):
        await serve._run_materializer_tripwire()

    assert canary_called is False
    assert serve._MATERIALIZER_TRIPWIRE_ENV not in os.environ
    assert "not set" in caplog.text


@pytest.mark.asyncio
async def test_clean_verdict_keeps_env_var(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary returns ``is_safe=True`` → env var stays set, INFO log."""
    monkeypatch.setenv(serve._MATERIALIZER_TRIPWIRE_ENV, "1")

    async def _fake_canary(**_kwargs: object) -> CanaryResult:
        return CanaryResult(is_safe=True, findings=[], claude_jsonl_path=None)

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _fake_canary)
    with caplog.at_level(logging.INFO):
        await serve._run_materializer_tripwire()

    assert os.environ.get(serve._MATERIALIZER_TRIPWIRE_ENV) == "1"
    assert "PASS" in caplog.text


@pytest.mark.asyncio
async def test_drift_verdict_clears_env_var(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Canary returns drift findings → env cleared, WARNING per finding logged."""
    monkeypatch.setenv(serve._MATERIALIZER_TRIPWIRE_ENV, "yes")

    async def _fake_canary(**_kwargs: object) -> CanaryResult:
        return CanaryResult(
            is_safe=False,
            findings=[
                DiffFinding(
                    location="entry[3].type",
                    detail="unknown entry type 'shiny-new-thing'",
                ),
                DiffFinding(
                    location="entry[5].message",
                    detail="required field 'message' missing from claude entry",
                ),
            ],
            claude_jsonl_path=None,
        )

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _fake_canary)
    with caplog.at_level(logging.WARNING):
        await serve._run_materializer_tripwire()

    assert serve._MATERIALIZER_TRIPWIRE_ENV not in os.environ
    assert "FAIL" in caplog.text
    assert "shiny-new-thing" in caplog.text
    assert "message" in caplog.text


@pytest.mark.asyncio
async def test_canary_exception_falls_back_to_v2(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception from the canary clears the env (never crashes boot).

    The canary going wrong is operational news, but it must NOT block
    the server from starting. v2 (CLI-owned mode) is always a safe
    fallback.
    """
    monkeypatch.setenv(serve._MATERIALIZER_TRIPWIRE_ENV, "true")

    async def _exploding_canary(**_kwargs: object) -> CanaryResult:
        raise RuntimeError("simulated canary failure")

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _exploding_canary)
    with caplog.at_level(logging.WARNING):
        await serve._run_materializer_tripwire()

    assert serve._MATERIALIZER_TRIPWIRE_ENV not in os.environ
    assert "simulated canary failure" in caplog.text


@pytest.mark.asyncio
async def test_env_var_falsy_value_does_not_trigger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Setting the var to ``0`` (or any non-truthy) skips the canary.

    Mirrors the parsing contract in ``roles/common.py``; if they
    diverge, operators get confusing behaviour.
    """
    monkeypatch.setenv(serve._MATERIALIZER_TRIPWIRE_ENV, "0")
    canary_called = False

    async def _fake_canary(**_kwargs: object) -> CanaryResult:
        nonlocal canary_called
        canary_called = True
        return CanaryResult(is_safe=True, findings=[], claude_jsonl_path=None)

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _fake_canary)
    with caplog.at_level(logging.INFO):
        await serve._run_materializer_tripwire()

    assert canary_called is False
    assert "not set" in caplog.text


@pytest.mark.asyncio
async def test_env_var_truthy_variants_all_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``1`` / ``true`` / ``yes`` / ``TRUE`` all opt in.

    Operators paste from docs, mix case, copy/paste artifacts.
    Accepting common truthy values matches the documented
    ``SAGENT_CLI_OWN_SESSION`` contract.
    """
    canary_calls = 0

    async def _fake_canary(**_kwargs: object) -> CanaryResult:
        nonlocal canary_calls
        canary_calls += 1
        return CanaryResult(is_safe=True, findings=[], claude_jsonl_path=None)

    monkeypatch.setattr(serve, "arun_canary_against_live_cli", _fake_canary)
    for value in ("1", "true", "TRUE", "Yes"):
        monkeypatch.setenv(serve._MATERIALIZER_TRIPWIRE_ENV, value)
        await serve._run_materializer_tripwire()
        assert os.environ.get(serve._MATERIALIZER_TRIPWIRE_ENV) == value
    assert canary_calls == 4
