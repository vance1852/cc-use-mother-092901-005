# 联审跨区域旅游包车协同服务

本项目在综合交通运输共享基础能力（运营机构、操作者、角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，实现一套完整的**跨区域旅游包车联审与履约服务**：

- 旅行社一次提交团次、车辆、驾驶员、线路区段、停靠计划与含退款责任的合同承诺；
- 各地审批人只能处理本辖区责任区段，区段批准串联成**许可链**，全部齐备后才签发可执行行程；
- 规则引擎识别证照有效期、跨日工时、车辆/驾驶员重复占用、重复申报与互斥路线；
- 临时封路、旅客人数变化、车辆替换、驾驶员替班、取消按**影响范围**生成新版本：原批准事实保留，不受影响区段的审批自动沿用；
- 行程开始后普通改动不会整体回退，头部状态保持履约中，仅受影响区段重审；
- 执法人员凭密钥核验当前有效许可、责任地区与异常处置依据；旅行社可查询阻塞原因、退款责任与仍可沿用的审批结果。

运行时仅使用 Python 标准库和 SQLite。

## 目录

- `src/transport_coordination/`
  - `service.py`：基础登记、角色权限、幂等回执；
  - `charter.py`：团次提交、分区审批、许可链签发、版本修订、履约事件、执法核验；
  - `rules.py`：证照有效期、跨日出勤/累计驾驶/连续驾驶、人数容差等纯函数规则；
  - `storage.py`：SQLite 建表与事务（新旧库均自动补建联审业务表）；
  - `audit.py` / `clock.py` / `errors.py`：哈希审计链、可替换时钟、业务异常；
  - `api.py`：仅依赖标准库的 HTTP/JSON 边界；
  - `acceptance.py`：离线端到端验收。
- `tests/`：规则、服务事务、HTTP 路由与端到端验收测试。

## 核心规则

| 规则 | 结论编码 | 说明 |
| --- | --- | --- |
| 必需证照缺失 | `license.required_missing` | 车辆需行驶证与承运人保险，驾驶员需驾驶证与从业资格 |
| 证照已失效/中途失效/未生效 | `license.expired` / `license.covers_partial` / `license.not_yet_effective` | 必须覆盖行程完整窗口 |
| 辖区路线许可缺失/暂停/窗口不覆盖 | `permit.region_missing` / `permit.region_suspended` / `permit.window_gap` | 批准必须能落到一条具体辖区许可 |
| 跨日出勤超限 | `worktime.duty_day_exceeded` | 单值乘时段或单日首次上车至末次下车超 13 小时 |
| 单日累计驾驶超限 | `worktime.daily_driving_exceeded` | 按自然日累计驾驶超 8 小时 |
| 连续驾驶超限 | `worktime.continuous_driving_exceeded` | 连续驾驶超 4 小时须满 20 分钟休息，跨午夜不自动清零 |
| 车辆/驾驶员重复占用 | `vehicle.double_booked` / `driver.double_booked` | 时间窗重叠的已签发行程 |
| 互斥路线 | `route.mutex_conflict` | 同辖区同路线时段冲突 |
| 重复申报 | `duplicate.group_declaration` | 同旅行社同合同号同团号 |
| 超员 | `vehicle.overcapacity` | 旅客人数超过座位数 |

## 版本与影响范围

- 团次以 `group_id` 标识，每次提交或履约事件形成不可变的新版本（`charter_versions`），区段批准按版本保存；
- `road_closure`、`driver_change`、`stop_adjustment` 为局部事件，仅受影响区段回到待审，其余批准复制到新版本并记录 `carried_from_version`；
- `pax_change` 在 ±10% 容差内为 `none`，审批全部沿用；超出容差或 `vehicle_change` 为 `all`；
- `cancel` 保留全部原批准事实（以 `revoked` 记录并标注来源版本），团次不再可执行，并按合同退款政策给出退款责任；
- 行程发车（`/trip-start`）后头部状态锁定为 `in_progress`，任何普通改动都不会将其回退到 `in_review`。

## HTTP 接口

写入接口带 `X-Actor-Id` 与 `request_id`（幂等）；执法接口带 `X-Inspection-Key`。

- `POST /reviewer-regions`：为审批人分配辖区；
- `POST /region-licenses` / `POST /region-license-status`：登记辖区路线许可、暂停（封路）/恢复；
- `POST /inspection-keys`：签发执法核验密钥（可限定辖区）；
- `POST /charter-groups`：提交团次；
- `POST /segment-decisions`：辖区审批人批准/驳回本辖区区段；
- `POST /trip-events`：申报 `road_closure` / `pax_change` / `vehicle_change` / `driver_change` / `stop_adjustment` / `cancel`；
- `POST /trip-start`：确认发车；
- `GET /charter-groups?group_id=...`：旅行社视角（状态、阻塞原因、可沿用审批、退款责任）；
- `GET /inspection/verify?group_id=...`：执法核验（当前有效许可、责任地区、异常处置依据）；
- `GET /health`、`GET /audit-events`：健康检查与审计链查询。

## 环境与测试

- Linux，Python 3.11 或更高版本，仅依赖标准库与 SQLite。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令在临时 SQLite 库中完成「提交 → 两辖区许可链签发 → 发车 → 途中封路局部重审且原批准沿用 → 执法核验」全链，成功时输出 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

服务重启后 SQLite 中的团次版本、许可链与审计历史继续保留。
