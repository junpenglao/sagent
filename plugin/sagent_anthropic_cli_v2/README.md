# blackjax-chat

A multi-agent chat channel for BlackJAX, built as a plugin on top of
[sagent](https://github.com/rekursiv-ai/sagent). Five specialised
agents (`tl`, `swe`, `junior-swe`, `statistician`, `tech-writer`) run
as long-running asyncio tasks in one Python process, talking to each
other and a human operator through a typed inbox with mid-turn
preemption, on-the-fly status, and a web UI.

For the full history of how we got here (the failed `channel/` tmux
runtime, the structural limits we hit, the external-MCP probe that
unblocked us), see
[`claude-config/project/worklog/threads/chat-to-sagent-migration.md`](../../../claude-config/project/worklog/threads/chat-to-sagent-migration.md).
This README sticks to what's shipping and how to run it.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ blackjax-chat serve.py (single Python process, asyncio)                  │
│                                                                          │
│ ┌─────────┐  ┌──────────┐  ┌─────────────┐  ┌──────────┐  ┌──────┐       │
│ │ tl      │  │ swe      │  │ junior-swe  │  │ statist. │  │ tech │       │
│ │ Agent   │  │ Agent    │  │ Agent       │  │ Agent    │  │ Agent│       │
│ └────┬────┘  └────┬─────┘  └─────┬───────┘  └────┬─────┘  └──┬───┘       │
│      │            │              │               │           │           │
│      ▼            ▼              ▼               ▼           ▼           │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ per-agent claude --print --mcp-config <role>.mcp.json        │         │
│ └────────────────────────┬─────────────────────────────────────┘         │
│                          │ MCP stdio                                     │
│                          ▼                                               │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ mcp_sagent/server.py (separate Python process per agent)     │         │
│ │   sagent_send / sagent_defer / sagent_self                   │         │
│ └────────────────────────┬─────────────────────────────────────┘         │
│                          │ HTTP POST to 127.0.0.1:8767                   │
│                          ▼                                               │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ HTTP + web UI (Starlette+uvicorn)                            │         │
│ │   /api/{roles,agents,messages,trace,search,post,defer,restart}│        │
│ └──────────────────────────────────────────────────────────────┘         │
│                                                                          │
│  Runtime observers per agent:                                            │
│   • trace_writer    → sessions/<role>.trace.jsonl                        │
│   • restart_notice  → splices outbound reconstructions on respawn        │
└──────────────────────────────────────────────────────────────────────────┘
```

Three layers because each MCP server is its own Python process (spawned
by `claude --print` via `--mcp-config`) and can't reach the live
`agent_registry` in `serve.py`. HTTP loopback to `serve.py`'s
`/api/post` is the only synchronisation point all three layers share.
Cost is sub-millisecond per peer message, swamped by model-call latency.

**Data files** live under `$SAGENT_DATA_DIR` (set at launch), not the
plugin source tree. This lets the audit log co-locate with the legacy
`channel/main.jsonl` for `bin/merge_jsonl.py`:

```
$SAGENT_DATA_DIR/
├── main.jsonl                  ← audit log (channel/-compatible)
└── sessions/
    ├── <role>.trace.jsonl     ← per-agent runtime events
    ├── <role>.mcp.json        ← per-role MCP config
    └── mcp_calls.log          ← MCP server debug log
```

---

## What doesn't work out of the box

Three structural mismatches surfaced when we tried to drop our 5-agent
team onto stock sagent. Each one is the justification for one of the
patches in the next section. Full investigation in
[`worklog/threads/chat-to-sagent-migration.md`](../../../claude-config/project/worklog/threads/chat-to-sagent-migration.md);
the compressed version:

1. **Sagent's in-bridge tools (`AgentSend`, `Read`, `Bash` — bare
   names) don't get structurally dispatched by Sonnet/Opus.** Both
   models emit the tool calls as TEXT inside the assistant message
   (`<function_calls>` blocks, raw JSON, or prose) rather than as
   `tool_use` content blocks. Only Haiku used the structured channel
   reliably. This is independently reported in pydantic-ai#1904,
   jundot/omlx#159, and the Cursor forum — it's a `claude --print`
   streaming-mode issue, not specific to sagent. **The probe at
   `/tmp/sagent_probe/` (2026-06-01) found that external MCP tools
   surfaced via `--mcp-config` with the `mcp__<server>__<tool>` prefix
   ARE structurally dispatched by all three models.** That's why this
   plugin mounts `sagent_send` / `sagent_defer` / `sagent_self` via a
   separate MCP stdio server (`mcp_sagent/server.py`) instead of
   sagent's in-bridge tool registry, and why the three-process
   architecture exists at all.

2. **The CLI subprocess runs the entire MCP tool loop opaquely.** From
   sagent's POV, one `claude --print` turn is one
   `ModelCallStarted` → one `ModelResponseComplete` — the runtime
   never sees intermediate `tool_use` / `tool_result` blocks. So
   `_stop_all_tools` has no in-flight cohort to act on, and mid-turn
   `AgentSendMessage` arrivals just queue into `_mid_stream_queue`
   while the in-flight CLI keeps running. The headline "mid-turn
   preempt" sagent advertises doesn't work at subprocess
   granularity without a SIGINT path. That's the
   `preempt_in_flight=True` patch (override #2 below).

3. **Sagent's inbox-coalesce is single-user-shaped.** Upstream merges
   consecutive same-source `AgentSendMessage`s into one history entry.
   This is right for human typing (three lines typed in a row = one
   prompt) but wrong for distinct peer events: when TL sends a
   delegation, then a correction, then a hard `STOP`, the recipient
   must see those as three separate inbounds — not one 9 KB blob
   with `STOP` buried at the bottom. Override #1 inverts this.

Beyond those three: the CLI provider strips `AssistantMessage` entries
before re-feeding history to a respawned subprocess
(`providers/anthropic_cli.py:537`) — so on `aborted_streaming`
recovery, the model has no record of its own prior delegations. The
plugin works around this with the splice-based `restart_notice`
observer (override #3 below).

---

## Sagent behaviour overrides

Three places we deviate from upstream sagent. All justified by the
mismatches above.

### 1. `coalesce_inbox=False`  (upstream default: `True`)

Upstream merges consecutive same-source `AgentSendMessage`s into one
history entry. Correct for human typing, wrong for distinct peer events:
a delegation + correction + `STOP` from TL must arrive at SWE as three
separate inbounds, not one 9 KB blob with `STOP` buried at the bottom.

With the override, the runtime injects a synthetic
`AssistantMessage("(runtime: discrete-inbound boundary)")` between
consecutive peer messages instead — satisfies API alternation, keeps each
peer message distinct.

### 2. `preempt_in_flight=True`  (upstream default: `False`)

Sends SIGINT to the in-flight `claude --print` subprocess via
`model.cancel_in_flight()` before buffering. Required because the CLI
runs its MCP tool loop opaquely — sagent's runtime can't see in-flight
tool dispatches, so `_stop_all_tools` has nothing to act on. Without
this, mid-turn corrections wait for the current turn to drain.
Implementation lives on `feat/cli-preempt-via-sigint` in this fork.

### 3. `restart_notice` observer  (plugin-side, not a sagent flag)

`providers/anthropic_cli.py:537` strips ALL `AssistantMessage` entries
before re-feeding history to a respawned subprocess. The new subprocess
sees peer replies but NOT its own prior delegations — so on
`aborted_streaming`/`ede_diagnostic` recovery, opus rationally re-issues
delegations it already made.

The observer (`runtime/restart_notice.py`) watches for
`ModelResponseError`. When fired, it walks `runtime.tape`, recovers each
prior `sagent_send`'s `to`/`content`, and `runtime.append_splice`-es a
synthetic UserMessage immediately after each matching peer reply:

```
[from sagent runtime] You previously sent to @swe: "<original content>"
```

The respawned CLI subprocess then sees clean outbound→inbound pairings in
its stdin feed and naturally consolidates instead of re-delegating. The
observer also pushes one orienting `[handoff from previous session]`
notice onto the inbox summarising the reconstruction.

Unit tests in `tests/restart_notice_test.py` (11 passing); live
behavioural validation pending the next organic API hiccup.

---

## Running it

Production form used during 2026-06-02 live testing:

```bash
tmux new-session -d -s sagent-chat -n serve \
  -c /home/jp/blackjax-devs \
  'SAGENT_DATA_DIR=/home/jp/blackjax-devs/claude-config/experimental/sagent \
   exec ~/rekursiv/sagent/.venv/bin/python \
   /home/jp/rekursiv/sagent/plugin/sagent_anthropic_cli_v2/bin/serve.py --port 8767'
```

Three things this form gets right:

1. `-c /home/jp/blackjax-devs` sets tmux pane cwd → bash → python →
   `Path.cwd()` at agent construction → each Bash tool's `start_cwd`
   is the monorepo root, not the plugin source dir.
2. `SAGENT_DATA_DIR=…experimental/sagent` lands audit log + traces
   beside the legacy `channel/main.jsonl` for end-of-day merge.
3. Absolute path to `serve.py` — relative paths wouldn't resolve with
   `-c` pointing at `~/blackjax-devs`.

Casual local run (no monorepo, no co-location):

```bash
cd ~/rekursiv/sagent
uv run python plugin/sagent_anthropic_cli_v2/bin/serve.py --port 8767
```

Web UI at `http://127.0.0.1:8767/` — open via SSH tunnel:

```bash
ssh -L 8767:127.0.0.1:8767 <host>
```

`SERVE_HOST` is forced to `127.0.0.1` (loopback-only, no auth).

---

## Comparison: `channel/` vs sagent+CLI vs sagent+API

Today's plugin is the middle column. Column 1 is what we migrated away
from. Column 3 is the next plausible step (direct Anthropic SDK
instead of `claude --print` subprocesses), speculative and not built.

Marker key: ✅ works / materially better, ⚠️ works with caveats,
❌ broken or materially worse, 🔮 speculation.

| Dimension | `channel/` (tmux) | sagent+CLI (today) | sagent+API (speculative) |
|---|---|---|---|
| **Process model** | ❌ One Python worker per agent, per tmux pane, per systemd cgroup. | ✅ Single process, asyncio task per agent. | 🔮 Same, but no CLI subprocesses at all. |
| **Cross-agent latency** | ❌ 5–15 s (poll cycle + cold CLI start). | ✅ Sub-second (in-process inbox + warm subprocess). | 🔮 Sub-second, no subprocess to wait on. |
| **Per-turn token overhead** | ❌ 300–500 tok reminder appended per directive (CLI session_id resets). | ✅ ~Zero marginal (system prompt + tool description cached). | 🔮 ~Zero, with full operator control over `cache_control` markers. |
| **Mid-turn cancel** | ❌ `kill -9`, no clean shutdown. | ✅ SIGINT to subprocess (override #2). | 🔮 Native — close the SSE stream. |
| **History feed on respawn** | ⚠️ New CLI session; full history re-fed via stdin. | ⚠️ Sagent re-feeds, BUT `anthropic_cli.py:537` strips ALL `AssistantMessage` entries before write. | ✅ History is just `messages=`; assistant turns + `tool_use` + `tool_result` blocks all go in verbatim. |
| **Outbound visibility on respawn** | ✅ Survives the stdin re-feed. | ❌→✅ Stripped by default. **Fixed in-plugin** by `restart_notice` splice-based reconstruction (override #3). | ✅ Free — `tool_use` blocks in `messages=` verbatim. |
| **`aborted_streaming` recovery** | ❌ CLI dies; manual restart. | ⚠️ Auto-respawn + observer notice. Cost: full prompt-cache miss (sagent's re-feed fingerprint ≠ claude's session-resume bytes). | 🔮 Honour the API's own `retry_delay_ms` (we get it today but ignore it); reissue with the same `messages=`. Cache stays warm. **Likely the biggest token-cost win** under organic-error pressure. |
| **Tool results in history** | ⚠️ Recreated from scratch each turn. | ⚠️ Internal to CLI; `ToolResult` in `agent.history` raises on the stdin path (`anthropic_cli.py:813-818`). Can't replay prior turns. | ✅ First-class user-message block; replayable. |
| **Prompt-cache hit rate** | ❌ Low (CLI session resets per turn). | ⚠️ Medium; respawn eats a full cache miss. | 🔮 High and operator-controllable. |
| **Observability** | ⚠️ Manual log scraping. | ✅ `/api/agents`, `/api/trace/<role>`, `/debug` console, web UI. | 🔮 Inherits the plugin's `/api/*` and traces — they observe runtime events, not transport. |
| **Implementation complexity** | ❌ Per-pane workers, mention router, polling, cgroup wiring. | ⚠️ Single binary, but three overrides + HTTP MCP bridge + observer needed. | 🔮 Direct SDK calls; observer + most overrides become unnecessary. |
| **Measured per-turn cost** | Baseline. | ✅ ~30% lower than `channel/`. | 🔮 Likely another 20–40% lower under error pressure; on par on the happy path. |

**Summary.** Migrating `channel/` → sagent+CLI was a big win on
latency, token cost, and observability. The price was inheriting two
CLI-shape problems (history stripping; opaque retry on `aborted_streaming`)
that we now mitigate in-plugin via overrides + the splice-based
`restart_notice` observer. The sagent+API jump is plausible if error
pressure stays elevated — it would delete the observer complexity and
reclaim the prompt-cache on recovery — but needs a non-CLI sagent
provider.

---

## Validation status (2026-06-02 evening)

**Closed** (live-validated today):

- ✅ Mention-router duplicate-emit cannot reproduce (router is gone).
- ✅ `hello, ready` warmup-template regression cannot reproduce
  (warmup uses `sagent_self`, silent to peers).
- ✅ `sagent_defer` round-trip works (`tl` scheduled +30 s, SWE
  later self-deferred +300 s and +180 s for CI polling — both fired
  on time, no `bash sleep` hangs).
- ✅ Structured channel works on opus/sonnet/haiku via external MCP
  (every live test today produced `tool_use` for
  `mcp__sagent_chat__sagent_send`).
- ✅ Mid-turn preempt works (SIGINT path fired during organic
  `ModelResponseError` events).
- ✅ Per-turn cost ~50% lower than the pre-migration baseline; ~30%
  lower than `channel/` on equivalent workloads.
- ✅ Sub-second cross-agent latency.

**Open**:

- [ ] `bin/merge_jsonl.py` round-trip across both streams (~30 min check).
- [ ] End-to-end PR drive (implementation phase, not just plan mode).
- [ ] Live validation of the `restart_notice` splice path under organic
  `aborted_streaming` (unit tests + deterministic seed test pass; needs
  an organic firing).
- [ ] Phase 6 cutover decision file at
  `claude-config/project/worklog/decisions/2026-06-02-blackjax-chat-cutover.md`.
- [ ] `/debug` page walkthrough (main `/` view confirmed).

---

## Known issues

### A. Opus occasionally writes the reply as plain text without calling `sagent_send`

Observed 12:31. TL produced a 1.8 KB recap as plain assistant text
with empty `tool_calls`. The reply appears in `sessions/tl.trace.jsonl`
but NOT in `main.jsonl` or the chat view.

**Operator recovery:** when TL appears idle but the expected reply is
missing, check the most recent `ModelResponseComplete` in
`sessions/tl.trace.jsonl` — the body is there. Manually `POST` it via
`/api/post` with `from=tl, to=user` if you want it surfaced.

No structural fix yet. Stronger `PEER_MESSAGING` wording has been
tried twice with limited effect.

### B. `aborted_streaming` / `ede_diagnostic` errors (the biggest live problem)

**This is the dominant operational pain point as of 2026-06-02 —
worth its own section.** Across the day the Anthropic streaming API
fired `SubprocessTransportError: aborted_streaming` and
`ede_diagnostic` errors on opus and sonnet roughly once every 3–10
minutes during sustained multi-agent traffic. Real examples observed
today:

- 12:53–12:58: three back-to-back errors on TL (5 min window).
- 17:36–17:48: three errors on TL + one on statistician (12 min).
- 18:29–18:30: two errors on TL inside 60 s — the second fired ~6 s
  into the recovery turn, before the first had finished consolidating.
- 19:02+: still flaring intermittently on the running server.

**What it looks like operationally:**

- The agent's CLI subprocess dies mid-stream with
  `SubprocessTransportError("AnthropicCLI: result is_error: …
  terminal_reason: aborted_streaming … errors: ['[ede_diagnostic]
  result_type=user last_content_type=n/a stop_reason=tool_use'])`.
- Sagent's `_AnthropicCLIModel` publishes `ModelResponseError` and
  respawns a fresh `claude --print` subprocess automatically.
- The respawn re-feeds history but strips `AssistantMessage` entries
  (`anthropic_cli.py:537`). Without the `restart_notice` observer the
  fresh subprocess has no record of its own prior delegations and
  often re-issues them — the original symptom we built the observer
  for.
- **Each respawn eats a full prompt-cache miss.** Sagent's re-fed
  byte sequence ≠ what claude's own session-resume would emit, so
  Anthropic's cache key doesn't match. On opus this is the largest
  single token cost per recovery.
- The error response from the API includes a `retry_delay_ms`
  schedule (we've seen `506 ms / 1247 ms / 2107 ms …` in headers).
  **Sagent does not honour it.** `agent/retry.py:345`'s whitelist
  bails on `aborted_streaming` / `ede_diagnostic` / `529` and goes
  straight to subprocess respawn instead of retrying the same call.

**What's mitigated today:**

- ✅ Auto-respawn — sagent's `HotSpare` brings the agent back without
  operator intervention.
- ✅ Model-side anchoring on respawn — the `restart_notice` observer
  (override #3 above) walks the tape, recovers each prior
  `sagent_send`'s arguments, and splices a `[from sagent runtime]
  You previously sent to @<peer>: "<content>"` after each matching
  peer reply. The respawned subprocess sees the conversation as
  paired outbound→inbound and naturally consolidates instead of
  re-delegating.

**What's NOT mitigated yet (the biggest open lever):**

- ⏳ **Honour `retry_delay_ms` in `send_with_retry`** instead of
  respawning. This is the upstream fix — ~10 lines in
  `sagent/agent/retry.py:345` to expand the retryable-error whitelist
  and use the schedule the API itself emits. Would eliminate most
  respawns at the source (the cause, not the symptom), preserve the
  prompt-cache, and make the `restart_notice` observer's job rare
  rather than per-incident. Not yet staged.
- ⏳ **Per-role circuit breaker** — when N `ModelResponseError`
  events fire on one role within M minutes, auto-restart the role
  and surface to the operator. Higher complexity. Only worth doing
  if the upstream retry fix doesn't sufficiently quiet the errors.

**Operator playbook** while we're stuck with respawns:

- Watch the server log for
  `RestartNoticeObserver: @<role> ModelResponseError`. Each line is
  one recovery. Repeated firings on the same role within ~1 min
  often cascade — consider `/api/restart` to wipe + recover cleanly
  rather than letting the observer paper over multiple stacked
  errors.
- The web UI's per-agent diagnosis (`hung` with high `age_sec`) often
  reflects a respawn in progress rather than a stuck agent.
- Token cost spikes during error storms are real — opus respawns
  burned ~$0.20–$0.40 per recovery in today's runs. If error
  pressure stays elevated, prioritising the `retry_delay_ms`
  upstream fix is the biggest single token-cost lever available.

---

## Status

Plugin is functional for daily operator use. `channel/` can be shut
down in parallel whenever ready. The Phase 6 decision file is the
remaining paperwork. **The `aborted_streaming` error rate is the
dominant residual risk** — the in-plugin mitigation (`restart_notice`
splice) is in place, but the upstream `send_with_retry` patch is
the actual fix if the API pressure stays elevated.
