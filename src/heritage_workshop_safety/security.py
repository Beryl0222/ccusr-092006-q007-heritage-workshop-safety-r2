"""角色与健康信息脱敏。

- ``merchant``（商户/工坊）：只能看到脱敏后的健康信息（是否有冲突），
  看不到具体过敏原；可以登记、报名、签到。
- ``officer``（安全管理人员）：可查看完整健康信息、执行事故操作、
  冻结后评估恢复。
- ``admin``（运营联合体管理员）：全部权限，含追溯与日报。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    MERCHANT = "merchant"
    OFFICER = "officer"
    ADMIN = "admin"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role
    display_name: str = ""

    @property
    def can_view_health(self) -> bool:
        return self.role in (Role.OFFICER, Role.ADMIN)

    @property
    def can_clear(self) -> bool:
        # 放行签字由管理人员负责，商户不能自评自放
        return self.role in (Role.OFFICER, Role.ADMIN)

    @property
    def can_handle_incident(self) -> bool:
        return self.role in (Role.OFFICER, Role.ADMIN)

    @property
    def can_trace(self) -> bool:
        return self.role in (Role.OFFICER, Role.ADMIN)


def redact_health(participant_payload: dict, actor: Actor) -> dict:
    """按角色裁剪参与者视图：商户只见过敏条目数与冲突标志。"""
    view = dict(participant_payload)
    allergies = list(view.get("allergy_declarations", []))
    if not actor.can_view_health:
        view["allergy_declarations"] = ["***"] if allergies else []
        view["allergy_count"] = len(allergies)
        view["health_redacted"] = True
    else:
        view["allergy_count"] = len(allergies)
        view["health_redacted"] = False
    return view


def redact_decision(decision_payload: dict, actor: Actor) -> dict:
    """放行结论对商户屏蔽具体过敏原，只保留冲突代码与处理建议。"""
    view = dict(decision_payload)
    if actor.can_view_health:
        return view

    def _mask(item: dict) -> dict:
        item = dict(item)
        if item.get("code") == "allergy_conflict":
            msg = item.get("message", "")
            if "：" in msg:
                item["message"] = msg.split("：", 1)[0] + "：健康信息已隐藏，请联系安全管理人员"
        return item

    view["findings"] = [_mask(item) for item in view.get("findings", [])]
    # excluded: {participant_id: [finding,...]} 同样要脱敏
    view["excluded"] = {
        pid: [_mask(item) for item in items]
        for pid, items in view.get("excluded", {}).items()
    }
    return view
