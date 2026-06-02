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
2. Calls into a delivery helper that pushes the appropriate runtime
   event (`AgentSendMessage`, `Clear`, `UserDeferredMessage`-style
   scheduled wake-up) into the target's inbox.
3. Writes a `main.jsonl`-format audit record sender-side.

Sagent's runtime is unchanged — the plugin sits beside it as `plugin/blackjax-chat/`
in this fork. Roles, the `serve.py` HTTP+web UI, the per-agent
trace writer, and the audit-log shape are direct ports of the
`channel/` and previous `experimental/sagent/` versions. The mention
router and defer router and per-turn flag machinery are **deleted** —
the MCP server makes them redundant.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ blackjax-chat serve.py (single Python process, asyncio)             │
│                                                                     │
│ ┌─────────┐  ┌──────────┐  ┌─────────────┐  ┌──────────┐  ┌──────┐  │
│ │ tl      │  │ swe      │  │ junior-swe  │  │ statist. │  │ tech │  │
│ │ Agent   │  │ Agent    │  │ Agent       │  │ Agent    │  │ Agent│  │
│ └────┬────┘  └────┬─────┘  └─────┬───────┘  └────┬─────┘  └──┬───┘  │
│      │ HotSpare   │              │               │           │      │
│      ▼            ▼              ▼               ▼           ▼      │
│ ┌──────────────────────────────────────────────────────────────┐    │
│ │ per-agent claude --print --mcp-config <role>.json            │    │
│ │   (subprocess, SAGENT_ROLE=tl|swe|… in env)                  │    │
│ └────────────────────────┬─────────────────────────────────────┘    │
│                          │ MCP CallTool                             │
│                          ▼                                          │
│ ┌──────────────────────────────────────────────────────────────┐    │
│ │ plugin/blackjax-chat/mcp_sagent/server.py                    │    │
│ │   (one stdio server per agent, but same Python code)         │    │
│ │   sagent_send(to, content, delay?)                           │    │
│ │   sagent_defer(delay_s, body)                                │    │
│ │   sagent_self(status?, context?)                             │    │
│ └────────────────────────┬─────────────────────────────────────┘    │
│                          │ in-process delivery                      │
│                          ▼                                          │
│ ┌──────────────────────────────────────────────────────────────┐    │
│ │ delivery.py                                                  │    │
│ │   agent_registry.get(target).runtime.inbox.push_back(...)    │    │
│ │   append_record(main.jsonl, {from, to, body, ts})            │    │
│ └──────────────────────────────────────────────────────────────┘    │
│                                                                     │
│ ┌──────────────────────────────────────────────────────────────┐    │
│ │ HTTP + web UI (Starlette, port 8767)                         │    │
│ │   /        index.html (chat view)                            │    │
│ │   /debug   debug.html (agents grid + search)                 │    │
│ │   /api/{roles,agents,messages,trace,search,post,defer,restart}│   │
│ └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

The MCP server is launched **per-agent** (each agent's `claude --print`
spawns its own stdio peer via `--mcp-config`), but all instances share
the same Python module and run inside the same `serve.py` process via
asyncio. Delivery goes through the in-process `agent_registry`, so
peer messaging is a function call away — no HTTP roundtrip, no extra
service to deploy.

### What's deleted vs the previous attempt

From `claude-config/experimental/sagent/`:

- `mention_router.py` — no longer needed; structured channel works.
- `defer_router.py` — no longer needed; `sagent_defer` is structural.
- `LoggingAgentSend` wrapper in `shim.py` — replaced by direct audit
  log writes from the MCP server's tool handlers.
- `_structured_send_this_turn`, `_deferred_send_this_turn`, and the
  per-turn flag plumbing — no longer needed.
- `DEFER_VIA_PROSE` onboarding block — no longer needed.

What stays (ported with minor edits):

- `roles/*.py` and `roles/*.md` (role definitions + system prompts)
- `bin/serve.py` (HTTP + web UI surface; internal wiring simpler)
- `bin/merge_jsonl.py` (audit log union with channel/)
- `web/index.html`, `web/debug.html` (unchanged)
- `runtime/trace_writer.py` (per-agent runtime event JSONL)

---

## Running it

```bash
cd ~/rekursiv/sagent  # or wherever this plugin lives
uv run python plugin/blackjax-chat/bin/serve.py --port 8767
```

The web UI is at `http://127.0.0.1:8767/`. Open via SSH tunnel:

```bash
ssh -L 8767:127.0.0.1:8767 <host>
```

`SERVE_HOST` is forced to `127.0.0.1` and the server rejects any other
bind — no auth, loopback-only.

---

## TODO — validation gates before we declare this the cutover path

These open until the new plugin has demonstrably matched or exceeded
the `claude-config/experimental/sagent/` numbers on real work.

- [ ] **UI verification.** Open `http://127.0.0.1:8767/` after a clean
  start, confirm:
  - members sidebar renders 5 roles with status pills
  - clicking a member opens the right-side trace panel
  - sending a message via the input box reaches the targeted agent
  - `/debug` page agents grid auto-refreshes; status transitions
    (idle → working → idle) are visible in real time
- [ ] **End-to-end task in the new channel.** Drive one substantive PR
  through the chat from operator → TL → SWE → review → merge. Capture
  the audit log + per-agent traces for the worklog. The original 2026-06-02
  test prompt set ("read worklog" + "speed benchmark should run nightly")
  is a fair fixture — it exercises delegation, structured AgentSend,
  CI-wait, and a real PR (tuningfork #141 was the output last time).
- [ ] **Confirm Bug A doesn't reproduce.** TL's inbox should receive
  exactly ONE AgentSendMessage per SWE structured send. No phantom
  growing-length duplicates from a trailing-text fallback.
- [ ] **Confirm Bug B doesn't reproduce.** No `hello, ready` regressions
  after a preempt+respawn cycle. (Bug B was triggered specifically by
  the warmup-pattern parrot loop, which our new warmup doesn't
  establish; `sagent_self(status='ready')` is the bootstrap and is
  silent to peers.)
- [ ] **Confirm `sagent_defer` works.** Have TL schedule a wake-up via
  `sagent_defer(delay_s=60, body='check PR CI')`. Verify:
  - audit log shows `[defer +60s scheduled]` immediately
  - TL goes idle (no `bash sleep` hang)
  - after 60 s, TL receives the deferred body as a fresh inbound
  - TL re-evaluates and pulls CI status in the next turn
- [ ] **Confirm the mid-turn preempt still works.** Replay the PR #134
  race: send TL directive A, then a correction at T+2min while TL is
  mid-implementation. SIGINT preempt (from `feat/cli-preempt-via-sigint`,
  this fork) should fire, correction lands in TL's history, TL produces
  the corrected implementation in a single coherent turn.
- [ ] **Cost telemetry vs `experimental/sagent/` baseline.** During the
  end-to-end task, log `total_cost_usd` per role at minute granularity.
  Compare against the 2026-06-02 baseline ($1.72 on TL after 2 prompts
  in 5 min — the structural-noise duplicates were probably inflating
  this). Expect lower; if higher, investigate.
- [ ] **`bin/merge_jsonl.py` round-trip.** Run end-of-day routine
  against both `experimental/channel/main.jsonl` and this plugin's
  `main.jsonl` simultaneously; confirm the chronological union renders
  correctly in the chat-serve viewer.
- [ ] **Phase 6 decision file.** Either commit to decommissioning
  `experimental/channel/` AND `experimental/sagent/` in favor of this
  plugin, or document why we're keeping one of them and what would
  change our mind. File at
  `claude-config/project/worklog/decisions/2026-06-XX-blackjax-chat-cutover.md`.

---

## Status: 2026-06-02

- Episodes 1, 2, 2.5, 2.7 are history (in `claude-config/project/worklog/threads/chat-to-sagent-migration.md`).
- Episode 3 probe results in `/tmp/sagent_probe/` (haiku/sonnet/opus all
  structurally dispatched `mcp__sagent__sagent_send`).
- Episode 3 plugin scaffolded; no end-to-end test yet — that's the
  next gate.
