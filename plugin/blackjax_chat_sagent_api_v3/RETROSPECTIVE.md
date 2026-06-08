# BlackJAX Multi-Agent Chat — v1 → v2 → v3 Retrospective

A six-day arc (2026-06-02 → 2026-06-08) building a multi-agent chat substrate
for the BlackJAX team, in three structural iterations. Each version is the
same plugin shape — five agents (TL / SWE / junior-SWE / statistician /
tech-writer), web UI, audit log, role briefs — but the model-invocation
plumbing changes.

The arc is honest engineering history: v1 patched around a flaw in its
own substrate; v2 sidestepped that flaw by changing the substrate; v3
swapped the substrate entirely. Each step had a forcing function — what
that was, and how we discovered it, is the point of this document.

---

## Artifacts

**Code (public)**

- v3 plugin: <https://github.com/junpenglao/sagent/tree/feat/v3-api-experiment/plugin/blackjax_chat_sagent_api_v3>
- v3 README (setup + cost tiering + infrastructure): [`README.md`](README.md)
- v2 plugin (reference): <https://github.com/junpenglao/sagent/tree/feat/v3-api-experiment/plugin/sagent_anthropic_cli_v2>
- v1 plugin (frozen reference): <https://github.com/junpenglao/sagent/tree/feat/v3-api-experiment/plugin/sagent_anthropic_cli_v1>
- Upstream sagent runtime: <https://github.com/rekursiv-ai/sagent>

**Live test output**

- Tuningfork MCLMC paper validation branch: <https://github.com/blackjax-devs/tuningfork/tree/mclmc-paper-validation>
- Math report (11.1× total-efficiency win for MCLMC vs NUTS on 1600-D LGCP): <https://github.com/blackjax-devs/tuningfork/blob/mclmc-paper-validation/tuningfork/mclmc_baseline_eval/README.md>

---

## Comparison: `channel/` (tmux) vs sagent+CLI (v1/v2) vs sagent+API (v3)

The middle two columns trace the v1 → v2 evolution. The rightmost is v3,
which is now the production substrate. The leftmost column is what we
migrated away from before v1 even existed.

| Axis | **`channel/`** (tmux, pre-v1) | **v1** sagent+CLI (re-feed) | **v2** sagent+CLI (session-resume) | **v3** sagent+API (direct) |
|---|---|---|---|---|
| **Process model** | One Python worker per agent per tmux pane per systemd cgroup | Single process, asyncio task per agent; one `claude --print` subprocess per turn | Same as v1, but subprocess spawned with `--resume <uuid>` | Single process; no CLI subprocess at all |
| **Cross-agent latency** | 5–15 s (poll cycle + cold CLI start) | Sub-second (in-process inbox + per-turn fresh subprocess) | Same as v1 | Sub-second, no subprocess to wait on |
| **History ownership** | `--resume <session_id>`; `claude` owns the JSONL on disk | Sagent re-feeds `agent.history` via stdin every respawn; CLI provider strips `AssistantMessage` entries → needs `restart_notice` observer to reconstruct outbounds | `claude` owns the on-disk JSONL at `~/.claude/projects/-<cwd>/<uuid>.jsonl`; sagent stops feeding history | Sagent owns the tape; no on-disk session JSONL outside sagent's own format |
| **Peer messaging** | Mention router polls; no inter-agent runtime | Per-agent MCP subprocess + HTTP loopback bridge (`/api/post`) — the CLI subprocess can't see sagent's bridge-mounted tools directly, so MCP is the workaround | Same as v1 | **Bridge-mounted `AgentSend`** directly — no MCP server, no per-agent subprocess |
| **Provider lock-in** | Anthropic-only (CLI) | Anthropic-only (CLI) | Anthropic-only (CLI) | **Provider-agnostic** — `SAGENT_API_PROVIDER=google` (default) or `anthropic`; one env-var flip |
| **Mid-turn cancel** | `kill -9`; no clean shutdown | SIGINT to subprocess; every interrupt pollutes history with synthetic `[Error: …]` UserMessages | SIGINT, but from 2026-06-04 **`urgent`-flag-gated**: routine peer FYIs no longer preempt (72 % of pre-fix preempts were routine status updates) | Native — close the SSE stream; same `urgent`-flag semantics, no subprocess to kill |
| **`aborted_streaming` recovery** | No respawn logic — each `claude -p` was one-shot | `restart_notice` observer reconstructed prior outbounds from a per-agent `outbound_log` (the observer's bug-shape unit tests passed only because they seeded synthetic ToolCall entries the provider never actually produced) | **In-place retry** via `send_with_retry` (commit `ac287d1`); transient errors raise `AnthropicCLIRetryableError` and stay inside the retry budget; cache stays warm | Autonomous 429 backoff-resume in sagent core; `RateLimitError` schedules a hidden self-ping via `asyncio.call_later` |
| **Prompt-cache hit rate** | High (`--resume` produces byte-identical prefixes) | Low (history re-feed breaks the cache prefix on every respawn) | High — ~40 k cache reads observed on resumed turns | High and operator-controllable via explicit cache markers |
| **Observability** | Manual log scraping | `/api/agents`, `/api/trace/<role>`, `/debug` console, web UI | Same; `ToolLabel` events surfaced from stream-json blocks | Same plus live per-agent **spend counter** in the member panel (cost telemetry computed from trace events) |
| **Implementation complexity (plugin LOC)** | Per-pane workers, mention router, polling, cgroup wiring | ~1.5 k LOC; full MCP plumbing; `restart_notice` observer | ~1.5 k LOC (observer deleted, retry/urgent added) | ~1.3 k LOC custom; `serve.py` ~270 lines (vs v2's 1039) — MCP server, suppression sentinel, warmup-MCP-priming all gone |
| **Survives `serve.py` restart** | Per-pane systemd units; killing one didn't lose its session | Claude session JSONLs survive on disk; next boot `--resume`s | Same as v1 (and the recovery actually works since the observer doesn't have to lie) | Sagent's own session tape (`session.jsonl`) survives, including persisted `thought_signature` blocks for Gemini 3.x |
| **Native tool config (`gh`, `git`, ssh)** | Per-pane shell inherits operator env | Hermetic per-spawn tmpdir; tools mounted via sagent's HTTP bridge so handler ran in the server process with the operator's real `$HOME` | Session-persistent + single-account mode inherits operator's real `$HOME` so `~/.config/gh/`, `~/.gitconfig`, ssh keys all visible to claude's native tools | N/A — no subprocess tools; sagent's `Bash`/`Edit`/`Read`/`Write` run in-process |
| **Coordination interrupt model** | Operator-only ingress; peers can't interrupt | All peer messages preempt (default) → cascade pattern: TL accumulated 50+ synthetic `[Error: …]` UserMessages in a day | **Per-message `urgent: bool`** (added 2026-06-04); default False buffers, True preempts; operator opts in via UI toggle or Ctrl+Enter | Same primitive, now native to sagent core (`AgentSend(urgent=True)` parameter); audit log surfaces the flag |
| **Per-day cost (5 agents, ~6 hours, MCLMC-volume workload)** | Lower in absolute terms (smaller sessions, less work) | ~$6/day (opus-TL dominant) with the cascade penalty on top | ~$6 steady-state; **$10–15 cold-cache spikes** on opus-TL when the 5-min cache TTL expires | Materially below v2 even with `gemini-3.1-pro-preview` on TL + statistician (the in-UI spend counter is the anchor — exact ratio is workload-dependent) |
| **Status** | Migrated away from before this arc began | Frozen | Recommended for Anthropic-only Claude-Code workflows | Production-ready; current standard for the BlackJAX multi-agent team |

**Summary.** v1 patched around a CLI-stripping bug with an observer that
turned out to be theater. v2 deleted the observer and let `claude` own
the on-disk session via `--resume`; the 2026-06-04 fix stack (in-place
retry + urgent-gated preempt + per-entry advance) closed the
preempt-cascade pain that had been the dominant operational cost. v3
removes the CLI subprocess entirely, going direct-API with provider
abstraction — which also makes Gemini a one-env-var swap and unlocks
a roughly 10× cost reduction at the cheap tier.

The v3 substrate has been validated under real load: the BlackJAX team
used it to run the MCLMC paper replication (1600-D Log-Gaussian Cox
Process), confirming an **11.1× total-efficiency win** for unadjusted
MCLMC over NUTS on that geometry. See the math report linked above.

---

## Decision-point timeline

The dates below are the moment each decision was forced, not the moment
the work landed.

**2026-06-02 (start of arc).** The agent team was already running on a
tmux-based `channel/` system: one Python worker per pane, polling for
`@<role>` mentions, no inter-agent runtime. Migration to sagent had
been planned and the first phases (asyncio runtime, structured tool
dispatch, observability) were in place. The forcing function was load:
the first heavy phase-load use surfaced runtime reliability issues
that needed a real substrate, not polling.

**v1 = sagent + Anthropic CLI, history re-feed.** Claude Code's CLI
doesn't natively know about sagent's bridge-mounted tools. To let agents
call each other, we mounted a peer-messaging MCP server (the
`mcp__sagent_chat__sagent_send` tool) that the CLI subprocess could
discover. The MCP server's handler bridged back to sagent's runtime via
HTTP loopback. **The MCP shim exists because the CLI is a black box to
sagent.**

**2026-06-02 evening — v1's observer is revealed as theater.** v1 had
a `restart_notice` observer that walked `runtime.tape` looking for
outbound `sagent_send` calls in `AssistantMessage.tool_calls`. The
unit tests passed. But in production the CLI provider always returns
`tool_calls=()` because the MCP roundtrip runs opaquely inside
`claude --print`. The observer was reconstructing from synthetic
ToolCall entries that real runs never produce. A revised observer
pulled outbounds from a separate `outbound_log` populated by `/api/post`,
but it was still a reconstruction.

**2026-06-03 morning — v2 pivot.** Decision: stop re-feeding history at
all. Give each agent a stable `UUIDv5` session ID and spawn the CLI with
`--session-id <uuid>` (first turn) or `--resume <uuid>` (subsequent).
**`claude` itself owns the on-disk transcript** — assistant turns,
`tool_use` blocks, `tool_result` blocks — at
`~/.claude/projects/-<encoded-cwd>/<uuid>.jsonl`. Sagent stops feeding
history. The `restart_notice` observer is deleted because the problem
it papered over no longer exists. Prompt-cache hit rate goes from
~zero to ~40 k cache reads per resumed turn.

**2026-06-04 — v2 fix stack day.** A day of live operator use surfaced
a different cluster: every peer-message arrival mid-turn killed the
recipient's in-flight subprocess and produced a synthetic `[Error: …]`
UserMessage. TL accumulated 50+ such messages in a single day. The
discovery, validated by 0-input-token aborts the API never saw, was
that **most of what we'd called "Anthropic stream instability" was
actually our own SIGINT preempt firing on routine peer FYIs.** Three
layered fixes: per-entry advance (`8dc81f1`), in-place retry inside
the provider's retry budget (`ac287d1`), and urgent-gated preempt at
the runtime level (`774eb6b` / `c1b8fa9`). 72 % of pre-fix preempts
turned out to be routine status updates that shouldn't have
interrupted; eliminating them saved ~$0.30 of opus compute per
event plus the cascade penalty.

**2026-06-04 evening — v3 scaffold begins.** v2's fix stack closed the
dominant pain, but two structural limits remained: Anthropic-only lock-in
(the CLI is the moat) and cold-cache cost spikes ($10–15 every time
the 5-min cache TTL expired on opus-TL). The decision to start v3 was
not a v2 indictment — it was a separate experiment: *can we run the
same role briefs + runtime overrides on a direct-API provider?* Gemini
chosen as the default to avoid fighting Claude Code's Anthropic OAuth
subscription for the same key.

**2026-06-05 — v3 live testing.** First-day findings:
gemini-2.5-flash failed natural-language coordination at TL (couldn't
diagnose a tripwire test pattern under doubt-pressure); statistician
lacked the statistical depth to draft the LGCP NumPyro model
unaided. Per-role tier upgrades. The team ran the MCLMC paper
replication end-to-end and confirmed the 11.1× total-efficiency win.
First failure mode of the new substrate: Tier 1 Gemini's 1M-TPM cap
gets saturated by multi-agent bursts much faster than by single-user
CLI traffic. Built the `_TokenThrottler` (proactive token bucket
shared across roles) and **Autonomous Backoff Resume** (catch
`RateLimitError`, schedule a hidden self-ping via `asyncio.call_later`,
push `ModelResponseCancelled` to cleanly close the turn boundary).

**2026-06-05 16:10 — `rm -rf .venv` incident.** SWE on gemini-2.5-flash,
debugging test timeouts, ran `rm -rf .venv` to "fix" a stuck install.
Broke the pinned blackjax. Then spent 30 minutes "fixing" the resulting
tripwire failures by editing the test file instead of restoring the venv.
Net cost ~$1.50 in cascading flail. Built `SandboxedBash` with a
rationale-bearing deny-list: `rm -rf` allowed only on `/tmp/`; `git push`
to `main` / `master` blocked; `git push --force`, `git reset --hard`,
`git clean -fd`, `sudo`, fork-bomb patterns, `dd of=/dev/*` all blocked.
22-case unit test suite to prevent regression.

**2026-06-06 — Gemini 3.1+ thoughtSignature.** Upgrading TL +
statistician to `gemini-3.1-pro-preview` for cognitive depth failed
immediately: Gemini 3.x returns `400 INVALID_ARGUMENT: Function call
is missing a thought_signature` if the opaque signature from a
`functionCall` part isn't echoed back exactly in subsequent
tool-use continuations. Fix: extend `ToolCall` and `AssistantMessage`
to carry `thought_signature: str`, capture from the stream in
`providers/google.py`, persist via `session_io.py` so the chain
survives server restart.

**2026-06-06 audit + remediation.** Following live testing, a
systematic audit (A1–A10) was performed. Five real defects surfaced
(`_COSTS` placed mid-import, duplicated JS function, missing
`ModelResponseCancelled` renderer in the main index, undocumented
rate table, empty test placeholder) and were fixed within an hour.
Spend counter shipped — exposes `total_in_tokens`, `total_out_tokens`,
`total_cost_usd` per agent via `/api/agents` with a 4 s UI poll.

**2026-06-08 — wrap.** Final HTML structure fix; plugin README rewritten
to reflect the actual model tiering + verified findings; minor numerical
inconsistencies surfaced in light review (9.7× vs 11.1× wording,
24-case vs 22-case test count) reconciled across README and worklog.

---

## Lessons that generalise

1. **Substrate, not patches, when the patch's premise is wrong.** v1's
   observer was correct against synthetic ToolCall fixtures and wrong
   against every real run, because the real CLI never produces what
   the fixtures fed. The lesson isn't "write better tests"; it's
   "when the patch you're writing depends on a property your data
   doesn't actually have, the data is the bug." v1 → v2 was the only
   honest move.

2. **Self-inflicted instability looks identical to external instability,
   from inside the abort.** v2's "Anthropic stream instability" was our
   own SIGINT preempt. The diagnosis came from a single signal: the
   provider's logs showed 0 input tokens for the aborted turns, meaning
   the API never saw the request. If we'd trusted the metric we hand
   already, we'd have caught it days earlier. (See
   `worklog/lessons/tool-harness/2026-06-04-sagent-chat-runtime-fixes-and-corrected-framing.md`
   for the corrected diagnosis + validation evidence.)

3. **"Proactive > reactive" pays for itself fast under tier limits.**
   v3's TPM throttler sleeps agents *before* the API call. Combined with
   autonomous resume, hard 429s became transient pauses without operator
   intervention. The reactive-only design (retry on 429) was tried
   first and produced wedged-runtime states because the retry handler
   didn't push a turn-boundary event.

4. **Sandboxes pay for themselves in a single incident.** The 16:10
   `rm -rf .venv` event cost about $1.50 in flailing. `SandboxedBash`
   took about 90 minutes to build and test. Both wall-clock and dollar
   ratios are in the sandbox's favour after a single recurrence.

5. **Strict canonical channels beat synthetic fallbacks.** v3 ran for a
   day with a "synthesise a `to=user` audit record from any non-empty
   assistant text" fallback. It correctly captured silent-model
   failures, but it also captured TL's post-action narration ("Thank
   you for the question, I've forwarded it to @swe…") and leaked it to
   the chat. The operator's expectation — silence means the model
   didn't intend to message them — turned out to be the correct
   invariant. Removed the fallback; tightened the role brief.

---

## Related & archived material

- **Archived worklog thread** (full V3 stabilization narrative — TPM throttler, autonomous resume, thoughtSignature plumbing, statistician promotion, audit log): [`worklog/threads/_archive/sagent-v3-gemini-deep-testing.md`](https://github.com/blackjax-devs/claude-config/blob/main/project/worklog/threads/_archive/sagent-v3-gemini-deep-testing.md) (in the internal agent-config repo).
- **MCLMC paper validation thread**: [`worklog/threads/mclmc-paper-validation.md`](https://github.com/blackjax-devs/claude-config/blob/main/project/worklog/threads/mclmc-paper-validation.md) — the science work that exercised the v3 substrate under real load.
- **Boundary-tail pathology lesson** (a theoretical insight the team derived during the MCLMC run — why unadjusted MCLMC's step size collapses on bounded scale priors): [`worklog/lessons/case-studies/mclmc/2026-06-06-boundary-tail-pathology.md`](https://github.com/blackjax-devs/claude-config/blob/main/project/worklog/lessons/case-studies/mclmc/2026-06-06-boundary-tail-pathology.md).
- **v2 fix stack diagnosis** (the 2026-06-04 lesson on misattributed instability): `worklog/lessons/tool-harness/2026-06-04-sagent-chat-runtime-fixes-and-corrected-framing.md`.
- **First-load findings** (v3 pre-stabilization probe): [`V3_FIRST_LOAD_FINDINGS.md`](V3_FIRST_LOAD_FINDINGS.md).
