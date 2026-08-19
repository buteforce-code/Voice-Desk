"""Audit sinks.

`agent_actions` is append-only and carries one row per tool *attempt*,
rejections included. The Postgres sink is written when the pipeline lands; this
in-memory one exists so the registry can be exercised locally and by the eval
harness without a database.

The interface is deliberately narrow: record, and look up a replay. There is no
update and no delete, here or in the schema, because C16 is prohibited and an
audit log you can edit is not an audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from voicedesk.tools.schemas import ToolContext


@dataclass(frozen=True)
class AuditRow:
    clinic_id: str
    call_id: str
    trace_id: str
    tool_name: str
    args_redacted: dict[str, Any]
    result: str
    idempotency_key: str
    authorized_by: str
    rejection_reason: str | None
    at: datetime


@dataclass
class InMemoryAudit:
    """Append-only in the same sense the table is: rows only go in."""

    rows: list[AuditRow] = field(default_factory=list)
    _replays: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def record(
        self,
        ctx: ToolContext,
        tool_name: str,
        args_redacted: dict[str, Any],
        result: str,
        idempotency_key: str,
        authorized_by: str,
        rejection_reason: str | None = None,
    ) -> None:
        self.rows.append(
            AuditRow(
                clinic_id=str(ctx.clinic_id),
                call_id=str(ctx.call_id),
                trace_id=ctx.trace_id,
                tool_name=tool_name,
                args_redacted=args_redacted,
                result=result,
                idempotency_key=idempotency_key,
                authorized_by=authorized_by,
                rejection_reason=rejection_reason,
                at=datetime.now(UTC),
            )
        )

    async def find_replay(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._replays.get(idempotency_key)

    def remember_replay(self, idempotency_key: str, data: dict[str, Any]) -> None:
        """Record a successful mutating result so a retry returns it verbatim.

        The Postgres sink derives this from the stored row; in memory it is set
        explicitly by whoever performed the write.
        """
        self._replays[idempotency_key] = data

    # -- reading, for the dashboard and for tests -------------------------

    def for_call(self, trace_id: str) -> list[AuditRow]:
        return [r for r in self.rows if r.trace_id == trace_id]

    def rejections(self) -> list[AuditRow]:
        """Every refused attempt. This is the view that shows what the model
        tried to do and was not allowed to."""
        return [r for r in self.rows if r.result == "rejected"]
