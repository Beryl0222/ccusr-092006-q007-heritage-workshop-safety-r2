# 非遗体验安全放行后端

景区把合香、扎染、彩灯、风筝制作做成随到随学后，现场不能只看报名人数。本服务是运营联合体的
**非遗体验安全放行后端**：工坊登记技艺版本、关键步骤、材料批次、适龄范围与讲师资格；场次按参与者
年龄、监护确认、过敏声明、工位容量与应急设施决定能否开；事故发生后先冻结同批次未开始场次，再由
有权人员评估恢复。

全部状态以**只追加事件**为唯一事实来源（event sourcing），当前状态由事件流投影；离线补传与事件
重放因此天然可合并。

## 领域规则

### 登记（商户 operator）

- 技艺版本 `CRAFT_VERSIONED`：适龄范围、关键步骤（标注危险源，如 `CUTTING_TOOL`）、要求的应急设施。
- 材料批次 `MATERIAL_RECEIVED`：供应商、过敏原清单（香粉/染料批次）。
- 讲师资格 `INSTRUCTOR_QUALIFIED`：资格证代码与有效期；刀具工位须持 `CUTTING_TOOL_SUPERVISION`。
- 场地 `VENUE_REGISTERED`：工位容量、已配应急设施（急救包、洗眼器等）。

### 放行评审（仅管理人员 officer 可签批）

`SESSION_CLEARED` 记录决定（`CLEARED` / `RESTRICTED`）、原因清单与**签批时的场次快照版本号**：

| 原因码 | 含义 |
| --- | --- |
| `AGE_OUT_OF_RANGE` | 参与者年龄超出技艺适龄范围 |
| `GUARDIAN_UNCONFIRMED` | 未成年参与者缺少监护确认 |
| `ALLERGY_CONFLICT` | 过敏声明命中配给批次的过敏原 |
| `CAPACITY_EXCEEDED` | 报名人数超过工位容量 |
| `FACILITY_MISSING` | 场地缺少要求的应急设施 |
| `CERT_MISSING` | 关键步骤含危险源，但无持有效资格的看护讲师 |
| `BATCH_FROZEN` | 配给批次处于事故冻结 |
| `REFERENCE_INVALID` / `SESSION_CANCELLED` | 引用缺失 / 场次已取消 |

**签字不跨变更沿用**：报名变化（`ENROLLMENT_ADDED`）或更换材料/讲师/场地（`ASSIGNMENT_CHANGED`）
会抬升场次版本号并作废旧签批；批次冻结也会作废旧签批。重新评估签批后才能开场，签到时强制校验
存在与当前快照一致的有效放行。

### 现场（讲师 instructor / officer，支持离线）

- 签到 `CHECKIN_RECORDED` 与材料领用 `MATERIAL_ISSUED` 带 `source=online|offline`。
- **只能记一次**：同一 `event_id` 重传被拒；另有业务去重键（同场次同人签到、同场次同批次同人
  领用），即使换了 event_id 重复扫码也被拒。
- 成品 `PRODUCT_RECORDED` 固化场次、技艺版本、指导者与实际领用批次，作为追溯锚点（须先有领用记录）。

### 事故处置

1. `INCIDENT_REPORTED` 后立即发出 `BATCH_FROZEN`：批次停用，并自动作废**同批次所有未开始场次**
   的签批（已开始场次不动）；冻结批次拒绝继续领用与放行。
2. 仅 officer 可凭书面评估意见发出 `BATCH_RELEASED` 解冻；解冻不恢复旧签字，相关场次须重新放行。
3. 场次可由 operator/officer 取消（`SESSION_CANCELLED`）。

### 健康信息保护

- **商户**视图只有聚合信号（未成年人数、监护确认数、过敏冲突数），看不到“谁对什么过敏”，
  批次过敏原词表与过敏原因详情也不下发。
- **讲师**视图只含现场必需的“参与者↔冲突批次”提醒，不含申报明细。
- **管理人员**可见完整明细，并可由一件成品 `trace_product` 追到场次、指导者（含资格）、
  材料批次与来源供应商。
- `daily_report` 输出某天哪些场次被限制、取消，哪些在整改/解冻后**重新放行**及原因。

## 代码结构

```
src/heritage_workshop_safety/
  events.py       15 类只追加事件与最小交换字段
  models.py       事件投影出的领域模型
  repository.py   事件存储、投影、JSON 持久化与离线重放
  clearance.py    放行评审纯函数（原因码见上表）
  service.py      应用服务：用例编排、角色鉴权、幂等、事故冻结流程
  queries.py      角色化场次视图、成品追溯、当天管控日报
tests/            43 个用例（unittest / pytest 均可）
```

## 本地核对

```bash
python3 -m unittest discover -s tests
# 或
pip install -r requirements-test.txt
python3 -m pytest -q
```

`data/sample.json` 是仅用于格式核对的虚构事件样例。
