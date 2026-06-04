"""statistician role factory — v3 (direct-API)."""

from __future__ import annotations

from pathlib import Path

from .common import MODEL_DEFAULT, build_agent


_ROLE_MD = Path(__file__).with_suffix("").with_name("statistician.md")


def build():
    """Construct the statistician Agent.

    Owns algorithm correctness review, math-to-code verification,
    MCMC parameter tuning + benchmarking. Has full edit scope in
    ``tuningfork/experiments/``.

    Model: cheapest tier for the active provider.
    """
    from sagent import tools

    return build_agent(
        role_name="statistician",
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
