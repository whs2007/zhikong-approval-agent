"""审计日志模型（append-only）。详见 services/audit_service.py。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AuditEvent:
    event_type: str
    actor: str = "system"
    actor_role: str = "system"
    thread_id: str | None = None
    trace_id: str | None = None
    node: str | None = None
    phase_before: str | None = None
    phase_after: str | None = None
    detail: Any = None
    level: str = "INFO"

    def as_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "thread_id": self.thread_id,
            "trace_id": self.trace_id,
            "node": self.node,
            "phase_before": self.phase_before,
            "phase_after": self.phase_after,
            "detail": self.detail,
            "level": self.level,
        }
