"""非遗体验安全放行后端。

事件溯源式领域服务：登记（技艺版本/材料批次/讲师资格/场地）→ 报名 → 放行评估签批
→ 现场签到与材料领用（支持离线补传、只记一次）→ 成品登记追溯；
事故触发同批次未开始场次冻结，经管理人员评估后恢复。
"""

from __future__ import annotations

from .clearance import ClearanceDecision, Reason, evaluate_clearance
from .errors import (
    AuthorizationError,
    Clock,
    ConflictError,
    DomainError,
    FixedClock,
    NotFoundError,
)
from .events import EVENT_KINDS, REQUIRED_FIELDS, Event, validate_event
from .models import (
    DECISION_CLEARED,
    DECISION_RESTRICTED,
    ROLE_INSTRUCTOR,
    ROLE_OFFICER,
    ROLE_OPERATOR,
)
from .queries import daily_report, session_safety_view, trace_product
from .repository import Repository, replay
from .service import Principal, SafetyService

__all__ = [
    "EVENT_KINDS",
    "REQUIRED_FIELDS",
    "Event",
    "validate_event",
    "Repository",
    "replay",
    "SafetyService",
    "Principal",
    "evaluate_clearance",
    "ClearanceDecision",
    "Reason",
    "session_safety_view",
    "trace_product",
    "daily_report",
    "Clock",
    "FixedClock",
    "DomainError",
    "NotFoundError",
    "ConflictError",
    "AuthorizationError",
    "DECISION_CLEARED",
    "DECISION_RESTRICTED",
    "ROLE_OPERATOR",
    "ROLE_OFFICER",
    "ROLE_INSTRUCTOR",
]
