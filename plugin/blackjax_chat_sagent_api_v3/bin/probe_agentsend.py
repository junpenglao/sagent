"""Empirical probe: does Gemini's API structurally invoke sagent's bridge-mounted AgentSend?

The whole reason v1/v2 needed the MCP shim was that Anthropic CLI
strips structured tool dispatch in opaque ways — peer messaging
ends up as prose ("I'll send a message to @swe") inside the
assistant text instead of as a ``tool_use`` block. v2's
``mcp_sagent/server.py`` works around this by mounting peer-send
as an external MCP tool that Anthropic CLI DOES dispatch
structurally.

v3's design hypothesis: Gemini's direct API doesn't have that
property — it should produce clean ``ToolCall`` entries on the
assistant turn for any tool we register. If the hypothesis holds,
v3 doesn't need an MCP shim. If it fails, we need to figure out
why and adapt.

This script tests the hypothesis with a tiny two-agent setup:

  1. Construct TL + SWE with the cheap-tier Gemini models.
  2. Register both in ``agent_registry`` so ``AgentSend(to='swe')``
     can resolve a target.
  3. Push a prompt to TL that EXPLICITLY requires peer messaging
     (no ambiguity about what TL is supposed to do).
  4. Wait for TL's first assistant turn to complete.
  5. Walk TL's tape and report: was there a structured ToolCall
     to ``AgentSend``? Did SWE receive a real ``AgentSendMessage``
     in its inbox? Or did TL just emit prose?

Cost: ~$0.001 (one short TL turn at gemini-2.5-flash + one tiny
SWE warmup turn at gemini-2.5-flash-lite).

Usage::

    cd plugin/blackjax_chat_sagent_api_v3
    uv run python bin/probe_agentsend.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))


async def _wait_for_agent_idle(agent: object, timeout_s: float = 60.0) -> bool:
    """Block until the agent publishes ``AgentIdle`` or timeout."""
    from sagent.types.runtime import AgentIdle

    idle_evt = asyncio.Event()

    def _watcher(ev):
        if isinstance(ev, AgentIdle):
            idle_evt.set()

    agent.runtime.observers.append(_watcher)
    try:
        await asyncio.wait_for(idle_evt.wait(), timeout_s)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        try:
            agent.runtime.observers.remove(_watcher)
        except ValueError:
            pass


async def _run_one_turn(agent: object, text: str) -> bool:
    """Push a UserMessage and wait for AgentIdle on this turn."""
    from sagent.types.runtime import UserMessage

    agent.runtime.inbox.push_back(UserMessage(text=text))
    return await _wait_for_agent_idle(agent)


def _audit_tape(agent: object, label: str) -> tuple[bool, list[str], str]:
    """Walk the agent's tape; return (structured_send_found, tool_call_names, last_text)."""
    from sagent.types.runtime import AssistantMessage

    tool_call_names: list[str] = []
    last_text = ""
    structured_send = False
    for entry in agent.runtime.context().messages:
        if not isinstance(entry, AssistantMessage):
            continue
        last_text = entry.text or last_text
        for tc in entry.tool_calls:
            tool_call_names.append(tc.name)
            if tc.name == "AgentSend":
                structured_send = True
                # Also report the args to confirm semantics
                args_to = tc.args.get("to") if isinstance(tc.args, dict) else None
                args_content = tc.args.get("content") if isinstance(tc.args, dict) else None
                print(f"  [{label}] structured AgentSend args: "
                      f"to={args_to!r} content={(str(args_content) or '')[:80]!r}")
    return structured_send, tool_call_names, last_text


def _audit_inbox(agent: object, label: str) -> int:
    """Count AgentSendMessage entries delivered to this agent's tape."""
    from sagent.types.runtime import AgentSendMessage

    count = 0
    for entry in agent.runtime.context().messages:
        if isinstance(entry, AgentSendMessage):
            count += 1
            print(f"  [{label}] received AgentSendMessage from "
                  f"{entry.source!r}: {entry.text[:80]!r}")
    return count


async def main() -> None:
    # Build TL + SWE, run a single TL turn that should invoke AgentSend,
    # and verify both the canonical label fix AND the audit-log emission.
    import os
    import tempfile

    from roles import swe, tl
    from runtime import audit_writer
    from sagent.tools.core import agent_registry

    # Route the probe's audit log to a tmpdir so it doesn't pollute
    # the operator's real $SAGENT_DATA_DIR/main.jsonl.
    tmpdir = Path(tempfile.mkdtemp(prefix="probe-audit-"))
    audit_log = tmpdir / "main.jsonl"
    os.environ["SAGENT_DATA_DIR"] = str(tmpdir)

    print("=== Probe: does Gemini structurally invoke AgentSend? ===\n")
    print("Building TL (gemini-2.5-flash) + SWE (gemini-2.5-flash-lite)...")
    tl_agent = tl.build()
    swe_agent = swe.build()
    # Wire the audit-log observer for TL so we can verify outbound
    # AgentSend calls land in main.jsonl.
    audit_writer.install_on(tl_agent, "tl", audit_log_path=audit_log)
    audit_writer.install_on(swe_agent, "swe", audit_log_path=audit_log)

    # Spin up agent loops in background. Agent.serve_forever() drains
    # the inbox + drives model_calls; runtime.run() takes a msg arg
    # (single turn) so it's not the right entrypoint for a daemon.
    #
    # We deliberately do NOT manually populate ``agent_registry`` here.
    # ``serve_forever()`` runs ``_install_contextvars`` which registers
    # each agent under its canonical name (since we set
    # ``agent._persistent = True`` in ``build_agent`` to disable
    # ``unique_registry_label``'s auto-suffix). Manual pre-registration
    # would race with that and trigger the very ``_1`` suffix this
    # change is meant to prevent.
    tl_task = asyncio.create_task(tl_agent.serve_forever(), name="rt-tl")
    swe_task = asyncio.create_task(swe_agent.serve_forever(), name="rt-swe")
    # Give serve_forever a moment to install contextvars + register.
    await asyncio.sleep(0.2)
    print(f"  agent_registry after spawn: {sorted(agent_registry)}")

    # Prompt with NO ambiguity — explicit instructions to call the tool.
    prompt = (
        "Send the message 'hello from TL, please ack' to peer @swe via "
        "the AgentSend tool. Do not write the message in your prose; "
        "do not describe what you'll do; just call the AgentSend tool "
        "now with {to: 'swe', content: 'hello from TL, please ack'} "
        "and end your turn. This is a structural test, the operator "
        "is watching for whether you use the tool or write prose."
    )

    print(f"\nPushing prompt to TL:\n  {prompt[:140]!r}...\n")
    print("Waiting for TL's first turn to complete (≤60 s)...\n")
    tl_ok = await _run_one_turn(tl_agent, prompt)
    if not tl_ok:
        print("[err] TL did not reach AgentIdle in 60 s — probe inconclusive.")
        tl_task.cancel()
        swe_task.cancel()
        sys.exit(1)

    # Give SWE a moment to drain anything TL sent it.
    await asyncio.sleep(2.0)

    print("\n=== TL tape audit ===")
    tl_structured, tl_tool_calls, tl_text = _audit_tape(tl_agent, "tl")
    print(f"\nTL's tool calls this turn: {tl_tool_calls!r}")
    print(f"TL's final assistant text (first 200 chars): {tl_text[:200]!r}")

    print("\n=== SWE inbox audit ===")
    swe_received = _audit_inbox(swe_agent, "swe")

    # ----- Audit-log verification -----
    print("\n=== Audit log audit (main.jsonl entries from this probe) ===")
    audit_records = []
    if audit_log.exists():
        with audit_log.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    audit_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    print(f"  {len(audit_records)} record(s) in {audit_log}")
    for r in audit_records:
        print(f"    {r.get('ts','')} {r.get('from','?'):8s} -> {r.get('to','?')!r:14s}: "
              f"{(r.get('body') or '')[:60]!r}")
    audit_ok = any(
        r.get("from") == "tl" and r.get("to") == ["swe"] for r in audit_records
    )

    print("\n=== Verdict ===")
    if tl_structured and swe_received > 0 and audit_ok:
        print("[PASS] TL produced a structured AgentSend tool_use block, "
              "SWE received the message in its inbox, AND the audit log "
              "captured the peer traffic.")
        print("       The MCP shim from v2 is structurally unnecessary "
              "for v3 — Gemini dispatches sagent's bridge-mounted "
              "AgentSend correctly, and a lightweight AuditWriter "
              "observer covers the main.jsonl emission v2's "
              "delivery.py used to handle. The architecture "
              "hypothesis holds.")
        exit_code = 0
    elif tl_structured and swe_received > 0 and not audit_ok:
        print("[PARTIAL] TL invoked AgentSend structurally + SWE received, "
              "but the AuditWriter didn't emit a main.jsonl record. "
              "Web UI would still show no peer traffic.")
        exit_code = 5
    elif tl_structured and swe_received == 0:
        print("[PARTIAL] TL invoked AgentSend structurally but SWE didn't "
              "receive — registry resolution issue, not a tool-dispatch issue.")
        exit_code = 2
    elif "AgentSend" not in tl_tool_calls and tl_text:
        print("[FAIL] TL emitted PROSE instead of a structured AgentSend "
              "call. This is the v1/v2 pathology, which would mean we need "
              "the MCP shim for v3 too. Inspect TL's text + system prompt "
              "to understand why.")
        exit_code = 3
    else:
        print(f"[INCONCLUSIVE] No prose, no AgentSend call. Tools used: "
              f"{tl_tool_calls!r}")
        exit_code = 4

    # Clean shutdown.
    tl_task.cancel()
    swe_task.cancel()
    await asyncio.gather(tl_task, swe_task, return_exceptions=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
