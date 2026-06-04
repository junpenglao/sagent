# blackjax-chat (v3 — direct-API)

A multi-agent chat channel for BlackJAX, built as a sagent plugin
that uses **direct API calls** to the model provider — no CLI
subprocess, no MCP shim, no on-disk session JSONLs that survive
server restart. The v2 README's speculative "sagent+API" comparison
column, now actually built.

**This is the comparison build.** v2 (`sagent_anthropic_cli_v2/`,
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

## Cost model (cheap-tier config, default)

Per `roles/common.py`:

| Role | Model | Input | Output | Cache read |
|---|---|---|---|---|
| **tl** | `gemini-2.5-flash-lite` | $0.10 / Mtok | $0.40 | $0.025 |
| **swe, junior-swe, statistician, tech-writer** | `gemini-1.5-flash` | $0.075 / Mtok | $0.30 | $0.01875 |

A day of multi-agent coordination at v2's volume (~6.3 K user
turns + 1.3 K assistant turns + 387 tool calls across 5 agents,
2026-06-03 baseline) should cost on the order of **$0.20-$1.00**
in v3 — vs $6.30 in v2 — primarily because the model tier is
much cheaper. (Quality tradeoff is real; v3 is the architecture
test, not the production substitute.)

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

This hits both configured models with a 1-token prompt. Costs
~$0.0001 total. Surfaces auth / connectivity / model-availability
errors here instead of during the live boot.

### 4. Launch the server

```bash
SAGENT_DATA_DIR=/path/to/data \
  uv run python bin/serve.py --port 8767
```

Web UI at `http://127.0.0.1:8767/`. The data dir holds the audit
log + per-agent trace files (same shape as v2's
`SAGENT_DATA_DIR`); reuse the v2 directory if you want main.jsonl
continuity across builds.

## What's inherited from v2 (verbatim or near-verbatim)

- **Role briefs** (`roles/*.md`): the system prompts for each
  agent. Reused as-is; PEER_MESSAGING shim handles the
  `mcp__sagent_chat__sagent_send` → `AgentSend` rename.
- **Web UI** (`web/`): provider-agnostic; talks to the same
  `/api/post` shape.
- **Sandboxed tools** (`sandboxed_tools.py`): file-edit sandbox
  for the role-specific scopes.
- **Runtime overrides**: `preempt_in_flight=True` and
  `coalesce_inbox=False` are set in `build_agent`; provider-
  agnostic at the sagent layer.
- **The `urgent` flag**: gates both peer and operator preempt;
  works identically here because it's a sagent-core feature
  (added 2026-06-04 in commits `774eb6b` / `c1b8fa9`).

## What's NEW in v3 (vs v2)

- **No `mcp_sagent/` directory.** Peer messaging routes via
  sagent's bridge-mounted `AgentSend` directly. Saves the per-
  agent stdio MCP subprocess + the HTTP loopback bridge.
- **No `--session-id` / `--resume` machinery.** The API provider
  works on `messages=` arrays; sagent owns history; no on-disk
  session JSONL outside sagent's own tape format.
- **No claude-CLI-specific argv tuning.** No `--tools ""`
  bisect, no `DISABLE_AUTO_COMPACT` env var, no HOME passthrough
  contortions. The plugin doesn't know what shape the underlying
  HTTP request takes; sagent's provider abstracts it.
- **Provider switch via one env var.** `SAGENT_API_PROVIDER=google`
  (default) or `anthropic`.

## Status

**Scaffold complete; not yet live-tested.** Files in this
directory boot agents structurally, but no live multi-agent
session has been run against the API path as of the v3 scaffold
commit. The next-step plan:

1. Run `bin/check_api_key.py` to confirm auth + model availability.
2. Boot `serve.py` and verify the web UI loads.
3. Send a single message to TL; observe whether `AgentSend` is
   structurally invoked vs prose-emitted (the v2 lesson would
   predict structurally — Gemini doesn't have CLI's strip
   pattern).
4. Multi-peer task to exercise routine peer FYIs (should NOT
   preempt) + urgent STOPs (SHOULD preempt). Validates the
   `urgent`-flag wiring at the API layer.
5. Catalog the first organic failure modes; write a
   `2026-06-XX-v3-first-load.md` lesson when patterns emerge.

`bin/serve.py` is a slim ~270-line adaptation of v2's 1039-line
serve.py with MCP plumbing, the suppression sentinel, defer/search
endpoints, and warmup-MCP-priming all removed. Same HTTP-route
contract as v2 for the operationally important endpoints
(`/api/agents`, `/api/post`, `/api/messages`, `/api/trace/<role>`,
`/api/restart`); the web UI talks to the same shapes.
