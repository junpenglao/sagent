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


# Model assignments per provider. Structure:
#   provider -> {role_name: model_id, "_default": model_id}
# Roles not explicitly listed fall through to the "_default" entry.
#
# Iteration history (live tested 2026-06-04):
#
#   17:17 — TL=flash → empty natural-language coordination response.
#           Upgraded TL=gemini-2.5-pro. ✓
#   17:49 — junior-swe=flash-lite wrote @tl as PROSE. Upgraded
#           non-TL default to gemini-2.5-flash. ✓
#   19:10 — TL on 2.5-pro reversed its own correct diagnosis when
#           prompted to "double-check". SWE on flash persistently
#           edited the wrong file (tripwire tests) even after TL's
#           explicit instruction. Upgraded:
#             - TL → gemini-3.1-pro-preview (preview tier; better
#               at holding a hypothesis under doubt-pressure)
#             - SWE → gemini-2.5-pro (capability for the wrapper-
#               vs-test discrimination)
#             - statistician → gemini-2.5-pro (similar reasoning
#               demands; pre-emptive since she hasn't been engaged
#               yet at depth)
#             - junior-swe stays on flash (her work is simpler
#               edits + tests; flash is adequate)
#             - tech-writer stays on flash (doc QA; flash adequate)
#
# Pricing reference (2026-06-04 Google catalog):
#   gemini-2.5-flash-lite: $0.10 in / $0.40 out / $0.025 cache
#   gemini-2.5-flash:      $0.30 in / $2.50 out / $0.075 cache
#   gemini-2.5-pro:        $1.25 in / $10.0 out / $0.31 cache
#   gemini-3.1-pro-preview: $2.00 in / $12.0 out / $0.20 cache
_PROVIDER_MODELS: dict[str, dict[str, str]] = {
    "google": {
        "tl": "gemini-3.1-pro-preview",
        "swe": "gemini-3.5-flash",
        "statistician": "gemini-3.5-flash",
        "junior-swe": "gemini-3.1-flash-lite",
        "tech-writer": "gemini-3.1-flash-lite",
        "_default": "gemini-3.5-flash",
    },
    "anthropic": {

        # Anthropic catalog assignments — adjust per
        # providers/anthropic.py KNOWN_MODELS pricing.
        "_default": "claude-haiku-4-5",
    },
}

if _PROVIDER not in _PROVIDER_MODELS:
    raise RuntimeError(
        f"SAGENT_API_PROVIDER={_PROVIDER!r} is unknown. "
        f"Supported: {sorted(_PROVIDER_MODELS)}.",
    )

_ROLE_MODELS = _PROVIDER_MODELS[_PROVIDER]
MODEL_DEFAULT = _ROLE_MODELS["_default"]


def model_for_role(role_name: str) -> str:
    """Return the model_id assigned to ``role_name`` for the active provider.

    Falls back to ``MODEL_DEFAULT`` when no explicit override is
    configured (``junior-swe``, ``tech-writer``).
    """
    return _ROLE_MODELS.get(role_name, MODEL_DEFAULT)


# Back-compat alias retained for ``roles/tl.py`` (the only file that
# imported ``MODEL_TL`` directly). New role files should call
# ``model_for_role(role_name)`` instead.
MODEL_TL = model_for_role("tl")


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

**This applies to the user too.** To reply to the user, call \
`AgentSend(to="user", content=...)` — your assistant text is NOT \
shown in their chat UI. The user only sees explicit AgentSend \
calls.

**Operator Visibility:** The operator (@user) has FULL visibility \
into all live peer-to-peer traffic via the audit log. Do NOT \
forward or quote full peer messages when reporting to the user. \
Provide only terse high-level summaries of peer progress. If you \
finish a tool sequence and want to confirm completion to the \
user, call `AgentSend(to="user", ...)` once with the result; \
do NOT write a narrative summary of "here's what I just did" \
in your text content blocks — it will not be delivered. \
Silence is honest feedback that you didn't intend to message \
anyone this turn.

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


def _build_compactor():
    """Construct a SummaryCompactor with conservative thresholds.

    sagent's default ``utilization_trigger=0.95`` fires when the
    request fills 95% of the model's context window. For Gemini's
    1 M context this is ~950 K tokens — too close to the wall.
    v2 hit ``blocking_limit`` at ~1.5 M (claude's auto-compact
    couldn't reduce enough); we want headroom before v3 sees the
    same pattern.

    Conservative settings:

    * ``utilization_trigger=0.7`` — fire at 70% of window (~700 K
      tokens for Gemini). Leaves the agent ~30% of the window to
      breathe after compaction; the SummaryCompactor compresses
      ~10× so the post-compaction request size drops well below
      the trigger.
    * ``keep_recent=20`` — preserve the last 20 conversation
      entries uncompacted so the agent doesn't lose
      situational context across a compaction. ~20 turns is
      enough to hold a multi-step coordination chain in working
      memory.
    * ``proactive=False`` — only compact when the trigger fires
      (not on every turn). Saves a model call per turn vs
      ``proactive=True``.

    Returns a fresh instance per agent (the compactor's CompactionState
    is managed by the runtime, not the instance itself; instances
    are config holders, safe to share but cheap to recreate).
    """
    from sagent.compaction.summary import SummaryCompactor

    return SummaryCompactor(
        utilization_trigger=0.7,
        keep_recent=20,
        proactive=False,
    )


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


def _session_dir_for(role_name: str) -> Path | None:
    """Per-role session directory under ``$SAGENT_DATA_DIR/sessions/<role>/``.

    When ``SAGENT_DATA_DIR`` is set, returns a stable path so each
    server restart finds the same directory and ``load_session()``
    can replay the prior tape. When the env var is unset, returns
    None so persistence is disabled (matches v1/v2 stateless behaviour
    for casual local runs without a data dir).
    """
    base = os.environ.get("SAGENT_DATA_DIR")
    if not base:
        return None
    session_dir = Path(base).expanduser().resolve() / "sessions" / role_name
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _resume_if_persisted(agent: object, session_dir: Path | None) -> None:
    """If ``session.jsonl`` exists in ``session_dir``, replay its tape.

    Mirrors the v2 ``--resume <uuid>`` promise but at the sagent layer
    instead of the claude-CLI layer. ``load_session`` returns None
    when no persisted state exists (first boot), in which case the
    agent starts with empty history.
    """
    import logging

    log = logging.getLogger(__name__)
    if session_dir is None:
        return
    try:
        from sagent.agent.session_io import load_session
    except ImportError as exc:
        log.warning("session_io.load_session not available: %s", exc)
        return
    try:
        loaded = load_session(session_dir, {})
    except Exception as exc:  # noqa: BLE001 -- log + continue with empty tape
        log.warning(
            "load_session(%s) failed: %s — starting with empty tape",
            session_dir, exc,
        )
        return
    if loaded is None:
        # First boot for this role — no persisted session yet.
        return
    try:
        agent.resume(*loaded)
        log.info(
            "resumed agent %r from %s",
            getattr(agent, "name", "?"), session_dir,
        )
    except Exception:  # noqa: BLE001 -- log + continue with empty tape
        log.exception(
            "resume() failed for %r; continuing with empty tape",
            getattr(agent, "name", "?"),
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
    session_dir = _session_dir_for(role_name)
    agent = Agent(
        model=provider.model(model_id),
        model_spec=_model_spec_for(model_id),
        system=load_system_prompt(role_md_path),
        tools=list(tools),
        compactor=_build_compactor(),
        name=role_name,
        session_dir=session_dir,
        max_tool_call_rounds=max_tool_call_rounds,
        max_budget_usd=max_budget_usd,
        # Same runtime-level overrides as v2 — these are provider-
        # agnostic. The urgent flag gating (added 2026-06-04) is in
        # sagent core so it works here too.
        preempt_in_flight=True,
        coalesce_inbox=False,
    )
    _resume_if_persisted(agent, session_dir)
    # Mark the agent as persistent so ``_install_contextvars``
    # (called by ``serve_forever``) registers it under its canonical
    # name (``tl``, ``swe``, …) rather than the auto-disambiguated
    # ``tl_1`` / ``swe_1`` that ``unique_registry_label`` produces
    # for transient agents. Persistent means "long-lived agent with
    # a stable identity" — semantically correct for a chat-channel
    # role that runs for the lifetime of serve.py.
    #
    # Without this, the first probe (2026-06-04) showed peer messages
    # from TL arriving at SWE with ``source='tl_1'`` — bootstrap had
    # pre-registered ``tl`` in ``agent_registry``, so serve_forever
    # auto-suffixed when it registered itself. With it, peers see
    # ``source='tl'`` and ``AgentSend(to='tl', ...)`` resolves cleanly.
    agent._persistent = True  # noqa: SLF001 -- sagent flag, not a constructor arg
    return agent
