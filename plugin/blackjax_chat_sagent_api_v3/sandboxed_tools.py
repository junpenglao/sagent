"""Path-sandboxed wrappers around ``sagent.tools.Write`` and ``sagent.tools.Edit``.

The statistician role is permitted to edit experiment scripts and
diagnostic notebooks under ``tuningfork/experiments/`` only. Without
runtime enforcement, the role's prompt-level "do not edit production
code" instruction is advisory — a misjudgment costs us a production
edit. These wrappers convert the boundary into a structural property
of the tool dispatch: any attempt to write or edit a path outside the
configured sandbox root resolves immediately as a ``ToolResult`` with
``is_error=True``, without ever opening the file.

The wrappers inherit metadata (``name``, ``tool_id``,
``directive_schema``, ``summary``, etc.) from the underlying tool, so
the model sees an ordinary ``Write`` / ``Edit`` tool — no special
prompt needed. Only the validation hook in ``run`` differs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from sagent.tools.bash import Bash
from sagent.tools.core import resolve_tool_path
from sagent.tools.edit import Edit
from sagent.tools.write import Write
from sagent.types.runtime import ToolResult


def _is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or any descendant.

    Resolves both ends (follows symlinks, normalizes ``..``) so a
    sneaky ``../../etc/passwd`` argument cannot escape via path
    manipulation. ``parent`` is resolved at config time by the
    caller; ``child`` at dispatch time.
    """
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject(message: str) -> ToolResult:
    return ToolResult(call_id="", content=message, is_error=True)


class SandboxedWrite(Write):
    """``Write`` restricted to a configured root directory.

    Args:
        sandbox_root: Directory tree the tool is allowed to write
            inside. Must be an absolute path. Resolved once at
            construction.
    """

    def __init__(self, *, sandbox_root: Path | str) -> None:
        super().__init__()
        root = Path(sandbox_root)
        if not root.is_absolute():
            raise ValueError(
                f"sandbox_root must be absolute, got {sandbox_root!r}"
            )
        self._sandbox_root = root.resolve()

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        raw_path = str(args.get("file_path", ""))
        if not raw_path:
            return _reject("'file_path' is required.")
        resolved_str = resolve_tool_path(raw_path)
        target = Path(resolved_str).resolve()
        if not _is_within(target, self._sandbox_root):
            return _reject(
                f"Refused: {target} is outside the statistician sandbox "
                f"({self._sandbox_root}). Statistician may only write under "
                f"this root. Flag production-code changes to @swe via "
                f"AgentSend instead."
            )
        return await super().run(args)


class SandboxedEdit(Edit):
    """``Edit`` restricted to a configured root directory. See ``SandboxedWrite``."""

    def __init__(self, *, sandbox_root: Path | str) -> None:
        super().__init__()
        root = Path(sandbox_root)
        if not root.is_absolute():
            raise ValueError(
                f"sandbox_root must be absolute, got {sandbox_root!r}"
            )
        self._sandbox_root = root.resolve()

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        raw_path = str(args.get("file_path", ""))
        if not raw_path:
            return _reject("'file_path' is required.")
        resolved_str = resolve_tool_path(raw_path)
        target = Path(resolved_str).resolve()
        if not _is_within(target, self._sandbox_root):
            return _reject(
                f"Refused: {target} is outside the statistician sandbox "
                f"({self._sandbox_root}). Statistician may only edit under "
                f"this root. Flag production-code changes to @swe via "
                f"AgentSend instead."
            )
        return await super().run(args)


# ---------------------------------------------------------------------
# SandboxedBash — deny-list of destructive commands
# ---------------------------------------------------------------------


# Pure-regex deny patterns. These match irrespective of arguments —
# the action itself is the problem.
_BANNED_BASH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgit\s+reset\s+--hard\b"),
        "`git reset --hard` is destructive (drops uncommitted work). "
        "Use `git stash` to discard with recovery, or `git reset --soft "
        "<ref>` to move HEAD without touching the working tree.",
    ),
    (
        re.compile(r"\bgit\s+push\s+(?:[^|;&]*\s+)?(?:--force|-f)\b"),
        "`git push --force` / `-f` is blocked. Force-push rewrites public "
        "history; escalate to @user via AgentSend if you genuinely need it.",
    ),
    (
        re.compile(r"\bgit\s+clean\s+(?:[^|;&]*\s+)?-[a-zA-Z]*[fd]"),
        "`git clean -fd` deletes untracked files irrecoverably. Don't use "
        "it. Use `rm <exact-path>` for a specific file, or `git restore .` "
        "to revert tracked changes only.",
    ),
    (
        re.compile(r"\bsudo\b"),
        "`sudo` is blocked. v3 agents run as the operator user; escalate "
        "anything needing root to @user via AgentSend.",
    ),
    (
        re.compile(r":\(\)\s*\{|:\(\)\s*\{[^}]*\|"),
        "Fork bomb pattern detected. Blocked.",
    ),
    (
        re.compile(r"\bdd\s+(?:[^|;&]*\s+)?of=/dev/"),
        "`dd of=/dev/*` (writing to a device) is blocked.",
    ),
    (
        re.compile(r">\s*/dev/sd[a-z]|>\s*/dev/nvme"),
        "Writing directly to a block device is blocked.",
    ),
)


# rm -rf needs path-aware checking: allowed on /tmp/ targets, blocked
# elsewhere. Pattern matches ``rm -rf``, ``rm -fr``, ``rm -r -f``,
# ``rm -rfv``, etc. — any rm invocation with both -r and -f flags.
_RM_RECURSIVE_FORCE_RE = re.compile(
    r"""
    \brm\s+         # rm command
    (?:             # one or more flag groups, must include both r and f
        -[a-zA-Z]*  # short flags like -rf or -r
        \s+
    )+
    (.+?)           # capture everything after flags, lazily
    (?:\s*(?:[;&|]|$))  # stop at a shell separator or end-of-string
    """,
    re.VERBOSE,
)


def _rm_targets_outside_tmp(command: str) -> list[str] | None:
    """If the command contains a recursive-force ``rm`` invocation, return
    the list of target paths that are NOT under ``/tmp/``. Return None if
    no such rm invocation is present.

    Allowed: ``rm -rf /tmp/x``, ``rm -rf /tmp/foo /tmp/bar``,
             ``rm -rf "/tmp/has spaces"``.
    Blocked: ``rm -rf .venv``, ``rm -rf /home/jp/x``,
             ``rm -rf /tmp/x /home/jp/y`` (mixed targets — block).
    """
    import shlex

    bad: list[str] = []
    found_any_rm = False
    for match in _RM_RECURSIVE_FORCE_RE.finditer(command):
        # The match needs to actually be a -rf / -fr (both flags present).
        full = match.group(0)
        flags_segment = full.split(None, 1)[1] if len(full.split(None, 1)) > 1 else ""
        # Pull just the flag groups (tokens starting with `-`) from the head.
        flag_tokens = []
        for tok in flags_segment.split():
            if tok.startswith("-"):
                flag_tokens.append(tok)
            else:
                break
        all_flag_chars = "".join(t.lstrip("-") for t in flag_tokens)
        if "r" not in all_flag_chars or "f" not in all_flag_chars:
            continue  # not destructive enough; allow super().run to handle
        found_any_rm = True
        target_blob = match.group(1).strip()
        try:
            tokens = shlex.split(target_blob)
        except ValueError:
            # Malformed quoting — refuse to take chances, treat as bad.
            bad.append(target_blob)
            continue
        for tok in tokens:
            if tok.startswith("-"):
                # Trailing flag (unusual but legal). Ignore.
                continue
            # Allow /tmp/ and subpaths. Allow exactly "/tmp" too.
            if tok == "/tmp" or tok.startswith("/tmp/"):
                continue
            bad.append(tok)
    return bad if found_any_rm else None


# ``git push`` to ``main`` (any non-force form too — branches called
# main/master shouldn't be pushed-to from agents at all). The earlier
# force-push pattern still catches force; this catches the plain form.
_GIT_PUSH_MAIN_RE = re.compile(
    r"""
    \bgit\s+push\b      # git push
    (?:\s+[^|;&\s]+)?   # optional remote name
    \s+
    (?:HEAD:)?          # optional HEAD: prefix
    (?:main|master)\b   # the protected branch name
    """,
    re.VERBOSE,
)


def _check_command(command: str) -> str | None:
    """Validate a bash command. Return rejection message if blocked, None if OK."""
    # 1. Pure-regex bans.
    for pattern, rejection in _BANNED_BASH_PATTERNS:
        if pattern.search(command):
            return (
                f"Refused destructive command pattern.\n\n"
                f"Matched: {pattern.pattern}\n"
                f"Why: {rejection}"
            )
    # 2. rm -rf with non-/tmp targets.
    bad_targets = _rm_targets_outside_tmp(command)
    if bad_targets:
        return (
            f"Refused recursive-force `rm` on non-/tmp paths: "
            f"{bad_targets!r}.\n\n"
            f"`rm -rf` is only allowed on paths under `/tmp/`. If you "
            f"want to wipe a real directory, escalate to @user via "
            f"AgentSend first with the rationale — the operator can "
            f"decide. The earlier `rm -rf .venv` cascade on 2026-06-04 "
            f"~16:10 cost us ~$1.50 in downstream flailing; this guard "
            f"exists so future versions don't repeat that pattern."
        )
    # 3. git push to main / master.
    if _GIT_PUSH_MAIN_RE.search(command):
        return (
            "Refused `git push ... main` (or `master`). Agents must not "
            "push directly to the protected branch. Push to a feature "
            "branch (e.g. `git push origin feat/<slug>`) and ask @user "
            "via AgentSend to open a PR if you need the changes merged."
        )
    return None


class SandboxedBash(Bash):
    """``Bash`` with deny-list + path-aware policy.

    Built from concrete failure modes observed during v3 live testing
    on 2026-06-04, not a theoretical threat model. The rules:

      * ``rm -rf`` / ``rm -fr`` / etc. — only allowed on paths under
        ``/tmp/``. Was the root cause of the 16:10 SWE thrash that
        broke the venv and cost ~$1.50 in cascading-fail flailing.
      * ``git push ... main`` (or ``master``) — blocked. Agents must
        push to feature branches and ask the operator to open PRs.
      * ``git push --force`` / ``-f`` — blocked regardless of target.
      * ``git reset --hard`` — blocked (use `git stash` instead).
      * ``git clean -fd`` — blocked (deletes untracked irrecoverably).
      * ``sudo`` — blocked (agents run as operator user; escalate).
      * Fork bombs, ``dd of=/dev/*``, raw block-device writes — blocked.

    This is not a security boundary -- a clever adversarial model
    could construct payloads that evade the regex (eval'd strings,
    base64 decoded payloads, etc.). It's a guardrail against the
    *common* mistakes cheap-tier coordinators reach for when they
    panic-fix issues.
    """

    async def run(self, args: Mapping[str, object]) -> ToolResult:
        command = str(args.get("command", ""))
        if not command:
            return await super().run(args)
        rejection = _check_command(command)
        if rejection is not None:
            return _reject(rejection)
        return await super().run(args)
