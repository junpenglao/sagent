"""Audit-log writer for peer traffic.

In v2 the audit log was written by the MCP server's ``delivery.py``
because peer messaging went through ``/api/post`` (HTTP loopback
from the per-agent MCP subprocess). In v3 there's no MCP server —
sagent's bridge-mounted ``AgentSend`` pushes directly to the
recipient's inbox and never touches main.jsonl.

This observer fills that gap. Per agent: watch for
:class:`AssistantMessage` events that contain ``AgentSend`` tool
calls, and append one audit-log record per call so the web UI's
``/api/messages`` endpoint can surface peer traffic to the
operator the same way it always has.

We also emit a record for inbound user-side activity
(``UserMessage`` from the operator → recipient) but that's already
written by ``serve.py``'s ``/api/post`` handler, so we only emit
the outbound side here to avoid double-counting.

Schema (matches v2's ``main.jsonl`` format):

    {
      "ts": "<iso8601-z>",
      "from": "<sender label>",
      "to": ["<recipient>"],
      "body": "<text>",
      "urgent": true | (omitted)
    }
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> Path:
    """Mirror serve.py's resolution — same SAGENT_DATA_DIR env var.

    Keeping the lookup local so the audit writer doesn't pull in
    serve.py (which would create a circular import once we add the
    serve.py side of the wiring).
    """
    env = os.environ.get("SAGENT_DATA_DIR")
    base = (
        Path(env).expanduser().resolve()
        if env
        else Path(__file__).resolve().parent.parent
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _iso8601_z() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class AuditWriter:
    """Observer that emits one ``main.jsonl`` record per outbound
    ``AgentSend`` tool call from the watched agent.

    Operates on the assistant-turn boundary: when sagent's runtime
    publishes an ``AssistantMessage`` (the model's completed turn),
    we scan its ``tool_calls`` for ``AgentSend`` and write one
    audit record per call before sagent's runtime delivers the
    actual ``AgentSendMessage`` to the recipient. The order
    matches v2's contract: audit log entry first, then the inbox
    push.
    """

    def __init__(self, role_name: str, *, audit_log_path: Path | None = None) -> None:
        self.role_name = role_name
        self.audit_log = (
            audit_log_path
            if audit_log_path is not None
            else _resolve_data_dir() / "main.jsonl"
        )

    def __call__(self, event: Any) -> None:  # noqa: ANN401 -- runtime event variant
        # Imports kept lazy so this module can be imported without
        # forcing sagent's heavy runtime types to load.
        #
        # Runtime publishes ``ModelResponseComplete(message=msg)``
        # when an assistant turn lands — NOT the bare AssistantMessage
        # (the first probe iteration missed this and the audit log
        # was silent). Unwrap to get at the tool_calls.
        from sagent.types.runtime import ModelResponseComplete

        if not isinstance(event, ModelResponseComplete):
            return
        msg = event.message
        for tc in msg.tool_calls:
            if tc.name != "AgentSend":
                continue
            args = tc.args if isinstance(tc.args, dict) else {}
            to = str(args.get("to", "")).strip()
            content = str(args.get("content", ""))
            if not to or not content:
                continue
            record = {
                "ts": _iso8601_z(),
                "from": self.role_name,
                "to": [to],
                "body": content,
            }
            # Note: ``urgent`` is intentionally not surfaced here. The
            # operator can opt in to plumbing it once sagent's
            # AgentSend exposes the flag (see "deferred" item in the
            # v3 research notes).
            try:
                with self.audit_log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except OSError as exc:
                logger.warning(
                    "audit_writer: failed to append %s -> %s: %s",
                    self.role_name, to, exc,
                )


def install_on(agent: Any, role_name: str, *, audit_log_path: Path | None = None) -> AuditWriter:  # noqa: ANN401
    """Attach an AuditWriter observer to an agent and return it."""
    writer = AuditWriter(role_name, audit_log_path=audit_log_path)
    agent.runtime.observers.append(writer)
    return writer
