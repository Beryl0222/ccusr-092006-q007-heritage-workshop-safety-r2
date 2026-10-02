"""可替换的时间源，便于离线补传与测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))


class Clock:
    def now(self) -> datetime:  # pragma: no cover - 接口
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """测试用固定/可拨动时钟。"""

    def __init__(self, start: datetime | str):
        self._now = start if isinstance(start, datetime) else parse_dt(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, value: datetime | str) -> None:
        self._now = value if isinstance(value, datetime) else parse_dt(value)


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO8601 字符串，裸时间按 +08:00（景区本地）处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
