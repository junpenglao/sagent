"""blackjax-chat v3 (direct-API) server.

Slim adaptation of v2's serve.py with the MCP plumbing removed. Single
Python process owns:

  * The five role agents (via ``roles.{tl,swe,junior_swe,statistician,
    tech_writer}.build()``), each backed by the active direct-API
    provider (Google by default; see ``roles/common.py``).
  * The HTTP API (``/api/agents``, ``/api/post``, ``/api/messages``,
    ``/api/trace``, ``/api/restart``, ``/api/members``) and the web UI.
  * The audit log (``$SAGENT_DATA_DIR/main.jsonl``) and per-agent
    trace files (``$SAGENT_DATA_DIR/sessions/<role>.trace.jsonl``).

Peer messaging routes via sagent's bridge-mounted ``AgentSend`` tool —
NO MCP server, NO per-agent stdio subprocess, NO HTTP loopback bridge
for cross-process routing. The runtime owns the tool loop in-process
because the API provider doesn't have v2's CLI strip pathology.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

# Make sibling packages (``roles``, ``runtime``) importable when this
# script is run via its full path (the production form).
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("blackjax_chat_v3.serve")


# ---------------------------------------------------------------------
# Data dir + audit log
# ---------------------------------------------------------------------


def _resolve_data_dir() -> Path:
    """``$SAGENT_DATA_DIR`` if set; otherwise the plugin source dir.

    Matches v2's convention so audit logs / traces co-locate when the
    operator points the v2 and v3 plugins at the same dir for
    cross-build continuity.
    """
    env = os.environ.get("SAGENT_DATA_DIR")
    if env:
        path = Path(env).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        (path / "sessions").mkdir(exist_ok=True)
        return path
    return _PLUGIN_ROOT


_DATA_DIR = _resolve_data_dir()
_AUDIT_LOG = _DATA_DIR / "main.jsonl"


def _iso8601_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        datetime.now(timezone.utc).strftime("%f")[:3] + "Z"
    )


def _audit_append(record: dict) -> None:
    """Append one record to ``main.jsonl`` (one line of JSON)."""
    _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------
# Build the five role agents
# ---------------------------------------------------------------------


def build_agents() -> dict[str, object]:
    """Instantiate the five role agents.

    Returns a ``{label: Agent}`` mapping. Does NOT pre-populate
    ``agent_registry`` — that happens automatically when
    ``serve_forever()`` runs ``_install_contextvars`` on each agent,
    which uses the canonical name since ``build_agent`` set
    ``agent._persistent = True`` (see common.py rationale).
    """
    from roles import junior_swe, statistician, swe, tech_writer, tl
    from runtime import audit_writer, trace_writer

    builders = {
        "tl": tl.build,
        "swe": swe.build,
        "junior-swe": junior_swe.build,
        "statistician": statistician.build,
        "tech-writer": tech_writer.build,
    }
    agents: dict[str, object] = {}
    for label, builder in builders.items():
        agent = builder()
        agents[label] = agent
        trace_writer.install_on(agent, label)
        # Audit-log writer: emits one main.jsonl record per outbound
        # AgentSend so the web UI sees peer traffic. The /api/post
        # handler emits its own records for operator-side ingress;
        # this observer covers the peer-to-peer side that bypasses
        # /api/post entirely in v3 (sagent's AgentSend pushes to the
        # recipient inbox directly).
        audit_writer.install_on(agent, label, audit_log_path=_AUDIT_LOG)
    return agents


# ---------------------------------------------------------------------
# Warmup probe
# ---------------------------------------------------------------------


_WARMUP_PROMPT = (
    "Boot probe. Reply with a single word — 'ok' — and end your turn. "
    "Do NOT call any tools. Do NOT message any peer. This is a "
    "one-shot readiness check; future turns will involve real work."
)


async def warmup(agents: dict[str, object], timeout_s: float = 60.0) -> dict[str, bool]:
    """Send each agent a single boot probe; wait for AgentIdle.

    Returns a ``{label: ready_bool}`` map. Unlike v2's warmup, there's
    no MCP-suppression sentinel because there's no MCP server — the
    warmup turn naturally stays silent to peers because the probe
    text explicitly forbids tool calls + peer messaging.
    """
    from sagent.types.runtime import AgentIdle, UserMessage

    idle_events: dict[str, asyncio.Event] = {}
    observers: list = []
    for label, agent in agents.items():
        evt = asyncio.Event()
        idle_events[label] = evt

        def _watcher(ev, _evt=evt):
            if isinstance(ev, AgentIdle):
                _evt.set()

        agent.runtime.observers.append(_watcher)
        observers.append((agent, _watcher))

    for label, agent in agents.items():
        agent.runtime.inbox.push_back(UserMessage(text=_WARMUP_PROMPT))

    async def _wait_one(label: str) -> tuple[str, bool]:
        try:
            await asyncio.wait_for(idle_events[label].wait(), timeout_s)
            return label, True
        except asyncio.TimeoutError:
            return label, False

    try:
        results = await asyncio.gather(*[_wait_one(l) for l in agents])
        return dict(results)
    finally:
        for agent, watcher in observers:
            try:
                agent.runtime.observers.remove(watcher)
            except ValueError:
                pass


# ---------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------


def make_app(agents: dict[str, object]):
    """Construct the Starlette app with all HTTP routes wired up."""
    from sagent.tools.core import agent_registry
    from sagent.types.runtime import AgentSendMessage, UserMessage
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Route

    async def root(request: Request) -> Response:
        del request
        return FileResponse(_PLUGIN_ROOT / "web" / "index.html")

    async def debug_page(request: Request) -> Response:
        del request
        return FileResponse(_PLUGIN_ROOT / "web" / "debug.html")

    async def get_agents(request: Request) -> Response:
        del request
        out = []
        for label, agent in agents.items():
            rt = getattr(agent, "runtime", None)
            in_turn = bool(getattr(rt, "model_call", None))
            pending = len(getattr(rt, "_mid_stream_queue", []) or [])
            inbox = getattr(rt, "inbox", None)
            inbox_size = 0
            if inbox is not None:
                # ``inbox._queue`` is an ``asyncio.Queue`` — use ``qsize()``;
                # ``len()`` doesn't work on it.
                q = getattr(inbox, "_queue", None)
                if hasattr(q, "qsize"):
                    inbox_size = q.qsize()
            out.append({
                "role": label,
                "status": "working" if in_turn else "idle",
                "in_turn": in_turn,
                "pending": pending,
                "inbox_size": inbox_size,
                "model_id": getattr(agent, "model_id", None),
            })
        return JSONResponse({
            "now": _iso8601_z(),
            "agents": out,
        })

    async def post(request: Request) -> Response:
        """Operator + peer ingress. Body: ``{to, body, from?, urgent?}``."""
        payload = await request.json()
        to = str(payload.get("to", "")).strip()
        body = str(payload.get("body", ""))
        from_role = str(payload.get("from", "user")).strip() or "user"
        urgent = bool(payload.get("urgent", False))
        if not to or not body:
            return JSONResponse(
                {"error": "both 'to' and 'body' are required"},
                status_code=400,
            )
        target = agent_registry.get(to)
        if target is None and to != "user":
            return JSONResponse(
                {
                    "error": f"unknown target {to!r}; active: {sorted(agents)}",
                },
                status_code=404,
            )

        _audit_append({
            "ts": _iso8601_z(),
            "from": from_role,
            "to": [to],
            "body": body,
            **({"urgent": True} if urgent else {}),
        })

        if to == "user":
            # User-as-recipient: audit only; no inbox push (operator
            # reads via web UI).
            pass
        elif from_role == "user":
            target.runtime.inbox.push_back(UserMessage(text=body, urgent=urgent))
        else:
            target.runtime.inbox.push_back(
                AgentSendMessage(source=from_role, text=body, urgent=urgent),
            )
        return JSONResponse({"ok": True, "to": to, "from": from_role})

    async def get_messages(request: Request) -> Response:
        """Tail the audit log. Optional ``?since=<iso8601>`` for delta polling.

        Returns BOTH ``records`` and ``messages`` keys so the web UI
        works either way. v2's index.html polls ``data.records`` only
        (no ``messages`` fallback), so the v3 chat looked frozen until
        the operator hit refresh — same bug shape as /api/roles vs
        /api/members.
        """
        since = request.query_params.get("since", "")
        out = []
        if _AUDIT_LOG.exists():
            with _AUDIT_LOG.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since and r.get("ts", "") <= since:
                        continue
                    out.append(r)
        return JSONResponse({
            "records": out,
            "messages": out,
            "now": _iso8601_z(),
        })

    async def get_members(request: Request) -> Response:
        """Roles list for the UI's members panel.

        v3 exposes BOTH ``/api/members`` and ``/api/roles`` because
        the web UI (copied verbatim from v2) calls ``/api/roles``
        and unpacks ``data.roles``. Returning both keys in a single
        response so the same handler serves both paths.
        """
        del request
        roles = sorted(agents) + ["user"]
        return JSONResponse({"roles": roles, "members": roles})

    async def get_trace(request: Request) -> Response:
        role = request.path_params["role"]
        if role not in agents:
            return JSONResponse(
                {"error": f"unknown role {role!r}", "known": sorted(agents)},
                status_code=404,
            )
        path = _DATA_DIR / "sessions" / f"{role}.trace.jsonl"
        if not path.exists():
            return JSONResponse({"events": [], "total": 0, "returned": 0})

        events: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        total_on_disk = len(events)
        try:
            limit = max(1, min(20000, int(request.query_params.get("limit", "2000"))))
        except ValueError:
            limit = 2000
        sliced = events[-limit:] if total_on_disk > limit else events
        return JSONResponse({
            "events": sliced,
            "total": total_on_disk,
            "returned": len(sliced),
        })

    async def restart(request: Request) -> Response:
        """Clear an agent's history. Body: ``{role}``."""
        payload = await request.json()
        role = str(payload.get("role", "")).strip()
        agent = agents.get(role)
        if agent is None:
            return JSONResponse(
                {"error": f"unknown role {role!r}"}, status_code=404,
            )
        try:
            await agent.clear()
            return JSONResponse(
                {"ok": True, "role": role, "output": "history cleared"},
            )
        except Exception as exc:  # noqa: BLE001 -- surface to operator
            return JSONResponse(
                {"ok": False, "role": role, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )

    routes = [
        Route("/", root),
        Route("/debug", debug_page),
        Route("/api/agents", get_agents),
        Route("/api/post", post, methods=["POST"]),
        Route("/api/messages", get_messages),
        Route("/api/members", get_members),
        Route("/api/roles", get_members),
        Route("/api/trace/{role}", get_trace),
        Route("/api/restart", restart, methods=["POST"]),
    ]
    return Starlette(routes=routes)


# ---------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------


async def _run_agents(agents: dict[str, object]) -> list[asyncio.Task]:
    """Spawn each agent's loop as a background asyncio task.

    ``Agent.serve_forever()`` is the daemon entrypoint that drains
    the inbox + drives model_calls; ``runtime.run(msg)`` is the
    single-turn primitive and would crash here without an arg.
    """
    tasks: list[asyncio.Task] = []
    for label, agent in agents.items():
        task = asyncio.create_task(agent.serve_forever(), name=f"runtime-{label}")
        tasks.append(task)
    return tasks


async def _main(port: int) -> None:
    import uvicorn

    logger.info("data dir: %s", _DATA_DIR)
    logger.info("building agents...")
    agents = build_agents()
    logger.info("starting agent runtime loops...")
    runtime_tasks = await _run_agents(agents)

    logger.info("running warmup (timeout 60s)...")
    ready = await warmup(agents)
    timed_out = [k for k, v in ready.items() if not v]
    ready_ok = [k for k, v in ready.items() if v]
    logger.info(
        "warmup done: ready=%s timed_out=%s",
        sorted(ready_ok),
        sorted(timed_out),
    )

    app = make_app(agents)
    logger.info("starting HTTP server on http://127.0.0.1:%d", port)
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()

    for task in runtime_tasks:
        task.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="blackjax-chat v3 (direct-API)")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    asyncio.run(_main(args.port))


if __name__ == "__main__":
    main()
