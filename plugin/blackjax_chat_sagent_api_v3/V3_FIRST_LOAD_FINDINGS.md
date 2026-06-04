# v3 first-load findings (pre-operator-live-test, 2026-06-04)

Smoke-test pass + research findings before the operator's live test.
Architecture is structurally validated; two model-level behaviors
worth knowing going in.

## ✅ What works

| Area | Result |
|---|---|
| Boot time (5-agent warmup) | **~5 s** (vs v2's 30-60 s typical). All 5 agents reach `AgentIdle` in under 6 s. |
| `AgentSend` structural dispatch | ✅ Gemini API produces clean `tool_use` blocks, not prose. The MCP shim that v1/v2 needed for Anthropic CLI is **not needed** in v3. |
| Multi-peer routing | ✅ TL → SWE + TL → statistician with both peers responding cleanly (~3 s round-trip per leg). Verified in audit log. |
| Session persistence | ✅ `Agent(session_dir=...)` writes `session.jsonl` per role; on server restart, `load_session()` + `agent.resume()` replays the tape. Verified: TL remembered `PURPLE-MOOSE-7` after kill + restart. |
| Canonical peer labels | ✅ `agent._persistent = True` disables the `_1` auto-suffix. Peers see `source='tl'` not `'tl_1'`. |
| Audit log emission | ✅ `runtime/audit_writer.py` observer writes peer traffic to `main.jsonl` so the web UI sees it. Replaces v2's `delivery.py`. |
| Web UI | ✅ Serves at `/`. Has the urgent toggle button + Ctrl+Enter shortcut from v2 (inherited verbatim). `/api/agents` + `/api/messages` + `/api/members` + `/api/post` + `/api/restart` + `/api/trace/<role>` all respond. |
| Urgent flag posting | ✅ `/api/post` with `{urgent: true}` is accepted; threads through to `UserMessage(urgent=...)`. |

## ⚠️ Findings the operator should know

### Finding 1: gemini-2.5-flash fails natural-language coordination; gemini-2.5-pro works

**Comparison run on identical prompt** (2026-06-04 15:42-15:46):

| Model | Behavior |
|---|---|
| `gemini-2.5-flash` (TL on $0.30/Mtok in) | ❌ `text='' tool_calls=[]` — silent failure |
| `gemini-2.5-pro` (~4× the cost on input) | ✅ Full chain: dispatched to swe + statistician + status to user + summary |

The empty-response is genuinely a **model-tier limit**, not a prompt
issue. Same prompt, same role brief, same runtime — only the model
changed.

**Recommendation for the operator**: switch TL to
`gemini-2.5-pro` for the live test. Single-line change in
`roles/common.py`:

```python
"google": (
    "gemini-2.5-pro",         # TL (was: gemini-2.5-flash — empty-responded)
    "gemini-2.5-flash-lite",  # default — keep
),
```

**Cost implication**: TL on gemini-2.5-pro costs ~$1.25/Mtok input,
$10/Mtok output (vs flash's $0.30/$2.50). For a typical coordination
turn (~5-10K input tokens × $1.25/M = $0.006-0.012, plus output
~500 tokens × $10/M = $0.005), each TL turn lands around $0.01-0.02.
Multiplied by maybe 100 turns/day = $1-2/day on TL.

The four non-TL agents stay on `gemini-2.5-flash-lite` ($0.10/Mtok
in) — their tasks are simpler (acknowledgments, file edits, test
runs) and don't suffer the natural-language-coordination failure
mode.

Even at the upgraded TL tier, v3 total day cost should land
~$1-3/day vs v2's $6-15/day. The cheap-tier experiment did its
job (showed where flash breaks); the right config for actual use
is **pro for TL, flash-lite for the rest**.

### Finding 2: TL responses to operator sometimes come as text only

When TL replies to the operator, it sometimes returns the answer as
assistant text (`text='PURPLE-MOOSE-7'`) instead of calling
`AgentSend(to='user', content='PURPLE-MOOSE-7')`. The audit log /
web UI shows only the operator's request, not TL's response.

In v2 this didn't happen because the role brief mandated the MCP
tool call and the operator chat surface was always sourced from
audit log. In v3, sagent's native `AgentSend` works the same way —
but the model needs to choose to use it.

**Same workaround as finding 1**: explicit narrow prompts make TL
use the tool. The structural fix would be either upgrading the
model or tightening the role brief.

**Operator UX impact**: until this is fixed, watch the trace panel
(`/api/trace/tl`) in addition to the main chat — TL's responses
may appear there but not in the chat.

## Worklog-driven tests against v2's failure-mode catalog

Worklog
`2026-06-03-sagent-chat-runtime-reliability-failure-modes.md` lists
8 distinct v1/v2 failure modes. Mapping them against v3:

| # | v2 failure | v3 status |
|---|---|---|
| 1 | "No such tool available" MCP races | ❌ N/A — no MCP in v3 |
| 2 | Arg-type error mis-reported as missing tool | ❌ N/A — different wire format |
| **3** | **Message loss across server restart** | ✅ **PASSED** — sent 3 tokens, SIGKILLed server, restarted, TL recalled all 3 |
| 4 | Multi-fork bootstrap collision | ⚠️ in-process; less risk but not stress-tested |
| 5 | Full history loss / peer-query recovery | Partial — `agent.resume()` works; deliberate session deletion not tested in v3 |
| **6** | **Multi-part message partial delivery** | ✅ **PASSED** — sent TL→SWE with DATA + NARRATIVE + CITATION blocks (411 chars), SWE replied "Received all three sections" intact |
| 7 | Crossed messages / re-send to override | Structurally addressed via `urgent` flag (sagent core); not pattern-tested |
| 8 | Defer-contention polling | Not tested; sagent's `AgentSend(delay=...)` is the equivalent mechanism |

**Critical gap: NO compactor wired in `build_agent`.**
`sagent.compaction.summary.SummaryCompactor` exists but isn't passed
to the Agent constructor. v2 hit `blocking_limit` at ~1.5 M tokens
on opus's 1 M context window; Gemini-2.5-flash and flash-lite also
have 1 M context.

For day-1 testing this won't bite (sessions are small). For
sustained use the operator should wire SummaryCompactor before
crossing ~700 K tokens. ~5-line change in `build_agent`:

```python
from sagent.compaction.summary import SummaryCompactor
agent = Agent(
    ...,
    compactor=SummaryCompactor(...),  # check the constructor signature
)
```

## What's not yet tested

- **`urgent` flag on peer messages**: sagent's native `AgentSend`
  tool schema is `{to, content, delay}` — no `urgent`. Peers can't
  send urgent messages today; everything queues. Deferred per
  operator's instruction (observe first whether the lower message
  volume of v3 makes urgency unnecessary).
- **Long-running session size**: v2 hit `blocking_limit` at ~1.5M
  tokens. Gemini-2.5-flash and flash-lite both have 1M context
  window — slightly tighter than opus. Worth watching.
- **API error shapes**: zero errors hit during the smoke test. v2's
  `aborted_streaming` cascade was preempt-induced; the API path
  shouldn't have that pattern, but real failure modes (429, 503,
  timeout) are uncatalogued.
- **Cost on a realistic multi-turn coordination**: the smoke test
  spent <$0.01 across all calls. A full day of v2-equivalent
  coordination might land at $0.20-$1.00 — but that's projection,
  not measurement.

## Bootable + ready

Both branches preserved on `junpenglao/sagent`:

- `main` — clean, matches `upstream/rekursiv-ai`
- `feat/cli-session-resume` — v2 daily-driver (preserved, no v3)
- `feat/v3-api-experiment` — v3 scaffold + this commit, ready for
  live test

To launch:

```bash
cd ~/rekursiv/sagent/plugin/blackjax_chat_sagent_api_v3
# (API key already at ~/.config/gemini/api_key)
SAGENT_DATA_DIR=/path/to/v3-data \
  uv run python bin/serve.py --port 8769
# Web UI at http://127.0.0.1:8769/
```

## Test reproducibility

- `bin/probe_agentsend.py` — 2-agent probe (TL + SWE) verifying
  structural AgentSend dispatch + canonical labels + audit log
  emission. Cost: ~$0.001 per run.
- `bin/check_api_key.py` — 1-token smoke test against both
  configured models. Cost: ~$0.0001 per run.
