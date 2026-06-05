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


def _read_trace_jsonl(role_name: str) -> list[dict]:
    """Return the per-role trace.jsonl events (one dict per line).

    Used by ``/api/agents`` to enrich each agent's status with recent
    trace tail / inflight / last_result, the way v2's debug.html
    expects.
    """
    path = _DATA_DIR / "sessions" / f"{role_name}.trace.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _event_summary(ev: dict) -> tuple[str, str]:
    """Return (kind, short summary text) for one trace event."""
    kind = ev.get("_event") or "?"
    parts: list[str] = []
    txt = ev.get("text")
    if isinstance(txt, str) and txt:
        parts.append(txt)
    msg = ev.get("message")
    if isinstance(msg, dict):
        mt = msg.get("text")
        if isinstance(mt, str) and mt:
            parts.append(mt)
        tcs = msg.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    name = str(tc.get("name", ""))
                    if name:
                        parts.append(f"[{name}]")
    label = ev.get("label")
    if isinstance(label, str) and label:
        parts.append(label)
    src = ev.get("source")
    if isinstance(src, str) and src:
        parts.append(f"from={src}")
    return kind, " ".join(parts).strip()


def _diagnose_agent(label: str, agent: object) -> dict:
    """Build the rich per-agent record the debug page expects."""
    from datetime import datetime, timezone

    rt = getattr(agent, "runtime", None)
    in_flight_call = bool(getattr(rt, "model_call", None)) if rt else False
    inbox = getattr(rt, "inbox", None) if rt else None
    inbox_size = 0
    if inbox is not None:
        q = getattr(inbox, "_queue", None)
        if hasattr(q, "qsize"):
            inbox_size = q.qsize()

    # Trace-based enrichment (matches v2's debug page contract).
    events = _read_trace_jsonl(label)

    # Last assistant timestamp -> age_sec.
    age_sec: int | None = None
    last_assistant_ts: str | None = None
    for ev in reversed(events):
        if ev.get("_event") != "ModelResponseComplete":
            continue
        last_assistant_ts = ev.get("_ts", "")
        try:
            dt = datetime.fromisoformat(last_assistant_ts.replace("Z", "+00:00"))
            age_sec = int(
                datetime.now(timezone.utc).timestamp() - dt.timestamp(),
            )
        except (ValueError, AttributeError):
            pass
        break

    # In-turn detection: was there a ModelCallStarted with no matching
    # ModelIdle/ModelResponseComplete/ModelResponseError after it?
    started_idx = -1
    ended_idx = -1
    for i, ev in enumerate(events[-200:]):
        k = ev.get("_event") or "?"
        if k == "ModelCallStarted":
            started_idx = i
        elif k in ("ModelIdle", "ModelResponseComplete", "ModelResponseError"):
            ended_idx = i
    in_turn = started_idx > ended_idx

    # Best-effort inflight tool name.
    inflight: str | None = None
    if in_turn:
        for ev in reversed(events[-200:]):
            if (ev.get("_event") or "?") == "ToolLabel":
                inflight = str(ev.get("label") or ev.get("text") or "")
                break
        if inflight is None:
            inflight = "model thinking"

    # Last completed-turn result.
    last_result: dict | None = None
    for ev in reversed(events):
        k = ev.get("_event") or "?"
        if k == "ModelResponseComplete":
            last_result = {"ok": True, "ts": ev.get("_ts", "")}
            break
        if k == "ModelResponseError":
            last_result = {"ok": False, "ts": ev.get("_ts", "")}
            break

    # Recent trace tail (last 6, oldest-first so debug page can
    # ``.reverse()`` if it wants).
    recent: list[dict] = []
    for ev in events[-6:]:
        kind, summary = _event_summary(ev)
        recent.append({
            "ts": ev.get("_ts", ""),
            "kind": kind,
            "summary": summary[:200],
        })

    # Diagnosis: 1-line status string.
    if in_turn and (age_sec is None or age_sec > 90):
        status = "hung"
        diagnosis = (
            f"In a model call for {age_sec or '?'}s without a turn boundary. "
            f"Inflight: {inflight or 'model thinking'}."
        )
    elif in_turn:
        status = "working"
        diagnosis = f"Model call in flight. Inflight: {inflight or 'model thinking'}."
    elif inbox_size > 0:
        status = "stuck"
        diagnosis = f"Idle with {inbox_size} unanswered inbox item(s)."
    else:
        status = "idle"
        diagnosis = "Last turn complete, inbox empty — waiting for work."

    return {
        "role": label,
        "status": status,
        "diagnosis": diagnosis,
        "in_turn": in_turn,
        "inflight": inflight,
        "pending": inbox_size,
        "inbox_size": inbox_size,
        "pending_preview": [],  # debug page tolerates empty list
        "age_sec": age_sec,
        "last_result": last_result,
        "last_ts": last_assistant_ts,
        "recent": recent,
        "model_id": getattr(agent, "model_id", None),
        "total_cost_usd": float(getattr(agent, "total_cost_usd", 0.0) or 0.0),
        "total_tokens": _sum_tokens(getattr(agent, "total_tokens", None)),
        "token_breakdown": _token_breakdown(
            getattr(agent, "total_tokens", None),
        ),
    }


def _sum_tokens(tc: object) -> int:
    """Sum the 4 components of a ``TokenCount`` for a single dashboard number."""
    if tc is None:
        return 0
    parts = (
        getattr(tc, "input_tokens", 0) or 0,
        getattr(tc, "output_tokens", 0) or 0,
        getattr(tc, "cache_read_tokens", 0) or 0,
        getattr(tc, "cache_creation_tokens", 0) or 0,
    )
    return int(sum(parts))


def _token_breakdown(tc: object) -> dict:
    """Per-component dict for the debug page (cache hit vs creation is the
    interesting split when running cheap-tier models)."""
    if tc is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    return {
        "input": int(getattr(tc, "input_tokens", 0) or 0),
        "output": int(getattr(tc, "output_tokens", 0) or 0),
        "cache_read": int(getattr(tc, "cache_read_tokens", 0) or 0),
        "cache_creation": int(getattr(tc, "cache_creation_tokens", 0) or 0),
    }


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
        """Per-agent rich status (used by both web UI and /debug page).

        Returns the v2-shape ``recent``/``inflight``/``last_result``/
        ``age_sec``/``diagnosis`` fields that debug.html expects.
        Without these, the debug page renders "no trace events" for
        every agent.
        """
        del request
        out = [_diagnose_agent(label, agent) for label, agent in agents.items()]
        out.sort(key=lambda d: d["role"])
        total_cost = sum(a["total_cost_usd"] for a in out)
        total_tokens = sum(a["total_tokens"] for a in out)
        return JSONResponse({
            "now": _iso8601_z(),
            "agents": out,
            "total_cost_usd": total_cost,
            "total_tokens": total_tokens,
        })

    async def post(request: Request) -> Response:
        """Operator + peer ingress. Body: ``{to: str|list, body, from?, urgent?}``."""
        payload = await request.json()
        to_raw = payload.get("to", "")
        recipients = [to_raw] if isinstance(to_raw, str) else list(to_raw)
        recipients = [r.strip() for r in recipients if r and isinstance(r, str)]
        body = str(payload.get("body", ""))
        from_role = str(payload.get("from", "user")).strip() or "user"
        urgent = bool(payload.get("urgent", False))

        if not recipients or not body:
            return JSONResponse(
                {"error": "both 'to' and 'body' are required"},
                status_code=400,
            )

        # Validate all targets first
        from sagent.tools.core import agent_registry
        for to in recipients:
            if to != "user" and agent_registry.get(to) is None:
                return JSONResponse(
                    {"error": f"unknown target {to!r}; active: {sorted(agents)}"},
                    status_code=404,
                )

        _audit_append({
            "ts": _iso8601_z(),
            "from": from_role,
            "to": recipients,
            "body": body,
            **({"urgent": True} if urgent else {}),
        })

        for to in recipients:
            if to == "user":
                continue
            target = agent_registry.get(to)
            if from_role == "user":
                target.runtime.inbox.push_back(UserMessage(text=body, urgent=urgent))
            else:
                target.runtime.inbox.push_back(
                    AgentSendMessage(source=from_role, text=body, urgent=urgent),
                )

        return JSONResponse({"ok": True, "to": recipients, "from": from_role})

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
        qp = request.query_params
        # ``?around=N&ctx=K`` — return a K-event window on each side of
        # event index N. Used by the /debug page to show context around
        # search hits.
        if "around" in qp:
            try:
                around = int(qp["around"])
            except ValueError:
                around = total_on_disk - 1
            try:
                ctx = max(0, min(200, int(qp.get("ctx", "14"))))
            except ValueError:
                ctx = 14
            start = max(0, around - ctx)
            end = min(total_on_disk, around + ctx + 1)
            return JSONResponse({
                "events": events[start:end],
                "offset": start,
                "total": total_on_disk,
                "hit": around,
            })
        try:
            limit = max(1, min(20000, int(qp.get("limit", "2000"))))
        except ValueError:
            limit = 2000
        sliced = events[-limit:] if total_on_disk > limit else events
        return JSONResponse({
            "events": sliced,
            "total": total_on_disk,
            "returned": len(sliced),
        })

    async def search(request: Request) -> Response:
        """Full-text search across audit log + per-role traces.

        Used by /debug. Body shape (mirrors v2):
            ``?q=<text>&scope=traces|messages|all&limit=N``
        """
        import glob

        qp = request.query_params
        q = (qp.get("q") or "").strip().lower()
        scope = qp.get("scope", "all").lower()
        try:
            limit = max(1, min(2000, int(qp.get("limit", "300"))))
        except ValueError:
            limit = 300
        if not q:
            return JSONResponse({"results": [], "total": 0, "q": ""})
        results: list[dict] = []

        if scope in ("messages", "all"):
            if _AUDIT_LOG.exists():
                with _AUDIT_LOG.open(encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        haystack = (r.get("body") or "") + " " + " ".join(
                            r.get("to") or []
                        ) + " " + (r.get("from") or "")
                        if q in haystack.lower():
                            results.append({
                                "source": "messages",
                                "idx": i,
                                "ts": r.get("ts", ""),
                                "from": r.get("from", "?"),
                                "to": r.get("to") or [],
                                "snippet": (r.get("body") or "")[:200],
                            })
                            if len(results) >= limit:
                                break
        if scope in ("traces", "all"):
            for path in sorted(
                glob.glob(str(_DATA_DIR / "sessions" / "*.trace.jsonl")),
            ):
                role = Path(path).stem.replace(".trace", "")
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        _, summary = _event_summary(ev)
                        if q in summary.lower() or q in (ev.get("_event") or "").lower():
                            results.append({
                                "source": "trace",
                                "role": role,
                                "idx": i,
                                "ts": ev.get("_ts", ""),
                                "kind": ev.get("_event", "?"),
                                "snippet": summary[:200],
                            })
                            if len(results) >= limit:
                                break
                if len(results) >= limit:
                    break

        return JSONResponse({
            "results": results,
            "total": len(results),
            "q": q,
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
        Route("/api/search", search),
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
