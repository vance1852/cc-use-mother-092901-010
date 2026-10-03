# 闭环整改无障碍设施缺陷协同基础服务

本项目在运营机构、交通节点、操作者与结构化参考资料的共享登记能力之上，提供综合客运枢纽
**无障碍设施缺陷整改与复开服务**：统一登记设施位置、影响人群、风险等级、替代服务、维护合同、
工单、复检证据与责任时限；内置角色权限、请求幂等、SQLite 事务与哈希串联审计。

## 目录

- `src/transport_coordination/`：基础登记、SQLite 存储、权限服务、审计链、HTTP 路由、
  无障碍整改复开领域服务（`accessibility.py`）和离线验收；
- `tests/`：基础规则、无障碍整改链、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 核心规则

1. **报修即冻结**：报修时按设施影响自动升级风险（唯一无障碍通道 + 行动不便人群至少升至
   `high`），冻结当时生效维护合同的责任机构与 SLA 时限；无生效合同不允许报修。
2. **一条事件链，不许新工单掩盖**：重复报修挂原事件并升级；质保期（复检通过后 30 天，可由
   合同 SLA 覆盖）内复发重开同一事件链，原验收依据立即失效；复检不合格返工、证据撤回返工、
   跨单位移交全部以新工单延续原链（`continued_from_work_order_id`），原响应/维修时限不变。
3. **临时替代措施带到期与交接**：必须有 `valid_to` 与 `handover_rule`，单次最长 72 小时；
   唯一无障碍通道仅张贴告示会被拒绝；续设必须显式接替旧措施且不得出现空窗；可登记交接对象。
   到期由扫描任务置为 `expired` 并通知值班经理与旅客。
4. **独立复检才能复开**：维修完成进入待复检；复检人必须是 `reviewer` 且不属于任何承接过该
   事件的维护机构，须提交至少一份带哈希的证据；通过才关闭、结束替代措施并进入质保期。
5. **证据撤回**：复检证据可撤回；若该复检曾是关单依据，复开立即作废、设施重新停用、原链重开。
6. **跨单位移交**：仅管理员可执行，须指向覆盖该设施的有效合同；冻结责任方与原 SLA 时钟不变，
   历史逾期责任仍归原机构，新机构只承接剩余时限。
7. **逾期与升级**：响应、替代措施、维修、复检四个环节分别计时，扫描任务（`POST /sweeps`）
   对首次逾期升级并通知值班经理、责任机构，升级 3 级后通知监管方；同一环节重复扫描不重复升级。
8. **时点还原**：监管审计可按任意日期还原设施可用性、生效替代承诺、通知对象、验收依据与
   复发责任——全部由事件链重放得出，而非读取会被覆盖的当前状态。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/facilities` | 登记无障碍设施（类型、位置、影响人群、是否唯一无障碍通道） |
| POST | `/maintenance-contracts` | 登记维护合同（机构、有效期、设施专属或场所通用、自定义 SLA） |
| POST | `/defect-reports` | 报修；自动判定 new / repeat / recurrence 三种模式 |
| POST | `/work-orders/respond` | 冻结责任方响应工单 |
| POST | `/temporary-measures` | 启用临时替代措施（续设须带 `supersedes_measure_id`） |
| POST | `/temporary-measures/handover` | 替代措施交接 |
| POST | `/work-orders/complete` | 维修完成，进入待独立复检 |
| POST | `/reinspections` | 独立复检结论（pass/fail + 证据） |
| POST | `/reinspections/withdraw` | 撤回复检证据（曾为关单依据则重开） |
| POST | `/incidents/transfer` | 跨单位移交（管理员） |
| POST | `/sweeps` | 到期/逾期扫描，幂等 |
| GET | `/routes?site_id=` | 值班经理：当前可用路线、替代路线、不可用节点 |
| GET | `/expiring-commitments?site_id=&within_hours=` | 即将失效的替代承诺 |
| GET | `/overdue?site_id=` | 每个逾期环节、逾期分钟与责任机构 |
| GET | `/incidents?id=` | 完整事件链（工单/措施/复检/通知/逾期标记） |
| GET | `/facility-snapshot?facility_id=&at=` | 按任一日期还原 |

基础接口保持不变：`/organizations`、`/actors`、`/sites`、`/domain-records`、`/audit-events`、
`/health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写操作要求 `request_id` 幂等。

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

验收在临时 SQLite 数据库中走完"报修自动升级 → 重复报修 → 替代措施 → 独立复检复开 →
质保内复发 → 跨单位移交 → 再次复检 → 证据撤回重开 → 逾期扫描升级 → 多日期审计还原"
的完整链条，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。服务重启后 SQLite 中的业务状态、事件链与审计历史继续保留，
历史时点还原结果不受影响。
