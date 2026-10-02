"""时钟与领域错误。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


class Clock:
    def now(self) -> datetime:
        return datetime.now(CST)


class FixedClock:
    """测试用固定时钟。"""

    def __init__(self, moment: datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CST)
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的聚合不存在。"""


class ConflictError(DomainError):
    """重复登记或状态不允许该操作。"""


class AuthorizationError(DomainError):
    """角色无权执行或查看。"""
