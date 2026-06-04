"""tech-writer role factory — v3 (direct-API)."""

from __future__ import annotations

from pathlib import Path

from .common import MODEL_DEFAULT, build_agent


_ROLE_MD = Path(__file__).with_suffix("").with_name("tech-writer.md")


def build():
    """Construct the tech-writer Agent.

    Owns sampling-book notebooks, docstring QA, migration guides,
    final documentation gate before PR merge.

    Model: cheapest tier for the active provider.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sagent import tools
    from sandboxed_tools import SandboxedBash

    return build_agent(
        role_name="tech-writer",
        role_md_path=_ROLE_MD,
        tools=[
            tools.Read(),
            tools.Edit(),
            tools.Write(),
            SandboxedBash(),
            tools.Grep(),
            tools.Glob(),
            tools.AgentSend(),
            tools.AgentSelf(),
        ],
        model_id=MODEL_DEFAULT,
    )
