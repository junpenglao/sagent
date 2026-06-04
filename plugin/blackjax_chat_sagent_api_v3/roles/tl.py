"""TL role factory — v3 (direct-API)."""

from __future__ import annotations

from pathlib import Path

from .common import MODEL_TL, build_agent


_ROLE_MD = Path(__file__).with_suffix("").with_name("tl.md")


def build():
    """Construct the TL Agent.

    TL is read-only by scope (no Edit/Write); coordinates by reading
    code, inspecting git history, and routing work to peers via
    sagent's native ``AgentSend`` (no MCP shim in v3). Bash is included
    for read-only verbs (git log/diff/show, ls, cat, grep,
    journalctl), but TL must not run mutating commands.

    Model: 2nd-cheapest tier for the active provider (see
    ``MODEL_TL`` in common.py).
    """
    from sagent import tools

    return build_agent(
        role_name="tl",
        role_md_path=_ROLE_MD,
        tools=[
            tools.Read(),
            tools.Grep(),
            tools.Glob(),
            tools.Bash(),
            tools.AgentSend(),
            tools.AgentSelf(),
        ],
        model_id=MODEL_TL,
    )
