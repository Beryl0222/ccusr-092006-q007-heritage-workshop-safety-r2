"""测试用世界构造：四个非遗技艺、材料批次、讲师、场地。"""

from __future__ import annotations

from src.heritage_workshop_safety import (
    Actor,
    EventStore,
    FakeClock,
    Role,
    SafetyService,
)

NOW = "2026-10-02T08:00:00+08:00"

MERCHANT = Actor("m-001", Role.MERCHANT, "工坊主")
OFFICER = Actor("o-001", Role.OFFICER, "安全员")
ADMIN = Actor("a-001", Role.ADMIN, "运营管理员")


def build_service(path: str | None = None, now: str = NOW) -> SafetyService:
    clock = FakeClock(now)
    store = EventStore(path, clock=clock)
    return SafetyService(store, clock=clock)


def seed_world(svc: SafetyService, actor: Actor = MERCHANT) -> None:
    """登记合香/扎染/彩灯/风筝四个技艺版本及配套资源。"""
    svc.register_craft(actor, {
        "craft_code": "incense", "version": "1", "name": "合香",
        "age_min": 10, "age_max": 99,
        "key_steps": [
            {"step_id": "k1", "name": "配伍", "hazards": []},
            {"step_id": "k2", "name": "和香", "hazards": []},
        ],
        "required_certificates": ["incense_safety"],
    })
    svc.register_craft(actor, {
        "craft_code": "dye", "version": "1", "name": "扎染",
        "age_min": 8, "age_max": 99,
        "key_steps": [
            {"step_id": "k1", "name": "捆扎", "hazards": ["sharp_tool"]},
            {"step_id": "k2", "name": "浸染", "hazards": ["dye"]},
        ],
        "required_certificates": ["dye_safety"],
    })
    svc.register_craft(actor, {
        "craft_code": "lantern", "version": "1", "name": "彩灯",
        "age_min": 12, "age_max": 99,
        "key_steps": [
            {"step_id": "k1", "name": "焊架", "hazards": ["electrical", "heat"]},
        ],
        "required_certificates": ["lantern_safety"],
    })
    svc.register_craft(actor, {
        "craft_code": "kite", "version": "1", "name": "风筝制作",
        "age_min": 8, "age_max": 99,
        "key_steps": [
            {"step_id": "k1", "name": "劈竹", "hazards": ["sharp_tool"]},
        ],
        "required_certificates": ["kite_safety"],
    })

    svc.receive_material(actor, {
        "batch_id": "b-inc-001", "material_code": "incense-powder",
        "name": "合香香粉", "supplier": "云香坊",
        "allergens": ["sandalwood", "frankincense"],
        "received_at": "2026-09-01T09:00:00+08:00",
        "expires_at": "2027-09-01T09:00:00+08:00",
    })
    svc.receive_material(actor, {
        "batch_id": "b-inc-002", "material_code": "incense-powder",
        "name": "合香香粉（无檀香配方）", "supplier": "云香坊",
        "allergens": ["frankincense"],
        "received_at": "2026-09-20T09:00:00+08:00",
        "expires_at": "2027-09-20T09:00:00+08:00",
    })
    svc.receive_material(actor, {
        "batch_id": "b-dye-001", "material_code": "dye-indigo",
        "name": "靛蓝染料", "supplier": "大理染坊",
        "allergens": ["indigo"],
        "received_at": "2026-09-10T09:00:00+08:00",
        "expires_at": "2026-11-10T09:00:00+08:00",
    })
    svc.receive_material(actor, {
        "batch_id": "b-bamboo-001", "material_code": "bamboo",
        "name": "竹篾", "supplier": "本地竹器社",
        "allergens": [],
        "received_at": "2026-09-10T09:00:00+08:00",
        "expires_at": "2027-09-10T09:00:00+08:00",
    })

    svc.register_instructor(actor, {
        "instructor_id": "t-lin", "name": "林师傅",
        "qualified_crafts": ["incense", "kite"],
        "certificates": {
            "incense_safety": "2027-12-31T23:59:59+08:00",
            "kite_safety": "2027-12-31T23:59:59+08:00",
        },
    })
    svc.register_instructor(actor, {
        "instructor_id": "t-zhou", "name": "周师傅",
        "qualified_crafts": ["dye", "lantern"],
        "certificates": {
            "dye_safety": "2027-12-31T23:59:59+08:00",
            "lantern_safety": "2027-12-31T23:59:59+08:00",
        },
    })
    # 证书已过期的讲师
    svc.register_instructor(actor, {
        "instructor_id": "t-old", "name": "退休师傅",
        "qualified_crafts": ["incense"],
        "certificates": {"incense_safety": "2026-01-01T00:00:00+08:00"},
    })

    svc.register_venue(actor, {
        "venue_id": "v-hall-a", "name": "甲号工坊厅",
        "station_count": 20,
        "emergency_facilities": ["first_aid_kit", "fire_extinguisher", "eye_wash"],
    })
    svc.register_venue(actor, {
        "venue_id": "v-small", "name": "边角小位",
        "station_count": 2,
        "emergency_facilities": [],
    })


def schedule(svc: SafetyService, actor: Actor, *,
             session_id="s1", craft="incense", version="1",
             venue="v-hall-a", instructor="t-lin", batches=("b-inc-001",),
             start="2026-10-02T09:00:00+08:00",
             end="2026-10-02T10:30:00+08:00"):
    return svc.schedule_session(actor, {
        "session_id": session_id,
        "craft_code": craft,
        "version": version,
        "venue_id": venue,
        "instructor_id": instructor,
        "batch_ids": list(batches),
        "scheduled_start": start,
        "scheduled_end": end,
    })


def kid(pid: str, age: int = 10, *, guardian=True, allergies=(), alias="") -> dict:
    return {
        "participant_id": pid,
        "age": age,
        "guardian_confirmed": guardian,
        "allergy_declarations": list(allergies),
        "alias": alias,
    }
