"""非遗体验安全放行后端。

事件溯源（event-sourced）实现：所有登记与决策以追加事件保存，
当前状态由事件回放得到。模块包同时保留早期领域词表接口
（``EVENT_KINDS`` / ``REQUIRED_FIELDS`` / ``validate_event``）。
"""

from __future__ import annotations

from .errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .events import EventStore, EVENT_KINDS, REQUIRED_FIELDS, validate_event
from .models import (
    CraftVersion,
    KeyStep,
    MaterialBatch,
    Participant,
    Session,
    SessionStatus,
    Venue,
    Instructor,
)
from .service import SafetyService
from .rules import Decision
from .clock import Clock, SystemClock, FakeClock
from .security import Actor, Role, redact_health
from .traceability import trace_product
from .daily import daily_report

__all__ = [
    "EventStore",
    "SafetyService",
    "Decision",
    "Clock",
    "SystemClock",
    "FakeClock",
    "Actor",
    "Role",
    "redact_health",
    "trace_product",
    "daily_report",
    "CraftVersion",
    "KeyStep",
    "MaterialBatch",
    "Participant",
    "Session",
    "SessionStatus",
    "Venue",
    "Instructor",
    "DomainError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "PermissionDenied",
    "EVENT_KINDS",
    "REQUIRED_FIELDS",
    "validate_event",
]
