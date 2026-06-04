"""SWE role factory — v3 (direct-API)."""

from __future__ import annotations

from pathlib import Path

from .common import MODEL_DEFAULT, build_agent


_ROLE_MD = Path(__file__).with_suffix("").with_name("swe.md")


def build():
    """Construct the SWE Agent.

    SWE has full code-edit scope. Tool set covers reading, editing,
    running tests, and coordinating with peers via sagent's native
    ``AgentSend`` (no MCP shim in v3).

    Model: cheapest tier for the active provider (see
    ``MODEL_DEFAULT`` in common.py).
    """
    from sagent import tools

    return build_agent(
        role_name="swe",
        role_md_path=_ROLE_MD,
        tools=[
            tools.Read(),
            tools.Edit(),
            tools.Write(),
            tools.Bash(),
            tools.Grep(),
            tools.Glob(),
            tools.AgentSend(),
            tools.AgentSelf(),
        ],
        model_id=MODEL_DEFAULT,
    )
