# 闭环整改无障碍设施缺陷协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

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

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 无障碍设施缺陷整改与复开

`AccessibilityService` 在基础服务之上实现完整闭环，写入接口同样通过 `request_id` 幂等：

- `POST /facilities`、`POST /maintenance-contracts`：登记设施位置、影响人群和带有效期/SLA 的维护合同；
- `POST /defects`：报修，按受影响人群自动升级风险（影响轮椅/助行旅客最低按高风险），并把维护单位、合同与 SLA 冻结在事件上；设施仍在整改时重复报修只累加在当前事件，不另开工单；
- `POST /work-orders/respond`、`POST /interim-measures`、`POST /work-orders/complete`：响应、临时措施（必须带到期时间且不得超过该等级时限上限）与完工申报；
- `POST /reinspections`：独立复检（复检人组织必须不同于当前维护单位）通过后才复开；驳回则旧工单留 `rejected`、同事件续开新工单；
- `POST /evidence/withdraw`：撤回复开依据会沿原事件链新开 `evidence_withdrawn` 环节并重新停用设施；
- `POST /responsibility-transfers`：跨单位移交，旧工单留 `transferred` 痕迹、新工单归属新责任方，生效临时措施必须逐项交接（旧措施 `handed_over`、新措施重新计时）；
- `POST /sweep`：巡检落戳过期承诺与逾期工单；
- 值班视图：`GET /accessible-routes`（当前可用路线与仍有效的替代承诺）、`GET /expiring-commitments`、`GET /overdue-board`（每个逾期环节及其责任单位）；
- 监管还原：`GET /defect-chain?chain_id=` 还原整条事件链，`GET /facility-history?facility_id=&at=` 按任意时刻还原设施状态、通知对象、验收依据（含是否后来被撤回）与复发责任。

复发、证据撤回和移交都不会产生脱离历史的新工单：每个环节的工单不可变，逾期永久留在当时责任单位的工单上。
