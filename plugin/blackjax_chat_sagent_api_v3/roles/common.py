"""Shared building blocks for per-role Agent factories — v3 (direct API).

Provider-agnostic by design: the v2 README's speculative "sagent+API"
column, now actually built. Default provider is Google (Gemini) so
this plugin doesn't fight Claude Code's Anthropic subscription for
the same OAuth key — but the provider lookup is dispatched on the
``SAGENT_API_PROVIDER`` env var, so swapping to Anthropic (or any
other sagent-supported direct-API backend) only touches one function
+ the model-id constants. See ``build_provider`` below.

Conventions (v3-specific, differs from v2):

- All roles use a direct-API provider, no CLI subprocess.
- Peer messaging uses sagent's NATIVE ``AgentSend`` tool, not the v2
  MCP shim. There's no ``claude --print`` subprocess to host an
  external MCP server; sagent's runtime owns the tool loop directly,
  so the bridge-mounted ``AgentSend`` works as designed (and was
  the source of v2's "MCP probe" detour in the first place: Anthropic
  CLI strips structured tool dispatch in opaque ways, but Google's
  API doesn't have that property).
- Model assignments are budget-first: cheapest model
  (``gemini-1.5-flash``, $0.075 / Mtok in) for the four non-TL agents;
  second-cheapest (``gemini-2.5-flash-lite``, $0.10 / Mtok in, newer
  arch than gemini-2.0-flash at same price) for TL. The point of v3
  is to measure the API-mode shape, not the model quality — we can
  always tune model assignments up.
- Heavy background commands still wrap in ``systemd-run --user
  --scope --quiet --collect -- bash -c '<cmd>'``; same reminder as
  v2, appended to every system prompt at build time.
- ``preempt_in_flight=True`` and ``coalesce_inbox=False`` carry over
  from v2: the runtime-level overrides aren't provider-specific.
  ``urgent``-gating works identically (see v2 README + 2026-06-04
  worklog lesson for the design).

What v3 does NOT have:

- No MCP server (``mcp_sagent/``): peer messaging is via sagent's
  bridge-mounted ``AgentSend`` directly; no per-agent stdio MCP
  subprocess.
- No ``--session-id`` / ``--resume`` machinery: the Google provider
  works on ``messages=`` arrays; sagent owns history; no on-disk
  session JSONL outside sagent's own tape format.
- No HotSpare bypass complexity: there's no subprocess to pool.
- No 5-minute ephemeral cache TTL concern: sagent's Google provider
  exposes ``supports_cache_control`` (see ``providers/google.py``);
  cache markers are controllable per-turn.

Open questions for v3 testing:

- Does Gemini's native tool dispatch produce clean ``ToolCall``
  blocks the runtime can route? (v2 needed the MCP detour
  precisely because Anthropic CLI didn't.)
- Does ``cancel_in_flight`` work on the Google provider's streaming
  HTTP request the way SIGINT worked on the CLI subprocess? (For
  ``urgent`` peer messages mid-turn.)
- What's the per-turn token cost compared to v2 on equivalent work
  (cheap models help; cache control helps; no subprocess spawn
  helps)?
- What's the failure mode catalog? (v1/v2 had specific shapes
  — what does v3 surface?)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence


# Provider selection. Default: Google (Gemini). Override by setting
# ``SAGENT_API_PROVIDER=anthropic`` to switch to Anthropic's direct
# API (note: requires ``ANTHROPIC_API_KEY`` and competes with Claude
# Code's subscription auth on the same machine; consider running
# Claude Code with ``--bare`` or a different env if you want to
# avoid surprise auth conflicts).
_PROVIDER = os.environ.get("SAGENT_API_PROVIDER", "google").lower()


# Model assignments per provider — cheap tier for non-TL agents,
# second-cheapest for TL. Edit these to tune the experiment.
_PROVIDER_MODELS: dict[str, tuple[str, str]] = {
    # provider -> (tl_model, default_model)
    "google": (
        "gemini-2.5-flash-lite",  # TL: $0.10 / Mtok in, newer arch
        "gemini-1.5-flash",       # default: $0.075 / Mtok in, cheapest
    ),
    "anthropic": (
        # 2nd-cheapest + cheapest in Anthropic's catalog. Adjust if
        # the pricing/availability changes. Sagent's KNOWN_MODELS in
        # ``providers/anthropic.py`` is the authoritative reference.
        "claude-haiku-4-5",        # TL: same tier as default for now
        "claude-haiku-4-5",        # default: cheapest available
    ),
}

if _PROVIDER not in _PROVIDER_MODELS:
    raise RuntimeError(
        f"SAGENT_API_PROVIDER={_PROVIDER!r} is unknown. "
        f"Supported: {sorted(_PROVIDER_MODELS)}.",
    )

MODEL_TL, MODEL_DEFAULT = _PROVIDER_MODELS[_PROVIDER]


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
agent or to the user, you MUST call the **`AgentSend`** tool with \
`{to, content}`. Known peers: `tl`, `swe`, `junior-swe`, \
`statistician`, `tech-writer`, `user`.

**Your assistant text is NOT delivered to anyone.** It is logged \
only to your own trace and is invisible to peers and the user. \
Writing a message in your text content blocks instead of calling \
the tool means the recipient never receives the message. There is \
no `@mention`-based prose parser. There is no DM-default fallback. \
The structured tool call IS the only routing.

**Common failure mode (do not do this):** writing text like \
"I'll send the summary to @user" or "Let me send a message to @swe" \
without actually calling `AgentSend`. Describing the call does \
not perform it. If you intend to message anyone, call the tool — \
do not write about it in prose.

**Common success pattern:** call `AgentSend` first (one or more \
times if you need to message multiple peers, one call each), then \
optionally end the turn with a brief text content block describing \
what you sent so your own trace stays readable.

### Urgent vs queued peer messages

`AgentSend` accepts an optional `urgent: bool` argument (default \
`False`). When True, the recipient's in-flight model_call is \
interrupted so they see your message immediately. **Use \
`urgent=True` only when the recipient MUST see the message \
before taking any further action** — STOP / pivot / scope-change \
directives, OR when you need to correct or supersede your own \
previous message before they act on a stale version. Routine \
status updates, acks, and FYIs should leave `urgent=False`: \
interrupting discards the recipient's in-flight work, so the bar \
is genuine "they would do the wrong thing if they don't see this \
first". Authority-wise, urgent peer directives are typical from \
TL (coordinator) and from any peer correcting their own prior \
message; routine cross-peer FYIs should stay non-urgent.

### Self-defer (CI waits, polling, "check back later")

The runtime supports self-defer through the inbox. To schedule a \
wake-up for YOURSELF, use the `AgentSendDeferred` tool (or, in \
runtimes that expose it, `AgentSelf(action="defer", ...)`). After \
scheduling, end your turn — the runtime pushes the body back into \
your inbox after the delay and you process it in a fresh turn. \
**Do NOT use `bash sleep N`** — that blocks your entire turn for N \
seconds and makes you uninterruptible. **Do NOT just write "I'll \
check back in N minutes" in text** — that does not schedule \
anything; the recipient (yourself) never receives a wake-up."""


def load_system_prompt(role_md_path: Path) -> str:
    """Read a role's system-prompt markdown and append standing reminders.

    Same two-block append as v2: PEER_MESSAGING + HEAVY_BG_REMINDER
    appended after the role-specific body so the identity + scope
    leads and the reminders sit at a stable tail location for visual
    confirmation. Reinjected every turn so compaction doesn't drop
    them.
    """
    body = role_md_path.read_text(encoding="utf-8").strip()
    return f"{body}\n\n---\n\n{PEER_MESSAGING}\n\n---\n\n{HEAVY_BG_REMINDER}"


# Per-provider API key resolution. Each entry: (env_var_name,
# config_file_path, "where to get the key" URL).
_API_KEY_SOURCES: dict[str, tuple[str, Path, str]] = {
    "google": (
        "GEMINI_API_KEY",
        Path.home() / ".config" / "gemini" / "api_key",
        "https://aistudio.google.com/app/apikey",
    ),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        Path.home() / ".config" / "anthropic" / "api_key",
        "https://console.anthropic.com/settings/keys",
    ),
}


def _load_api_key(provider: str = _PROVIDER) -> str:
    """Resolve the API key for the active provider from one of two sources.

    Search order (per provider):

    1. The provider's env var (``GEMINI_API_KEY`` / ``ANTHROPIC_API_KEY``)
       — preferred for ephemeral / CI / shell-export use.
    2. The provider's config file (``~/.config/<vendor>/api_key``,
       one line, just the key) — preferred for persistent local use
       so the key isn't in shell history or process listings.

    Either source is fine; the env var wins if both are set. Trims
    whitespace from the file's content because operator copy-paste
    routinely brings a trailing newline.

    Raises:
        RuntimeError: with explicit setup instructions if neither
            source yields a non-empty key.
    """
    env_var, config_path, signup_url = _API_KEY_SOURCES[provider]
    env_key = os.environ.get(env_var, "").strip()
    if env_key:
        return env_key
    if config_path.exists():
        file_key = config_path.read_text(encoding="utf-8").strip()
        if file_key:
            return file_key
    raise RuntimeError(
        f"{env_var} not found. Get a key at {signup_url}, then EITHER:\n"
        f"  (a) `export {env_var}=<key>` before running serve.py, or\n"
        f"  (b) write the key to `{config_path}` (single line).\n"
        f"Verify with: `python bin/check_api_key.py` from this plugin dir.",
    )


def build_provider():
    """Construct the shared API provider.

    Selection is driven by ``SAGENT_API_PROVIDER`` (default ``google``).
    One provider instance is shared across all five roles (it owns
    only the API key + cost catalog; per-agent state lives on the
    Model returned by ``provider.model(...)``).
    """
    api_key = _load_api_key()
    if _PROVIDER == "google":
        from sagent.providers import Google

        return Google.from_key(api_key)
    if _PROVIDER == "anthropic":
        from sagent.providers import Anthropic

        return Anthropic.from_key(api_key)
    raise RuntimeError(f"Unsupported SAGENT_API_PROVIDER: {_PROVIDER!r}")


def _model_spec_for(model_id: str):
    """Build a ``ModelSpec`` that lets ``AgentSelf`` swap the model later.

    Without a spec, ``AgentSelf(model_id=...)`` rejects with
    "Agent has no model spec; cannot swap" — the runtime needs to
    know how to reconstruct the provider for the new model.
    """
    from sagent.types.model import ModelSpec

    return ModelSpec(
        provider="Google" if _PROVIDER == "google" else "Anthropic",
        auth="api_key",
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
    """Construct a sagent Agent for a role with v3 (Google API) defaults baked in.

    Args:
        role_name: Label used in ``agent_registry`` and as the ``name``
            field on the Agent. Must match the role's label used in
            ``AgentSend(to=...)`` calls from peers.
        role_md_path: Path to the role's system-prompt markdown.
        tools: Sequence of sagent Tool instances allowed for this role.
            Should typically include ``AgentSend()`` so the role can
            message peers; ``AgentSelf()`` if the role manages its own
            status / model swap.
        model_id: Gemini model id (see ``Google.KNOWN_MODELS``).
        max_tool_call_rounds: Per-turn cap; ``None`` for sagent default.
        max_budget_usd: Per-agent USD cap; ``None`` disables.

    Returns:
        A configured Agent ready to be registered + driven.
    """
    from sagent.agent import Agent

    provider = build_provider()
    return Agent(
        model=provider.model(model_id),
        model_spec=_model_spec_for(model_id),
        system=load_system_prompt(role_md_path),
        tools=list(tools),
        name=role_name,
        max_tool_call_rounds=max_tool_call_rounds,
        max_budget_usd=max_budget_usd,
        # Same runtime-level overrides as v2 — these are provider-
        # agnostic. The urgent flag gating (added 2026-06-04) is in
        # sagent core so it works here too.
        preempt_in_flight=True,
        coalesce_inbox=False,
    )
