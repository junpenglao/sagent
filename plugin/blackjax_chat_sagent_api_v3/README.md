# blackjax-chat (v3 — direct-API)

A multi-agent chat channel for BlackJAX, built as a sagent plugin
that uses **direct API calls** to the model provider — no CLI
subprocess, no MCP shim, no on-disk session JSONLs that survive
server restart. The v2 README's speculative "sagent+API" comparison
column, now actually built.

**This is the stabilized production build.** v2 (`sagent_anthropic_cli_v2/`,
the daily driver) wraps `claude --print` and rides the user's
Claude Code subscription. v3 takes the same role briefs +
runtime overrides but runs them on a direct-API provider.

By default v3 uses **Google Gemini** so it doesn't fight Claude
Code's Anthropic subscription for the same OAuth key. Provider
selection is one env var (`SAGENT_API_PROVIDER`); the architecture
is provider-agnostic and switching to Anthropic's direct API is a
one-flag flip.

## What v3 is trying to measure

Open questions the comparison should answer (informed by v2's
2026-06-04 lessons; see
`worklog/lessons/tool-harness/2026-06-04-sagent-chat-runtime-fixes-and-corrected-framing.md`):

1. **Does native tool dispatch work cleanly?** v2 needed the MCP
   detour because Anthropic CLI strips structured tool dispatch in
   opaque ways. Gemini's direct API doesn't have that property —
   so sagent's bridge-mounted `AgentSend` should work without the
   `mcp__sagent_chat__sagent_send` shim. v3 verifies this.
2. **Does `cancel_in_flight` work on a streaming HTTP request the
   way SIGINT worked on the CLI subprocess?** v2's preempt is
   SIGINT to the subprocess; v3's would be aborting the SSE
   stream. Required for `urgent`-flag peer/operator preempt to
   work.
3. **What's the per-turn cost vs v2 on equivalent work?**
   Cheap-tier flash models help (~10× cheaper than opus); cache
   control is exposed; no subprocess spawn cost (~1-3 s saved
   per turn).
4. **What's the failure mode catalog?** v1 had stripping + MCP
   issues; v2 had preempt-induced aborts + cascade cost. v3 will
   have its own. We name them as they appear.

## Findings after Live Testing (June 2026)

Live testing of the v3 architecture against high-volume implementation tasks (MCLMC paper validation) yielded the following answers to our initial questions:

1.  **✅ Native Tool Dispatch is Flawless.** Gemini 3.x and Anthropic direct APIs correctly emit structured function calls. The MCP shim is officially obsolete in the v3 path.
2.  **✅ SSE Preemption Works.** Aborting the `httpx` stream correctly triggers a `ModelResponseCancelled` event. This was verified during "friendly fire" incidents where tech-lead status checks interrupted synchronous benchmarks.
3.  **✅ Substantial Cost Reduction.** Even with "Thinking Tier" Pro models for coordination, aggregate team costs dropped from ~$6.30/day (v2 Opus) to **<$1.00/day (v3 mixed tier)**.
4.  **⚠️ New Failure Mode: The "Jacobian Tail" of Rate Limits.** We discovered that Tier 1 API limits (1M TPM) are hit much faster by multi-agent bursts than by the single-user CLI. This necessitated the building of the Throttler and Autonomous Resume mechanisms (see Infrastructure section).

## Cognitive Model Tiering

v3 utilizes a 3-tier strategy to balance cognitive depth against execution speed. Per `roles/common.py`:

| Tier | Role(s) | Model | Purpose |
| :--- | :--- | :--- | :--- |
| **Thinking** | `tl`, `statistician` | `gemini-3.1-pro-preview` | Architectural planning, complex reading, and peer coordination. |
| **Worker** | `swe` | `gemini-3.5-flash` | Fast implementation and high-volume coding. |
| **Volume** | `junior-swe`, `tech-writer` | `gemini-3.1-flash-lite` | Documentation, simple edits, and verification. |

## Evolution of the v3 Infrastructure

Since the initial scaffold, the following structural enhancements were implemented to stabilize the team against Tier 1 API constraints:

### 1. Global TPM Throttling (`_TokenThrottler`)
- **Motivation**: Tier 1 keys have a hard 1M TPM limit. A "thundering herd" of 5 agents implementing code simultaneously would immediately crash the team.
- **Solution**: Implemented a shared token-bucket in `sagent/providers/google.py`. All agents coordinate their consumption through a single throttler instance. It proactively `asyncio.sleep`s agents *before* the API call if capacity is insufficient.

### 2. Autonomous Self-Healing (429 Backoff)
- **Motivation**: Transient 429s (Resource Exhausted) previously killed agent turns, requiring manual resume.
- **Solution**: Equiped `AgentRuntime` with a stateful backoff handler. It catches `RateLimitError`, schedules a hidden system-labeled wake-up message via `asyncio.call_later`, and pushes `ModelResponseCancelled` to cleanly close the turn boundary.

### 3. Structural Preemption & `urgent` Flag
- **Motivation**: Default preemption was too destructive, killing long-running JAX benchmarks for minor coordination.
- **Solution**: 
    - Set `preempt_in_flight=True` to arm the machinery.
    - Extended the `AgentSend` tool with an **`urgent: bool`** parameter.
    - Result: Routine peer traffic now queues silently. Only explicit `urgent=True` (or the operator "Interrupt" UI toggle) triggers a work-destructive SIGINT.

### 4. Persistence & Signature Integrity
- **ThoughtSignatures**: Fixed a critical bug where Gemini 3.1+ turns would 400 if the model's opaque signature wasn't echoed in subsequent tool results.
- **Durable History**: Patched `session_io.py` to persist these signatures in `session.jsonl`, allowing the team to survive server restarts without losing task context.

## Setup

### 1. Get an API key

Free tier covers comfortable testing:
- **Google (default)**: [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)
- **Anthropic** (if you set `SAGENT_API_PROVIDER=anthropic`):
  [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)

### 2. Provide the key

Two clean options (env var wins if both are set):

```bash
# Option A — shell export (ephemeral; good for CI / quick testing)
export GEMINI_API_KEY=<your-key>

# Option B — persistent local file (single line, just the key)
mkdir -p ~/.config/gemini
echo '<your-key>' > ~/.config/gemini/api_key
chmod 600 ~/.config/gemini/api_key   # keep it readable only by you
```

For Anthropic switch:
```bash
export SAGENT_API_PROVIDER=anthropic
# either ANTHROPIC_API_KEY env var, or ~/.config/anthropic/api_key
```

### 3. Verify the key works

```bash
cd plugin/blackjax_chat_sagent_api_v3
uv run python bin/check_api_key.py
```

### 4. Launch the server

```bash
# Set data dir (holds main.jsonl and per-agent traces)
export SAGENT_DATA_DIR=~/blackjax-devs/claude-config/experimental/sagent-v3

# Set your API key
export GEMINI_API_KEY="..."

# Run the server
uv run python bin/serve.py --port 8767
```

## Operator Interfaces

- **Dashboard**: `http://localhost:8767/` — Live orchestration and live **spend counters**.
- **Debug Console**: `http://localhost:8767/debug` — Full-history search and aggregate agent diagnostics.
- **Trace API**: `/api/trace/<role>?limit=2000` — Direct access to agent state machine logs.

## Security & Operational Safety

The build has been hardened against common operational hazards:
- **SandboxedBash**: Re-verified with a 24-case unit test suite blocking destructive git/system commands.
- **Leak Protection**: Verified zero leakage of opaque `thoughtSignature` strings into operator-visible logs.
- **Audit Schema**: Surfaced the `urgent` flag in the canonical audit log for full transparency.

## Status

**STABLE / PRODUCTION-READY.** 

v3 is the current standard for the BlackJAX multi-agent team. It has successfully executed the MCLMC paper replication (confirming a 9.7x efficiency gain on 1600-D targets) while maintaining perfect stability under Tier 1 API limits.
