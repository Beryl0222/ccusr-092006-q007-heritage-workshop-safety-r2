# 非遗体验安全放行后端

景区合香、扎染、彩灯、风筝制作“随到随学”体验的安全放行服务。工坊登记
技艺版本与关键步骤、材料批次（含过敏原与保质期）、适龄范围、讲师资格
证书与场地应急设施；场次依据参与者年龄、监护确认、过敏声明、工位容量
与应急设施决定**正常放行 / 限制放行 / 取消**；事故发生后先冻结同批次
未开始场次，再由有权人员评估恢复。全部状态变更以只追加事件保存，成品
可从一件游客成品追溯到场次、指导者与材料来源。

## 设计要点

- **事件溯源**：登记、排期、报名、签字、签到、领用、成品、事故、冻结、
  恢复均为追加事件（默认 JSONL 落盘，可重启回放），任何改动留有审计痕迹。
- **放行基线指纹**：签字对“技艺版本 + 材料批次（含状态）+ 讲师（含证书
  有效期）+ 场地（含应急设施）+ 全部参与者声明”取 SHA-256 指纹。更换
  材料、讲师或场地会改变指纹，旧签字立即失效，场次回到待放行状态，
  **不能沿用上一场签字**。
- **幂等的离线补传**：签到与材料领用支持 `client_token`，同时以
  `(场次, 参与者[, 批次])` 业务键去重，断网补传只能记一次；
  `occurred_at` 保留现场时间。
- **事故处置顺序**：事故上报 → 相关批次冻结 → 同批次**未开始**场次
  冻结（已有签到或已到开始时间的场次视为已开始，不自动冻结）→ 安全员
  评估批次解冻、逐场重新检查并重新签字后恢复；恢复时也可直接换绑安全
  批次/讲师/场地，评估通过才落库。
- **健康信息保护**：角色分 `merchant`（商户）、`officer`（安全管理人员）、
  `admin`。商户视图中过敏声明被掩码（只见条目数与冲突提示），完整健康
  信息与成品追溯仅对管理人员开放；签字必须由管理人员完成，商户不能
  自评自放。
- **当日日报**：输出当天哪些场次因何被限制、取消、冻结、重新放行，以及
  换绑后待重检的场次。

## 放行规则（失败代码）

| 代码 | 级别 | 含义 |
|---|---|---|
| `craft_not_found` / `batch_not_found` / `instructor_not_found` / `venue_not_found` | 阻断 | 引用的登记资料不存在 |
| `batch_suspended` / `batch_expired` | 阻断 | 材料批次已冻结或过保质期 |
| `instructor_not_qualified` | 阻断 | 讲师不具备该技艺执教资格 |
| `certificate_missing` / `certificate_expired` | 阻断 | 缺少或证书过期（刀具/加热/用电等工位看护要求） |
| `capacity_exceeded` | 阻断 | 报名人数超过工位容量 |
| `emergency_facility_missing` | 阻断 | 危险工序要求的应急设施缺失（利器→急救包；染色→洗眼器；加热/用电→灭火器等） |
| `age_out_of_range` | 个体阻断 | 不在技艺适龄范围 |
| `guardian_unconfirmed` | 个体阻断 | 未成年人缺少监护确认 |
| `allergy_conflict` | 个体阻断 | 参与者过敏声明与批次成分冲突 |

存在场次级阻断 → `denied`（取消）；仅个别参与者被排除 → `restricted`
（限制放行，排除名单不可签到）；全部通过 → `cleared`。

## 目录

- `src/heritage_workshop_safety/`
  - `events.py`：16 种事件与 JSONL 事件存储（保留早期词表 `validate_event`）
  - `models.py` / `projection.py`：领域模型与事件回放投影
  - `rules.py`：放行规则引擎与基线指纹
  - `service.py`：应用服务（登记、排期、报名、签字、幂等签到/领用、事故冻结/恢复）
  - `security.py`：角色与健康信息脱敏
  - `traceability.py`：成品→场次→指导者→材料来源追溯
  - `daily.py`：当日限制/取消/冻结/恢复日报
  - `api.py` / `__main__.py`：标准库 HTTP JSON API（零第三方运行依赖）
- `tests/`：45 个 unittest（规则、基线失效、幂等补传、事故链路、脱敏、日报、HTTP 端到端）
- `data/sample.json`：事件格式虚构样例

## 运行

```bash
# 内存存储快速体验
python3 -m src.heritage_workshop_safety --host 127.0.0.1 --port 8080 --store ""

# JSONL 持久化（默认 data/events.jsonl）
python3 -m src.heritage_workshop_safety
```

所有请求需带网关透传头：`X-Actor-Id`、`X-Actor-Role`
（`merchant` / `officer` / `admin`）。

### 典型调用

```bash
# 登记（商户即可）
curl -s -X POST localhost:8080/materials -H "X-Actor-Id: m1" -H "X-Actor-Role: merchant" \
  -H "Content-Type: application/json" -d '{
    "batch_id":"b-inc-001","material_code":"incense-powder","name":"合香香粉",
    "supplier":"云香坊","allergens":["sandalwood"],
    "received_at":"2026-09-01T09:00:00+08:00","expires_at":"2027-09-01T09:00:00+08:00"}'

# 安全员签字放行
curl -s -X POST localhost:8080/sessions/s1/clearance \
  -H "X-Actor-Id: o1" -H "X-Actor-Role: officer"

# 签到（幂等键 + 现场时间，支持离线补传）
curl -s -X POST localhost:8080/sessions/s1/checkins -H "X-Actor-Id: m1" -H "X-Actor-Role: merchant" \
  -H "Content-Type: application/json" \
  -d '{"participant_id":"p1","client_token":"dev-abc","occurred_at":"2026-10-02T09:05:00+08:00"}'

# 事故 → 冻结同批次未开始场次
curl -s -X POST localhost:8080/incidents -H "X-Actor-Id: o1" -H "X-Actor-Role: officer" \
  -H "Content-Type: application/json" \
  -d '{"incident_id":"inc-1","session_id":"s1","description":"接触香粉后皮疹","severity":"health"}'

# 评估恢复：解冻批次 → 重新检查场次（也可在 release 时附 changes 直接换绑安全批次）
curl -s -X POST localhost:8080/batches/b-inc-001/release -H "X-Actor-Id: o1" -H "X-Actor-Role: officer" \
  -H "Content-Type: application/json" -d '{"note":"第三方检测合格"}'
curl -s -X POST localhost:8080/sessions/s2/release -H "X-Actor-Id: o1" -H "X-Actor-Role: officer" \
  -H "Content-Type: application/json" -d '{"note":"复检通过"}'

# 成品追溯与日报（仅 officer/admin）
curl -s localhost:8080/products/prod-1/trace -H "X-Actor-Id: o1" -H "X-Actor-Role: officer"
curl -s "localhost:8080/reports/daily?day=2026-10-02" -H "X-Actor-Id: o1" -H "X-Actor-Role: officer"
```

### 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/crafts` `/materials` `/instructors` `/venues` | 技艺版本/批次/讲师/场地登记 |
| POST | `/sessions` · GET `/sessions?day=` | 排期与列表 |
| GET | `/sessions/{id}` | 场次视图（健康信息按角色脱敏） |
| POST | `/sessions/{id}/enrollments` | 报名（年龄、监护确认、过敏声明） |
| POST | `/sessions/{id}/amend` | 换批次/讲师/场地/版本，旧签字失效 |
| GET | `/sessions/{id}/clearance/preview` · POST `/sessions/{id}/clearance` | 预检 / 签字（officer/admin） |
| POST | `/sessions/{id}/checkins` · `/sessions/{id}/issues` | 幂等签到、材料领用（可离线补传） |
| POST | `/products` · GET `/products/{id}/trace` | 成品登记与追溯 |
| POST | `/incidents` | 事故上报并联动冻结（officer/admin） |
| POST | `/batches/{id}/release` | 批次评估解冻（officer/admin） |
| GET | `/sessions/{id}/reassess` · POST `/sessions/{id}/release` | 冻结场次重新评估与恢复（支持 `changes` 换绑） |
| GET | `/reports/daily?day=YYYY-MM-DD` | 当日安全放行日报 |

## 测试

```bash
python3 -m unittest discover -s tests
```

> 生产部署时 HTTP 服务必须置于鉴权网关之后，由网关注入操作者头；
> 当前实现不自行完成登录认证。资料均为虚构，不含真实个人信息。
