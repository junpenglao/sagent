"""Shared building blocks for per-role Agent factories.

Conventions:

- All roles use ``AnthropicCLI.from_credentials()`` (Claude subscription,
  no API key consulted).
- All roles get ``preempt_in_flight=True``: TL corrections, peer
  pings, and user redirects fire as SIGINT to the in-flight CLI turn
  (requires the ``feat/cli-preempt-via-sigint`` patch on the sagent
  fork; see ``sagent/README.md``).
- Peer messaging happens via the plugin's MCP server (``mcp_sagent.server``)
  rather than sagent's bridge-mounted ``AgentSend``. The MCP server's
  ``sagent_send`` tool appears in the CLI's catalog as
  ``mcp__sagent_chat__sagent_send`` and works structurally on all three
  models (haiku/sonnet/opus) — see README § "Episode 3" for the probe
  result that motivated the design.
- Heavy background commands must be wrapped in ``systemd-run --user
  --scope --quiet --collect -- bash -c '<cmd>'`` to keep an OOM
  contained in a sibling cgroup instead of cascading through the
  pane. The wrap reminder is appended to every system prompt at
  build time so it survives context compaction (a structural fix
  for the chat/-era SWE-OOM-after-compaction failure mode).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


# Per-role model assignments mirror ``claude-config/project/.claude/agents/<role>.md``
# (the existing Claude Code subagent configs).
MODEL_OPUS = "claude-opus-4-8"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"

HEAVY_BG_REMINDER = """\
Operating reminder: if a job is expected to be heavy (memory or CPU), \
run it with `systemd-run --user --scope --quiet --collect \
--unit=<literal> -- bash -c '<cmd>'` so an OOM stays in a sibling \
cgroup and your worker survives. Do not put $(...) or $VAR inside any \
argument starting with `-`; the Bash tool's permission system rejects \
runtime-determined content there."""

PEER_MESSAGING = """\
## Peer messaging — REQUIRED structural tool calls

This is a multi-agent chat channel. To deliver a message to another \
agent or to the user, you MUST call **`mcp__sagent_chat__sagent_send`** \
with `{to, content}`. Known peers: `tl`, `swe`, `junior-swe`, \
`statistician`, `tech-writer`, `user`.

**Your assistant text is NOT delivered to anyone.** It is logged \
only to your own trace and is invisible to peers and the user. \
Writing a message in your text content blocks instead of calling \
the tool means the recipient never receives the message. There is \
no `@mention`-based prose parser. There is no DM-default fallback. \
The structured tool call IS the only routing.

**Common failure mode (do not do this):** writing text like \
"I'll send the summary to @user" or "Let me send a message to @swe" \
without actually calling `sagent_send`. Describing the call does \
not perform it. If you intend to message anyone, call the tool — \
do not write about it in prose.

**Common success pattern:** call `sagent_send` first (one or more \
times if you need to message multiple peers, one call each), then \
optionally end the turn with a brief text content block describing \
what you sent so your own trace stays readable.

### Self-defer (CI waits, polling, "check back later")

To schedule a wake-up for YOURSELF, call \
**`mcp__sagent_chat__sagent_defer`** with `{delay_s, body}`. After \
scheduling, end your turn — the runtime pushes the body back into \
your inbox after the delay and you process it in a fresh turn. \
**Do NOT use `bash sleep N`** — that blocks your entire turn for N \
seconds and makes you uninterruptible. **Do NOT just write "I'll \
check back in N minutes" in text** — that does not schedule \
anything; the recipient (yourself) never receives a wake-up."""


def load_system_prompt(role_md_path: Path) -> str:
    """Read a role's system-prompt markdown and append the standing reminders.

    Two reminders, in order, appended (not prefixed) so the role-specific
    identity + scope text leads the prompt and the reminders sit at a
    stable tail location for visual confirmation:

    1. ``PEER_MESSAGING`` — how to address peers (via MCP tool, not
       prose). Reinjected every turn so compaction doesn't drop it.
    2. ``HEAVY_BG_REMINDER`` — systemd-run wrap rule for OOM containment;
       reinjected every turn for the same reason.
    """
    body = role_md_path.read_text(encoding="utf-8").strip()
    return f"{body}\n\n---\n\n{PEER_MESSAGING}\n\n---\n\n{HEAVY_BG_REMINDER}"


def build_provider():
    """Construct the shared AnthropicCLI provider.

    One provider instance is shared across all five roles (it owns
    only credentials + the bridge URL; per-agent state lives on the
    Model returned by ``provider.model(...)``).
    """
    from sagent.providers import AnthropicCLI

    return AnthropicCLI.from_credentials()


def _sagent_mcp_server_entry(role: str) -> dict:
    """Per-role stdio MCP entry for the CLI's ``--mcp-config``.

    Spawns ``mcp_sagent/server.py`` with two env vars:

      - ``SAGENT_ROLE``: the calling agent's label, used by the MCP
        server to attribute outgoing peer messages.
      - ``SAGENT_HTTP_URL``: where to POST ``/api/post`` and
        ``/api/defer`` — i.e. ``serve.py``'s loopback URL. The MCP
        server runs in a SEPARATE Python process from ``serve.py``,
        so its in-process ``agent_registry`` is empty; HTTP is the
        only way to reach the live registry.
    """
    import os
    import sys

    from mcp_sagent.config_factory import SERVER_SCRIPT

    port = os.environ.get("SAGENT_HTTP_PORT", "8767")
    return {
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
        "env": {
            "SAGENT_ROLE": role,
            "SAGENT_HTTP_URL": f"http://127.0.0.1:{port}",
        },
    }


def _model_spec_for(model_id: str):
    """Build a ``ModelSpec`` that lets ``AgentSelf`` swap the model later.

    Without a spec, ``AgentSelf(model_id=...)`` rejects with
    "Agent has no model spec; cannot swap" — the runtime needs to
    know how to reconstruct the provider for the new model.
    """
    from sagent.types.model import ModelSpec

    return ModelSpec(
        provider="AnthropicCLI",
        auth="credentials",
        model_id=model_id,
    )


def build_agent(
    *,
    role_name: str,
    role_md_path: Path,
    tools: Sequence[object],
    model_id: str,
    max_tool_call_rounds: int | None = None,
    max_budget_usd: float | None = None,
):
    """Construct a sagent Agent for a role with shared defaults baked in.

    The plugin's MCP server (``mcp_sagent/server.py``) is auto-wired
    into the agent's ``claude --print`` subprocess via
    ``provider.model(..., extra_mcp_servers={"sagent": {...}})``. The
    role label is baked into the MCP server's env, so the server
    knows which agent it serves on every CallToolRequest.

    Args:
        role_name: Label used in ``agent_registry`` and as the ``name``
            field on the Agent. Must match the role's label used in
            ``sagent_send(to=...)`` calls from peers.
        role_md_path: Path to the role's system-prompt markdown.
        tools: Sequence of sagent Tool instances allowed for this role.
            Does NOT include AgentSend or AgentSelf — those live in
            the MCP server (``mcp__sagent_chat__sagent_send``,
            ``mcp__sagent_chat__sagent_self``).
        model_id: Claude model id.
        max_tool_call_rounds: Per-turn cap; ``None`` for sagent default.
        max_budget_usd: Per-agent USD cap; ``None`` disables.

    Returns:
        A configured Agent ready to be registered + driven.
    """
    from sagent.agent import Agent

    provider = build_provider()
    # NOTE: must not collide with sagent's bridge server name (``"sagent"``,
    # hardcoded at sagent/providers/lib/mcp_bridge.py:175). The bridge
    # exposes Read/Bash/Glob/Grep/etc. as ``mcp__sagent__<tool>``; the
    # plugin's MCP server exposes peer-messaging as
    # ``mcp__sagent_chat__sagent_send`` / ``__sagent_defer`` /
    # ``__sagent_self``.
    return Agent(
        model=provider.model(
            model_id,
            extra_mcp_servers={"sagent_chat": _sagent_mcp_server_entry(role_name)},
        ),
        model_spec=_model_spec_for(model_id),
        system=load_system_prompt(role_md_path),
        tools=list(tools),
        name=role_name,
        max_tool_call_rounds=max_tool_call_rounds,
        max_budget_usd=max_budget_usd,
        preempt_in_flight=True,
    )
