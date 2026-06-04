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

### Finding 1: gemini-2.5-flash sometimes empty-responds to natural-language coordination

**Symptom**: when TL gets a natural-language coordination request like
`"We need a quick coordination test, please send a message to @swe..."`, it
sometimes produces `text='' tool_calls=[]` in ~1 second — completely silent,
no dispatch. The operator's web UI would show only the request, no
response.

**Workaround that works**: if the prompt is explicit and narrow
(`"Call the AgentSend tool right now with arguments {to: 'swe', content: '...'}"`),
TL dispatches correctly within 3 seconds.

**Hypothesis**: cheap-tier Gemini follows narrow instructions reliably
but doesn't reliably translate higher-level coordination intent into
tool calls. Two paths to test:

1. **Upgrade TL to gemini-2.5-pro** (10× input cost: $1.25/Mtok vs
   $0.10) — might handle the natural-language case. Single-line
   change in `roles/common.py` (`MODEL_TL = "gemini-2.5-pro"`).
2. **Tweak the role brief** (`roles/tl.md`) to include explicit
   coordination examples Gemini can copy. Cheaper but needs iteration.

The PEER_MESSAGING block in `roles/common.py` already says "use
`AgentSend`, not prose" — gemini-2.5-flash just isn't following it
on the coordination path.

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
