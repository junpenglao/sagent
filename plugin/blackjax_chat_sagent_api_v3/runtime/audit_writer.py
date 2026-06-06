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
        records: list[dict[str, object]] = []
        # Group multiple AgentSend calls with identical content into one audit record.
        # Track urgency per content group: if any call in the group is urgent,
        # the entire audit record is marked urgent.
        content_to_recipients: dict[str, list[str]] = {}
        content_to_urgent: dict[str, bool] = {}
        for tc in msg.tool_calls:
            if tc.name != "AgentSend":
                continue
            args = tc.args if isinstance(tc.args, dict) else {}
            to = str(args.get("to", "")).strip()
            body = str(args.get("content", ""))
            urgent = bool(args.get("urgent", False))
            if not to or not body:
                continue
            if body not in content_to_recipients:
                content_to_recipients[body] = []
                content_to_urgent[body] = False
            if to not in content_to_recipients[body]:
                content_to_recipients[body].append(to)
            if urgent:
                content_to_urgent[body] = True

        for body, recipients in content_to_recipients.items():
            record = {
                "ts": _iso8601_z(),
                "from": self.role_name,
                "to": recipients,
                "body": body,
            }
            if content_to_urgent[body]:
                record["urgent"] = True
            records.append(record)
        # Canonical v1/v2 design (restored 2026-06-04 17:xx UTC):
        # ONLY explicit ``AgentSend`` tool calls land in the audit
        # log + web UI. Assistant text without an AgentSend stays in
        # the trace ONLY (operator can open the trace panel for
        # debugging when they suspect a model emitted prose instead
        # of calling the tool).
        #
        # An earlier iteration synthesized a ``to=user`` record from
        # any non-empty assistant text in turns that didn't address
        # ``user`` via AgentSend. That worked as a safety net when
        # TL was on gemini-2.5-flash and would empty-respond or
        # text-respond instead of calling the tool. After upgrading
        # TL to gemini-2.5-pro the tool-use behaviour became
        # reliable, but the synthetic fallback ALSO captured TL's
        # post-action narration ("Thank you for the question, I've
        # forwarded it to @swe...") and leaked it to the chat. The
        # operator's expectation is right: silence means the model
        # didn't intend to message them.
        # Note: ``urgent`` is now surfaced since AgentSend was extended
        # to support it. Matches v2 schema for operator visibility.
        if not records:
            return
        try:
            with self.audit_log.open("a", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning(
                "audit_writer: failed to append %s records: %s",
                self.role_name, exc,
            )


def install_on(agent: Any, role_name: str, *, audit_log_path: Path | None = None) -> AuditWriter:  # noqa: ANN401
    """Attach an AuditWriter observer to an agent and return it."""
    writer = AuditWriter(role_name, audit_log_path=audit_log_path)
    agent.runtime.observers.append(writer)
    return writer
