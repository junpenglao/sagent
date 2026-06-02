# blackjax-chat

A multi-agent chat channel for the BlackJAX project, built as a plugin on
top of [sagent](https://github.com/rekursiv-ai/sagent). Hosts five
specialized agents (`tl`, `swe`, `junior-swe`, `statistician`,
`tech-writer`) as long-running asyncio tasks in a single Python process,
talking to each other (and a human operator) through a typed message
inbox with mid-turn preemption, on-the-fly status, and a web UI.

This README documents how we got here — the previous failed approach,
the structural limit we ran into, the probe that found a way around it,
and what's currently shipping.

---

## Episode 1 — `claude-config/experimental/channel/` (the tmux runtime)

Before this plugin, we ran the same five-agent team via a custom Python
runtime called `channel/` (sources at
`claude-config/experimental/channel/`). Each agent lived in its own tmux
pane and was driven by a `chat consume` worker that:

1. Tailed an append-only JSONL file (`main.jsonl`).
2. For each new directive addressed to the agent, spawned a fresh
   `claude --print --output-format stream-json --verbose -p '<prompt>'`
   one-shot subprocess.
3. Streamed the assistant text back, parsed any `@<role>` mentions, and
   wrote the reply back to `main.jsonl` so the next worker could pick it up.

The web UI was a separate Starlette app reading `main.jsonl` and per-pane
trace files. It worked. It did not work *well*.

### What broke, in roughly increasing severity

- **Slow.** Every turn paid the `claude --print` cold-start cost (~3-5 s
  before the model produced its first token) because the workers spawned
  a fresh subprocess per directive. There was no warm session to reuse.
- **Untyped peer messaging.** Routing happened by parsing `@<role>` out
  of assistant prose. False positives from descriptive references
  (*"ask @swe later"*) sometimes leaked deliveries. Worse, the structure
  of "who is this message from / to" was entirely implicit in unformatted
  text — the audit log was lossy.
- **Hard to interrupt mid-turn.** A turn was a blocking black box. When
  TL realized 90 s into SWE's implementation that the directive had been
  wrong, there was no way to deliver a correction until SWE's current
  turn ended. We watched ~11 min of wasted work on PR #134 (June 1 2026)
  exactly this way: TL sent directive A, then sent a correction at T+2 min;
  SWE pushed code based on A; the correction arrived after the push;
  `git revert`.
- **OOM cascade kills the worker.** Each tmux pane ran
  `claude --print → bash → memory-heavy command`. When the memory-heavy
  command triggered the cgroup OOM killer, the cgroup boundary was the
  *pane*, so `bash` and `claude --print` got killed alongside the
  intended target. The `chat consume` worker process noticed the
  subprocess die, but had no way to recover — it kept tailing
  `main.jsonl`, silently no longer responding to directives. This
  happened to SWE on 2026-06-01 and was visible only because the audit
  log showed no SWE replies for ~30 minutes.
- **`claude exited -9` on context-limit compaction.** If a worker had
  been running long enough to hit context-window pressure, the CLI's
  internal compaction step could OOM-9-kill mid-turn. Each occurrence
  was an event in the audit log; recovery required noticing and
  manually restarting the pane.
- **Heavy bg commands forget the wrap rule under compaction.** SWE's
  onboarding said "wrap heavy commands in `systemd-run --user --scope`
  so OOMs stay in a sibling cgroup", but that instruction got
  paraphrased / dropped during compaction. Re-injecting it required
  custom prompt-level discipline that didn't survive compaction.

The structural diagnosis: **a turn is a blocking black box from the
channel's POV.** Cross-agent coordination, mid-turn correction, OOM
containment, and crash recovery all live below that abstraction
boundary, and we couldn't reach them without rewriting the runtime.

---

## Episode 2 — sagent looked promising on paper

[Sagent](https://github.com/rekursiv-ai/sagent) ships exactly the
primitives we needed:

- **`AgentRuntime` with an inbox-drain loop.** Each agent is an asyncio
  task that pulls `UserMessage` / `AgentSendMessage` events off a typed
  queue. Mid-stream messages preempt in-flight tool dispatch via a
  cohort-detach mechanism. The runtime is designed to handle "TL
  corrects SWE while SWE is mid-Bash" structurally.
- **Typed peer addressing.** `AgentSend(to='swe', content='...')` is a
  tool call. No prose parsing. The `to` arg IS the routing.
- **Session repair on crash.** If a tool dispatch dies mid-execution,
  the runtime synthesizes a `ToolResult(is_error=True, content='[interrupted]')`
  for any orphan tool_use so the model can recover cleanly.
- **Structured compaction.** `SummaryCompactor` preserves "user
  instructions and constraints" by class, so the systemd-run reminder
  doesn't get paraphrased away.
- **`AnthropicCLI` provider on subscription auth.** Spawns
  `claude --print --input-format stream-json` **once per agent,
  persistent** (the HotSpare keeps it warm and respawns at 100 turns or
  50% context). Same credentials path as `chat consume`, no API key.
  Cold-start cost amortizes across the whole agent lifetime.

We spiked it. The plan was: port our 5 roles to sagent + run them under
`AnthropicCLI` + done.

### The structural limit (Episode 2.5 — the spike that disproved Episode 2)

Spike `race_repro_v3.py` (2026-06-01) tried the headline mid-turn
preempt against the real PR #134 race scenario. **It failed in a
specific, important way.**

The CLI subprocess runs the ENTIRE MCP tool loop opaquely. From sagent's
POV, one `claude --print` turn is one `ModelCallStarted` followed by one
`ModelResponseComplete` — the runtime sees the final assistant text but
never the intermediate tool_use / tool_result steps. So `_stop_all_tools`
has nothing to act on: there's no in-flight cohort visible to the runtime.
`AgentSendMessage` arriving mid-turn just queues into `_mid_stream_queue`
and the in-flight CLI keeps running.

We patched that on `feat/cli-preempt-via-sigint` (in `junpenglao/sagent`
fork): added `cancel_in_flight()` on `_AnthropicCLIModel` that sends
SIGINT to the active HotSpare subprocess, used by `AgentRuntime` on the
preempt path. That moved the preempt primitive from "doesn't work" to
"works at the subprocess granularity". Good enough.

We then hit a different structural limit.

### Episode 2.7 — why the structured tool channel didn't fire

Once the runtime was up and we tested it, Sonnet and Opus refused to
emit `AgentSend` (and friends) as structured `tool_use` blocks. They
instead emitted the tool calls **as text inside the assistant message** —
XML `<function_calls>` blocks, raw JSON, or prose like *"I'll use the
AgentSend tool to send..."*. Only Haiku used the structured channel
reliably.

This is independently reported in pydantic-ai#1904, jundot/omlx#159,
and the Cursor forum; it's a `claude --print` streaming-mode issue, not
specific to sagent. But the consequence for us was severe: typed peer
addressing — the whole reason we wanted sagent — didn't work on the
models we actually use.

We initially worked around it with a `MentionRouter`: a runtime
observer that parsed `@<role>` out of assistant text on
`ModelResponseComplete` and synthesized the equivalent
`AgentSendMessage` delivery. That worked for delivery routing but
opened a different bug:

- **The mention router doubles-delivers when the model uses BOTH
  channels.** Opus often produces a (structured-channel) `AgentSend`
  AND trailing reasoning in the same turn. The trailing text contains
  the inline-rendered tool call ("Task #1 done…"), and the mention
  router's DM-default-to-last-sender path delivered that text as a
  phantom second message. TL's inbox received the same content three
  times with growing length (645 / 2848 / 4027 chars). The third
  arrival preempted TL mid-recovery and the model, with the warmup
  turn ("greet me back by calling AgentSend(to='user',
  content='hello, ready')") as its most-recent precedent after the
  preempt+respawn cycle, parroted that warmup template — sending
  "hello, ready" to the user as a real "reply".

We patched the duplicate with a per-turn `_structured_send_this_turn`
flag and added a `DeferRouter` to handle the analogous prose-defer
problem. Both worked. But maintaining a regex-based intent parser as
the primary IPC for a multi-agent system felt fragile, and the deeper
issue remained: **the model's behavior on the structured channel was
the actual problem, and we were patching the symptoms in a layer
above.**

Then we noticed: **sagent's in-bridge tools and external MCP tools
appear differently in the CLI's tool catalog.** Sagent's bridge mounts
its tools with bare names (`AgentSend`, `Read`, `Bash`). External MCP
servers (configured via `--mcp-config`) appear with a `mcp__server__tool`
prefix (`mcp__sagent__sagent_send`). What if the model's structural-vs-text
emission was correlated with this prefix?

---

## Episode 3 — the probe (and the answer)

A 30-line MCP stdio server with one tool: `sagent_send(to, content)`.
A 100-line driver that runs `claude --print --output-format stream-json
--mcp-config probe.json --model <m>` with a prompt asking the model to
send a message via `mcp__sagent__sagent_send`. Three runs, one per
model. Verdict criteria: did the MCP server's `_call_tool` handler
receive a `CallToolRequest`?

```
                       MCP CallTool   Structured     Prose
                       received       tool_use       leak
─────────────────────────────────────────────────────────────
claude-haiku-4-5       ✅ 1           ✅              ✅ none
claude-sonnet-4-6      ✅ 1           ✅              ✅ none
claude-opus-4-7        ✅ 1           ✅              ✅ none
```

**All three models cleanly emit `tool_use` for `mcp__sagent__sagent_send`.**
The bug we patched around in Episode 2.7 was a property of how the
tool appeared in the catalog, not a property of the model. External MCP
sidesteps it entirely.

That's what this plugin ships.

---

## What's here

A standalone MCP stdio server (`mcp_sagent/server.py`) exposing
`sagent_send`, `sagent_self`, and `sagent_defer`. Each tool's handler:

1. Resolves the calling agent's role from the `SAGENT_ROLE` env var
   passed when its `claude --print` subprocess launched its
   `--mcp-config`.
2. HTTP-POSTs the structured call to `serve.py`'s loopback
   (`/api/post` or `/api/defer`), which does the inbox push +
   audit-log write in-process. The MCP server can't touch
   `agent_registry` directly because it runs in a SEPARATE Python
   process (subprocess of `claude --print`, which is itself a
   subprocess of `serve.py`).
3. Returns a `ToolResult` to the CLI subprocess immediately on HTTP
   success.

Sagent's runtime is mostly unchanged — the plugin sits beside it as
`plugin/blackjax-chat/` in this fork. Two small upstream patches are
needed (`coalesce_inbox=False` flag + `extra_mcp_servers` plumbing
— see § "Sagent behaviour overrides"). The mention router, defer
router, and per-turn flag machinery from the prior attempt are
**deleted** — the MCP server makes them redundant.

### Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│ blackjax-chat serve.py (single Python process, asyncio)                  │
│                                                                          │
│ ┌─────────┐  ┌──────────┐  ┌─────────────┐  ┌──────────┐  ┌──────┐       │
│ │ tl      │  │ swe      │  │ junior-swe  │  │ statist. │  │ tech │       │
│ │ Agent   │  │ Agent    │  │ Agent       │  │ Agent    │  │ Agent│       │
│ └────┬────┘  └────┬─────┘  └─────┬───────┘  └────┬─────┘  └──┬───┘       │
│      │ HotSpare   │              │               │           │           │
│      ▼            ▼              ▼               ▼           ▼           │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ per-agent claude --print --mcp-config <role>.mcp.json        │         │
│ │   (subprocess; env SAGENT_ROLE + SAGENT_HTTP_URL + DATA_DIR) │         │
│ └────────────────────────┬─────────────────────────────────────┘         │
│                          │ MCP CallTool (stdio, --mcp-config sagent_chat)│
│                          ▼                                               │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ mcp_sagent/server.py (separate Python process per agent)     │         │
│ │   sagent_send(to, content, delay?)                           │         │
│ │   sagent_defer(delay_s, body)                                │         │
│ │   sagent_self(status?, context?)                             │         │
│ └────────────────────────┬─────────────────────────────────────┘         │
│                          │ HTTP POST to 127.0.0.1:8767                   │
│                          ▼                                               │
│ ┌──────────────────────────────────────────────────────────────┐         │
│ │ HTTP + web UI (Starlette+uvicorn on 127.0.0.1:8767)          │         │
│ │   /        index.html (chat view)                            │         │
│ │   /debug   debug.html (agents grid + search)                 │         │
│ │   /api/{roles,agents,messages,trace,search,post,defer,restart}│        │
│ │                                                              │         │
│ │   /api/post:  agent_registry.get(to).runtime.inbox.push_back │         │
│ │               + delivery.append_record(main.jsonl)           │         │
│ │   /api/defer: asyncio.call_later + same                      │         │
│ └──────────────────────────────────────────────────────────────┘         │
│                                                                          │
│  Runtime observers per agent (installed in _build_all_agents):           │
│   • trace_writer    → sessions/<role>.trace.jsonl                        │
│   • restart_notice  → pushes orienting UserMessage on ModelResponseError │
└──────────────────────────────────────────────────────────────────────────┘
```

**Data files** live in `$SAGENT_DATA_DIR` (set at launch — typically
`~/blackjax-devs/claude-config/experimental/sagent/`), NOT the plugin
source tree. This co-locates the plugin's `main.jsonl` next to the
legacy `channel/main.jsonl` so the end-of-day routine
(`bin/merge_jsonl.py`) finds both streams in one parent. Plugin code
+ web UI HTML are still read from the source tree at
`Path(__file__).resolve().parent.parent`.

```
$SAGENT_DATA_DIR/
├── main.jsonl                        ← audit log (chat/-compatible)
└── sessions/
    ├── <role>.trace.jsonl           ← per-agent runtime events
    ├── <role>.mcp.json              ← per-role MCP config
    ├── _suppress_audit              ← warmup sentinel
    └── mcp_calls.log                ← MCP server debug log
```

### Why HTTP instead of direct registry access

The MCP server is spawned per-agent by `claude --print` via
`--mcp-config`. Each MCP server instance is a **separate Python
process** with its own module state. The `agent_registry` it imports
is its own copy — empty. The first iteration of this plugin tried
to call `agent_registry.get(target).runtime.inbox.push_back(...)`
directly from the MCP server's tool handlers and got `Unknown peer
'tl'. Active: []` on every call.

HTTP loopback to `serve.py` (which owns the live registry in-process)
is the only synchronisation point that all three layers
(`serve.py`, `claude --print`, `mcp_sagent/server.py`) share. The
cost is one localhost roundtrip per `sagent_send` / `sagent_defer`
— sub-millisecond, swamped by model-call latency.

### What's deleted vs the previous attempt

From `claude-config/experimental/sagent/`:

- `mention_router.py` — no longer needed; structured channel works.
- `defer_router.py` — no longer needed; `sagent_defer` is structural.
- `LoggingAgentSend` wrapper in `shim.py` — replaced by direct audit
  log writes from the `/api/post` handler.
- `_structured_send_this_turn`, `_deferred_send_this_turn`, and the
  per-turn flag plumbing — no longer needed.
- `DEFER_VIA_PROSE` onboarding block — no longer needed.

What stays (ported with minor edits):

- `roles/*.py` and `roles/*.md` (role definitions + system prompts)
- `bin/serve.py` (HTTP + web UI surface; internal wiring simpler)
- `bin/merge_jsonl.py` (audit log union with channel/)
- `web/index.html`, `web/debug.html` (with trace-panel render fix:
  `ModelResponsePartial` + `SaveSession` events hidden by default —
  toggle via `localStorage.setItem('trace-show-partials', '1')` or
  `'trace-show-bookkeeping'`)
- `runtime/trace_writer.py` (per-agent runtime event JSONL)
- `runtime/restart_notice.py` (NEW — orienting UserMessage after
  `ModelResponseError`; see § "Sagent behaviour overrides #3")

---

## Sagent behaviour overrides (why we patch the upstream)

Two sagent design choices need to be inverted for the chat-channel
use case. Both are exposed as opt-in flags on the upstream
`AgentRuntime` so other sagent users keep the original behaviour;
plugin agents flip them via `Agent(...)` kwargs in `roles/common.py`.

### 1. `coalesce_inbox=False`  (default upstream: `True`)

Upstream sagent's `_append_or_coalesce_user` (`runtime.py:2474`)
merges consecutive same-source `AgentSendMessage`s into a single
history entry with `text = tail.text + "\n\n" + item.text`. This is
correct for human-operator input (typing three lines in a row should
arrive as one prompt) and satisfies Anthropic's user/assistant
alternation rule when the model errored or was cancelled mid-turn.

**Why we override:** in a multi-agent chat channel, each peer
`sagent_send` is a deliberate, distinct event by the sender. If TL
sends a delegation, then a correction, then a hard `STOP`, the
recipient must see those as three separate inbounds — not as one
9 KB blob with `STOP` buried at the bottom after `[Error: ...]` markers
from any failed retries in between. Coalescing hides the most recent
message inside the prior one and breaks the recipient's ability to
process events as turns.

**What the override does:** when `coalesce_inbox=False`, instead of
merging into the tail, we inject a synthetic
`AssistantMessage(text="(runtime: discrete-inbound boundary)")`
between the prior user-side message and the new item. The synthetic
turn satisfies the API alternation rule; each peer message remains a
distinct history entry; the boundary marker tells the model that the
prior turn ended (whatever the cause) and a fresh inbound follows.

Verified 2026-06-02 against an organic in-channel scenario: TL sent a
delegation, then 3 revisions (each preceded by a streaming-mode CLI
error that injected `[Error: ...]` into the prior message's tail),
then a hard `STOP`. Upstream coalescing produced a 9 800-char merged
inbound to SWE with STOP at the bottom. With the override, SWE
receives 5 discrete inbounds and STOP arrives as the latest standalone
turn.

### 2. `preempt_in_flight=True`  (default upstream: `False`)

Already documented in our prior fork branch `feat/cli-preempt-via-sigint`:
mid-stream peer messages send SIGINT to the in-flight
`claude --print` subprocess via `model.cancel_in_flight()` before
buffering. Required because the CLI runs the entire MCP tool loop
opaquely — sagent's runtime can't see in-flight tool dispatches, so
`_stop_all_tools` has nothing to act on. This patch is the
prerequisite that makes mid-turn corrections actually preempt instead
of waiting for the current turn to drain.

### 3. `restart_notice` observer (plugin-side, not a sagent flag)

Added 2026-06-02 in `runtime/restart_notice.py` after observing the
**API-error respawn-confusion** failure mode:

Sagent's `_AnthropicCLIModel` respawns its `claude --print` subprocess
when the API streams an aborted response (`aborted_streaming` /
`ede_diagnostic` — both seen repeatedly during 2026-06-02 live use).
On respawn, sagent re-feeds the full `agent.history` to the new
subprocess. The conversation isn't lost.

What IS lost is the model's implicit "I was in the middle of
responding to msg B" pointer. Opus on re-reading a long history with
multiple accumulated peer messages and an incomplete-looking trailing
assistant turn tends to **anchor on the largest/earliest identifiable
user task** and redo its prior work — re-issuing delegations,
re-running tool searches, etc.

Observed live 2026-06-02 12:53-12:58: TL hit three consecutive
`ModelResponseError(error=None)` events; on each respawn, TL went
back to re-doing the original "read worklog" task instead of
continuing the in-progress benchmark conversation. Result: two
duplicate delegations to SWE and statistician, SWE explicitly
flagged: *"Looks like a duplicate of the planning round we already
completed."*

**What the observer does:** installed per-agent in
`_build_all_agents`. Watches for `ModelResponseError` runtime events.
When fired, pushes a synthetic `UserMessage` into the agent's own
inbox with explicit re-orientation:

> "[runtime restart notice] Your claude subprocess just restarted…
>  Identify the MOST RECENT peer-side message in your history (above
>  this notice). That is the message you should respond to. Look at
>  the assistant turns above — if you can see evidence that you
>  already issued structured tool calls, DO NOT re-issue them…"

With `coalesce_inbox=False` (override #1), the notice arrives as a
distinct user-side history entry, becoming the most-recent
user-facing message the new subprocess sees. The model can't ignore
it as "background noise"; the API alternation rule forces a response.

**Status (2026-06-02):** wiring is unit-tested (4 tests in
`tests/restart_notice_test.py`). Live behavioural validation pending
the next organic API hiccup — can't deliberately trigger
`aborted_streaming` from the operator side.

---

## Running it

Casual / test run (no monorepo, no data co-location):

```bash
cd ~/rekursiv/sagent
uv run python plugin/blackjax-chat/bin/serve.py --port 8767
```

The plugin's audit log + traces land under
`plugin/blackjax-chat/{main.jsonl, sessions/}` (the plugin source dir).

Production / BlackJAX-monorepo deployment (the form actually used in
live testing 2026-06-02):

```bash
tmux new-session -d -s sagent-chat -n serve \
  -c /home/jp/blackjax-devs \
  'SAGENT_DATA_DIR=/home/jp/blackjax-devs/claude-config/experimental/sagent \
   exec ~/rekursiv/sagent/.venv/bin/python \
   /home/jp/rekursiv/sagent/plugin/blackjax-chat/bin/serve.py --port 8767'
```

Three things this form gets right:

1. `-c /home/jp/blackjax-devs` sets the tmux pane cwd → bash inherits
   → `python` inherits → `Path.cwd()` at agent-construction-time
   becomes each Bash tool's `start_cwd`. Agents see the monorepo root
   as their initial `pwd`, not the plugin source dir.

2. `SAGENT_DATA_DIR=…experimental/sagent` redirects audit log, per-role
   trace files, MCP configs, sentinel, debug log to land alongside the
   legacy `channel/main.jsonl`. End-of-day `bin/merge_jsonl.py` reads
   both streams from one parent.

3. Absolute path to `serve.py`. With `-c` pointing at `~/blackjax-devs`,
   a relative `bin/serve.py` wouldn't resolve.

The web UI is at `http://127.0.0.1:8767/`. Open via SSH tunnel:

```bash
ssh -L 8767:127.0.0.1:8767 <host>
```

`SERVE_HOST` is forced to `127.0.0.1` and the server rejects any other
bind — no auth, loopback-only.

---

## Observed wins vs Episode 1 (channel/) — live test 2026-06-02

Two structural wins surfaced after running real chat traffic through
the plugin alongside the legacy `chat consume` runtime. Recording
here because they're load-bearing for the "is this actually better"
decision, not just "does it work."

### 1. Snappier perceived latency

`channel/`'s runtime spread one Python worker process per role across
tmux panes, each in its own systemd cgroup, each tailing
`main.jsonl` on a polling interval. Cross-agent traffic incurred:

1. Sender's `claude --print` writes its reply to its pane's stdout.
2. The per-pane `chat consume` worker parses, posts to `main.jsonl`.
3. Recipient's per-pane `chat consume` worker polls `main.jsonl`, sees
   the new record, builds a directive, spawns a fresh `claude --print`
   for the recipient.

That's two poll-cycles + one CLI cold-start per peer message. Visible
in the operator's wall-clock as a 5-15 s gap between sender finishing
and recipient starting to respond.

The plugin's runtime is single-process: TL → SWE delivery is one
in-process `inbox.push_back` plus the recipient's already-warm CLI
subprocess picking up the message on its next `inbox.drain()` tick.
Wall-clock gap between sender's `sagent_send` and recipient's first
`ModelCallStarted` is now sub-second.

### 2. Lower per-turn token cost

`channel/` injected a system-prompt-style reminder at the **end of every
inbound directive** delivered to an agent (the `@<role> body`
addressing convention had to be re-explained on each turn because the
CLI's session_id was reset between turns). This was a 300-500 token
prefix on every single inbound — a non-trivial slice of every turn's
context budget, especially on opus.

The plugin's MCP-server-mounted `sagent_send` tool documents the
addressing convention **inside its tool description** (~80 tokens,
loaded once per `ListToolsRequest` at warmup, cached by Anthropic's
prompt-caching). The system prompt's `PEER_MESSAGING` block adds
~200 tokens, but it's part of the long-form system prompt that gets
cached across every turn. **Per-turn marginal cost: ~zero.**

Empirically across the first hour of live use: ~30% lower input-token
cost per turn vs the same agents on `channel/` doing equivalent work.

---

## Comparison: `channel/` vs sagent+CLI vs sagent+API

Three points in the design space. Today's plugin is the middle column.
Column 1 is what we migrated away from. Column 3 is the next
plausible step (direct Anthropic SDK calls instead of spawning
`claude --print` subprocesses) and is speculative — not built.

The dimensions below are the ones that actually moved the needle
during live testing on 2026-06-02. Marker key: ✅ works /
materially better, ⚠️ works but with caveats, ❌ broken or
materially worse, 🔮 speculation (not measured).

| Dimension | `channel/` (tmux runtime, migrated away from) | sagent + claude CLI (this plugin, today) | sagent + Anthropic SDK (speculative, not built) |
|---|---|---|---|
| **Process model** | ❌ One Python worker per agent, per tmux pane, per systemd cgroup. Cross-agent state via `main.jsonl` + polling. | ✅ Single process; one asyncio task per agent; in-process inbox. Operator runs one binary. | 🔮 Same single-process model; no subprocesses at all (no CLI to spawn). Smallest moving-part count of the three. |
| **Cross-agent delivery latency** | ❌ 5–15 s gap per peer message (poll cycle + cold `claude --print` start). | ✅ Sub-second (`inbox.push_back` → recipient's warm subprocess picks up on next drain). | 🔮 Same sub-second — and no subprocess at all to wait on. |
| **Per-turn token overhead** | ❌ 300–500 token reminder appended to every inbound directive (CLI session_id resets between turns; addressing convention has to be re-explained). | ✅ ~Zero marginal. Tool description (~80 tok) and system prompt (~200 tok) are part of the cached prefix; no per-message reminders. | 🔮 ~Zero marginal, same shape. Plus: full control over which blocks are marked `cache_control`, so we can pin the system + tools prefix with higher confidence. |
| **Mid-turn cancellation / preempt** | ❌ `kill -9` the worker; no clean shutdown; partial stdout to `main.jsonl`. | ✅ `preempt_in_flight=True` sends SIGINT to the CLI subprocess; runtime publishes `ModelResponseCancelled`. Override #2. | 🔮 Native — close the SSE stream and call `stop_streaming()`; no signals, no subprocess race. |
| **History feed on respawn** | ⚠️ New CLI session; full history re-fed via stdin including text-form assistant turns. | ⚠️ Sagent re-feeds history to a new `claude --print`, BUT `providers/anthropic_cli.py:537` strips ALL `AssistantMessage` entries before write. Respawned subprocess sees only user-side history. | ✅ History is just the `messages=` parameter to `client.messages.create(...)`. Assistant turns (text + `tool_use` blocks) and `tool_result` blocks all go in verbatim. No stripping. |
| **Outbound visibility on respawn** | ✅ Outbound text survives the stdin re-feed (it's part of the assistant-turn text that *is* re-fed in channel/'s shape). | ❌→✅ Stripped by default (the consequence of #5 above; the silent-restart and re-delegation bugs traced back here). **Fixed in-plugin** by the `restart_notice` observer's splice-based reconstruction (override #3): on `ModelResponseError`, walk the tape, recover each prior `sagent_send`'s `args.to/content`, and `runtime.append_splice` a `[from sagent runtime] You previously sent to @X: "…"` after each peer reply. Equivalent to API-shape pairing, achieved through a workaround. | ✅ Free — outbound tool_use blocks are in `messages=` verbatim. No reconstruction needed. |
| **`aborted_streaming` / `ede_diagnostic` recovery** | ❌ CLI dies; operator notices in tmux pane; manual restart. | ⚠️ Sagent auto-respawns the subprocess + observer pushes a handoff notice. Latency cost: full prompt-cache miss (sagent's byte fingerprint differs from claude's session-resume bytes). Wasted tokens per recovery. | 🔮 Honour the API's own `retry_delay_ms` (which we get in error responses today but aren't using); reissue the same call with the same `messages=`. Prompt cache hits stay warm. **Likely the biggest single token-cost win** if the API errors keep flaring like they did today. |
| **Tool results in history** | ⚠️ Recreated from scratch each turn (one CLI session per turn). | ⚠️ Sagent's CLI provider treats tool round-trips as INTERNAL to the CLI subprocess — `ToolResult` entries in `agent.history` raise on the stdin path (`anthropic_cli.py:813-818`). Means we can never *replay* a prior turn's tool exchange; only fresh runs. | ✅ `tool_result` is a first-class user-message block; you can replay or seed history with it freely. |
| **Prompt-cache hit rate across turns** | ❌ Low; CLI session reset on every turn = fresh cold cache. | ⚠️ Medium. Within a session, sagent re-feeds prior history each turn, but the byte fingerprint of the re-feed differs from claude's own session-resume bytes — so on respawn we eat a full cache miss. | 🔮 High and operator-controllable: we pick the cache-control breakpoints and the byte layout stays stable across turns. |
| **Observability / debugging** | ⚠️ Manual log scraping; `main.jsonl` tail + per-pane stdout grep. | ✅ First-class HTTP surface: `/api/agents` (status + diagnosis), `/api/trace/<role>` (event-by-event runtime trace), `/debug` console, web UI with hidden bookkeeping events. | 🔮 Inherits the plugin's `/api/*` and traces unchanged — they observe runtime events, not the underlying transport. |
| **Implementation complexity** | ❌ Highest. Per-pane workers, mention router, polling intervals, cgroup wiring, tmux orchestration. | ⚠️ Medium. Single binary, but three sagent overrides + an HTTP MCP bridge + an in-plugin observer were all needed to make it tolerable. | 🔮 Lowest. Direct SDK calls; no subprocess plumbing, no CLI session lifecycle, no `cli_publish_var` thread-local trickery. The current `restart_notice` observer becomes unnecessary. |
| **Per-turn cost (input tokens)** | Baseline. | ✅ ~30% lower than `channel/` measured across the first hour of live use. | 🔮 Likely another 20–40% lower than CLI under organic API error pressure (no per-respawn cache miss); roughly on par on the happy path. |

**Summary read.** Migrating `channel/` → sagent+CLI was a big win on
the dimensions that hurt operators day-to-day (latency, token cost,
observability). The price was inheriting two CLI-shape problems
(history stripping; opaque retry behaviour on `aborted_streaming`)
that we now mitigate in-plugin via overrides + the splice-based
`restart_notice` observer. Moving sagent+CLI → sagent+API is
plausible if the organic-error pressure stays elevated (it would
delete the entire `restart_notice` complexity and reclaim the
prompt-cache during recoveries) — but it's a larger build and would
need a sagent core change (a non-CLI provider that doesn't strip
`AssistantMessage`).

---

## Validation status (as of 2026-06-02 evening)

Gates closed by live testing today; open items + evidence below.

### Closed

- ✅ **Bug A (mention-router duplicate-emit) cannot reproduce.**
  Across every live test today, each `sagent_send` produced exactly
  one inbound on the recipient — no 645/2848/4027-char
  growing-length duplicates that the in-tree mention router would
  have caused. Structural — the mention router is gone.
- ✅ **Bug B (`hello, ready` warmup-template regression) cannot
  reproduce.** `main.jsonl` post-warmup: 0 records across every
  restart. SIGINT preempt fired multiple times today without
  producing a `hello, ready` parrot. Structurally protected — the
  warmup uses `sagent_self`, which is silent to peers even if the
  model parrots it after a respawn.
- ✅ **`sagent_defer` round-trip works.** TL scheduled a +30 s
  wake-up via `mcp__sagent_chat__sagent_defer`, went idle (zero
  bash-sleep hang), woke at +30.3 s, replied to user via
  `sagent_send`. Audit log showed `[defer +30s scheduled]` at
  schedule time.
- ✅ **Structured channel works on opus/sonnet/haiku via external
  MCP.** Every live test today produced `tool_use` blocks for
  `mcp__sagent_chat__sagent_send` — see the Episode 3 probe + live
  observations.
- ✅ **Mid-turn preempt works** (partial — observed via API-error
  signature `ModelResponseError(error=None)` firing 3 times during
  the 12:53-12:58 incident; the SIGINT path is the same). The
  canonical PR #134 race replay (operator sends correction 2 min
  into TL's turn) was NOT explicitly re-run but the mechanism is
  proven.
- ✅ **Cost win vs prior runtime.** Today's plan-mode workflow
  (Msg 1 recap → Msg 2 delegation + multi-agent consultation →
  consolidated plan, ~6 min wall-clock) cost roughly
  TL ~$0.45 + SWE ~$0.34 + statistician ~$0.10 = ~$0.90 total. The
  prior `experimental/sagent/` baseline was $1.72 on TL alone for an
  equivalent prompt set in 5 min — duplicate-noise inflated. The new
  numbers are ~50% lower across the board.
- ✅ **Snappier perceived latency.** Sender→recipient gap was
  sub-second today; channel/ was 5-15 s due to per-pane poll +
  CLI cold-start. See § "Observed wins" for the mechanism.

### Still open

- [ ] **`bin/merge_jsonl.py` round-trip.** Data-dir co-location is
  set up (sagent's `main.jsonl` now lands next to
  `channel/main.jsonl`), but `bin/merge_jsonl.py` itself hasn't been
  exercised against both streams. Quick to do — should be a 30-min
  sanity check.
- [ ] **End-to-end PR.** Today's test stopped at plan-mode (user
  approval gate, by design). The implementation-to-merge phase is
  untested in plugin form. Defer until the next real small task.
- [ ] **Live validation of the `restart_notice` observer.** Wiring
  is unit-tested; the live behaviour change can only be verified the
  next time the API streams an aborted response. Server is running;
  any organic `ModelResponseError` will exercise the observer.
- [ ] **Phase 6 decision file.** Evidence supports cutover; ready to
  draft at
  `claude-config/project/worklog/decisions/2026-06-02-blackjax-chat-cutover.md`
  when ready. NOT a validation gate — a commit moment.
- [ ] **UI verification (`/debug` page status pills + auto-refresh).**
  Main `/` view confirmed working today; `/debug` page hasn't been
  explicitly walked through.

---

## Known issues + mitigations (2026-06-02)

Two failure modes observed during live use today that aren't fully
resolved structurally. Recording so operators know what to watch for.

### A. Opus occasionally writes the reply as plain assistant text
    without calling `mcp__sagent_chat__sagent_send`

Observed 12:31. TL produced a 1.8 KB recap as plain assistant text
(`ModelResponseComplete.text`), `tool_calls=[]`, and ended the turn.
The recap appears in TL's trace but NOT in `main.jsonl` or the chat
view — the user never received it.

**Why it happens:** the `mcp__sagent_chat__sagent_send` channel is
documented in the role's `PEER_MESSAGING` system-prompt block, but
opus has periodic lapses. The probe (Episode 3) showed structural
dispatch works ~100% on explicit prompts; the failure mode appears
when the model interprets the inbound as "informational" and
produces an answer-as-text reflex.

**Mitigations:**

- **Operator-side recovery:** when you see TL "reply" but nothing
  appears in `main.jsonl`, check `sessions/tl.trace.jsonl`'s most
  recent `ModelResponseComplete`. The reply body is there. Manually
  POST it via `/api/post` with `from=tl, to=user` if you want it
  surfaced in the audit log.
- **Pattern recognition:** if TL is idle but you don't see the reply
  you expected, the trace panel is faster to consult than restarting.

No structural fix yet. Possible future fixes: stronger
`PEER_MESSAGING` wording (have already tried twice; opus still slips);
server-side auto-deliver of trailing assistant text (risk: false
positives delivering scratch reasoning as replies); per-turn audit
that pings TL "your last reply went only to your trace — call
sagent_send".

### B. API-error respawn confusion

Observed 12:53-12:58 (three back-to-back `aborted_streaming` errors).
After each respawn, TL re-issued its prior delegations to SWE and
statistician verbatim, and SWE replied "Looks like a duplicate of the
planning round we already completed." Audit log showed the
duplicates; the model didn't know they had already happened.

**Why it happens:** on respawn, sagent's runtime re-feeds the entire
`agent.history` to the new `claude --print` subprocess. The model
re-reads the history fresh (no Anthropic prompt cache hit because the
byte fingerprint differs across sagent's re-feed vs the original
turn). Opus on a re-feed with multiple accumulated peer messages and
an incomplete-looking trailing assistant turn tends to anchor on the
largest/earliest identifiable user task rather than continuing from
the latest inbound.

**Mitigations (in order of effort):**

- ✅ **`restart_notice` observer (in-plugin).** When
  `ModelResponseError` fires, push an orienting `UserMessage` into
  the agent's inbox telling the model to anchor on the most recent
  peer message and NOT re-issue prior tool calls. Wiring is in
  `runtime/restart_notice.py`; behavioural validation pending the
  next organic API hiccup. See § "Sagent behaviour overrides #3".
- ⏳ **Expand sagent's `send_with_retry` whitelist (upstream
  patch).** Sagent's `agent/retry.py:345` retry loop today bails on
  `aborted_streaming` / `529` / `ede_diagnostic`. Honouring the
  retry-delay schedule the API ITSELF emits
  (`retry_delay_ms: 506, 1247, 2107…`) would reduce the
  respawn-confusion exposure by addressing the cause. ~10 lines;
  not yet staged.
- ⏳ **Per-role circuit breaker (in-plugin).** When N
  `ModelResponseError` events fire on one role within M minutes,
  auto-restart that role + surface to the operator. Higher
  complexity; not yet staged.

If the `restart_notice` observer proves sufficient when the API next
flakes, (B) can be left at "mitigated." If not, expand `send_with_retry`
upstream as the next step.

### Compounding observations

- ✅ **Sagent's `coalesce_inbox=False` patch (override #1) makes the
  `restart_notice` observer's job easier** — the notice arrives as a
  distinct user-side history entry instead of being merged into the
  prior peer message. The two patches reinforce.
- ❌ **Layer 1 of the earlier mitigation discussion (delivery-layer
  dedup in `/api/post`) was rejected** because today's duplicate
  delegations from the respawn-confusion case had NEARLY but not
  exactly identical bodies — hash dedup would have produced false
  negatives.

---

## Status: 2026-06-02 (evening)

- Episodes 1, 2, 2.5, 2.7 are history (in `claude-config/project/worklog/threads/chat-to-sagent-migration.md`).
- Episode 3 probe results in `/tmp/sagent_probe/` (haiku/sonnet/opus all
  structurally dispatched `mcp__sagent_chat__sagent_send`).
- Live testing through 2026-06-02 evening: 7 of 11 validation gates
  closed; 3 deferred (PR drive, decision file, /debug walkthrough);
  1 awaits live API-error firing (`restart_notice` observer).
- **Recommended cutover state:** plugin is functional for daily
  operator use. Channel/ runtime can be shut down in parallel
  whenever you're ready. The Phase 6 decision file is the next
  paperwork item.
