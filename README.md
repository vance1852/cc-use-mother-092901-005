# 联审跨区域旅游包车协同服务

本项目在综合交通运输共享基础能力（运营机构、操作者、场所、结构化参考资料登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，提供**跨区域旅游包车联审与履约**领域服务：旅行社一次提交团次、车辆、驾驶员、线路区段、停靠计划与合同承诺；各地交通/文旅审批人只处理本辖区责任区段，两方决策串联成许可链，所有必要许可齐备后才签发可执行行程单；行程中的临时封路、人数变化、车辆替换、替班与取消按影响范围重审，保留原批准事实，已开始的行程不会因普通改动被整体回退。

## 目录

- `src/transport_coordination/`：基础模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收，以及包车联审领域服务 `charter.py`；
- `tests/`：基础规则、事务边界、接口路由、包车联审规则与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 业务规则

- 证照有效期：道路运输证、承运人责任险、驾驶员从业资格证必须覆盖整个行程，否则申报直接拒绝；
- 工时：连续驾驶不超过 4 小时、单日累计驾驶不超过 8 小时、跨日衔接休息不少于 11 小时、连续任务不超过 48 小时，违规允许申报但阻塞许可批准与行程单签发；
- 超员：旅客人数不得超过车辆核定座位；
- 资源互斥与重复申报：车辆/驾驶员在时间窗内与已获批或持有效行程单的团次冲突时拒绝；
- 辖区隔离：审批人只能处理本辖区、本部门（交通/文旅）的区段许可，双方齐备才批准；
- 按影响范围重审：封路只重审受影响区段，换车/替班/人数变化重审全部区段，行程调整按区段范围哈希差异判定；未受影响区段以"继承决策"沿用原批准；
- 行程单版本链：修订后签发新版本，旧版本标记 `superseded`，链条以首版 `chain_head` 串联；重审期间旧证仍有效，已开始行程不整体回退；取消则作废行程单并给出退款责任口径。

## 主要接口（X-Actor-Id 标识操作者）

- `POST /charter/regions`、`/charter/vehicles`、`/charter/drivers`、`/charter/closures`：辖区责任部门、车辆驾驶员证照台账、临时封路登记；
- `POST /charter/filings`：旅行社团次申报（团次、车辆、驾驶员、区段、停靠、合同）；
- `GET /charter/pending-permits`：审批人本辖区待办；
- `POST /charter/permit-decisions`：辖区交通/文旅审批（approve/reject，可附条件）；
- `POST /charter/certificates`：许可齐备后签发可执行行程单；
- `POST /charter/amendments`：封路、人数变化、换车、替班、行程调整、取消；
- `POST /charter/start`、`/charter/complete`：发车与完团；
- `GET /charter/filings/{id}`：联审视图（阻塞、许可链、行程单、修订）；`?view=agency` 为旅行社视角（阻塞原因、退款责任、可沿用审批）；
- `GET /charter/enforcement?certificate_no=&plate=&at=`：执法核验当前有效许可、责任地区与异常处置依据。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令在临时 SQLite 数据库中完成基础登记与旅游包车联审全链路（辖区双方审批、许可齐备签发、发车、封路按范围重审、重审期间旧证仍有效、退款责任口径），成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
