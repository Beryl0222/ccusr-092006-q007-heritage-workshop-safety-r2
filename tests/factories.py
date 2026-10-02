"""测试用场景工厂：合香/风筝两个技艺、香粉批次、讲师、场地。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from src.heritage_workshop_safety import (
    FixedClock,
    Principal,
    Repository,
    ROLE_INSTRUCTOR,
    ROLE_OFFICER,
    ROLE_OPERATOR,
    SafetyService,
)

CST = timezone(timedelta(hours=8))
DAY = date(2026, 10, 3)
START = datetime.combine(DAY, time(9, 0), CST)
END = datetime.combine(DAY, time(11, 0), CST)

OPERATOR = Principal("merchant-1", ROLE_OPERATOR)
OFFICER = Principal("officer-1", ROLE_OFFICER)
TEACHER = Principal("teacher-1", ROLE_INSTRUCTOR)


def build_service(clock=None) -> SafetyService:
    svc = SafetyService(Repository(), clock or FixedClock(START))

    # 合香：适龄 8-80，无刀具，但要求通风/洗眼等应急设施
    svc.register_craft_version(
        OPERATOR,
        craft_id="incense",
        version="2026-1",
        title="传统合香",
        age_min=8,
        age_max=80,
        key_steps=[
            {"seq": 1, "name": "辨识香粉"},
            {"seq": 2, "name": "和香捶丸"},
        ],
        required_facilities=["FIRST_AID_KIT", "EYE_WASH"],
    )
    # 风筝：削竹条使用刀具，须持刀具看护证
    svc.register_craft_version(
        OPERATOR,
        craft_id="kite",
        version="2026-1",
        title="风筝扎糊",
        age_min=10,
        age_max=70,
        key_steps=[
            {"seq": 1, "name": "削竹条", "hazards": ["CUTTING_TOOL"]},
            {"seq": 2, "name": "糊纸彩绘"},
        ],
        required_facilities=["FIRST_AID_KIT"],
    )

    svc.receive_material(
        OPERATOR,
        batch_id="B-INC-01",
        material_name="檀香合香粉",
        supplier="云岭香材合作社",
        allergens=["SANDALWOOD"],
    )
    svc.receive_material(
        OPERATOR,
        batch_id="B-INC-02",
        material_name="花香合香粉",
        supplier="云岭香材合作社",
        allergens=["JASMINE"],
    )
    svc.receive_material(
        OPERATOR,
        batch_id="B-BAMBOO-01",
        material_name="竹条纸材包",
        supplier="潍坊筝坊",
        allergens=[],
    )

    svc.register_instructor(
        OPERATOR,
        instructor_id="teacher-1",
        name="王护",
        certifications=[{"code": "CUTTING_TOOL_SUPERVISION", "valid_from": "2026-01-01", "valid_to": "2026-12-31"}],
    )
    svc.register_instructor(OPERATOR, instructor_id="teacher-2", name="李助", certifications=[])

    svc.register_venue(
        OPERATOR,
        venue_id="venue-1",
        name="一号工坊",
        station_capacity=4,
        facilities=["FIRST_AID_KIT", "EYE_WASH"],
    )
    svc.register_venue(
        OPERATOR,
        venue_id="venue-2",
        name="角落工位",
        station_capacity=1,
        facilities=["FIRST_AID_KIT"],
    )
    return svc


def schedule_incense(svc: SafetyService, session_id: str = "S-1", **kw) -> str:
    kw.setdefault("batch_ids", ["B-INC-01"])
    kw.setdefault("instructor_ids", ["teacher-2"])
    kw.setdefault("venue_id", "venue-1")
    svc.schedule_session(
        OPERATOR,
        session_id=session_id,
        craft_id="incense",
        craft_version="2026-1",
        scheduled_start=kw.pop("scheduled_start", START),
        scheduled_end=kw.pop("scheduled_end", END),
        **kw,
    )
    return session_id


def schedule_kite(svc: SafetyService, session_id: str = "S-KITE", *, certified: bool = True, **kw) -> str:
    kw.setdefault("batch_ids", ["B-BAMBOO-01"])
    kw.setdefault("instructor_ids", ["teacher-1"] if certified else ["teacher-2"])
    kw.setdefault("venue_id", "venue-1")
    svc.schedule_session(
        OPERATOR,
        session_id=session_id,
        craft_id="kite",
        craft_version="2026-1",
        scheduled_start=kw.pop("scheduled_start", START),
        scheduled_end=kw.pop("scheduled_end", END),
        **kw,
    )
    return session_id


def enroll(svc: SafetyService, session_id: str, pid: str, *, birth: str, allergies=(), guardian=True, name=None):
    return svc.add_enrollment(
        OPERATOR,
        session_id=session_id,
        participant_id=pid,
        name=name or pid,
        birth_date=birth,
        allergy_declarations=list(allergies),
        guardian_confirmed=guardian,
    )
