"""运行基础服务与无障碍设施缺陷整改复开的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .accessibility import AccessibilityService
from .clock import FixedClock
from .service import DomainService
from .storage import Database

BASE = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def iso(**delta) -> str:
    return (BASE + timedelta(**delta)).isoformat().replace("+00:00", "Z")


def run() -> dict[str, object]:
    """执行完整登记与缺陷整改链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(BASE)
        service = DomainService(database, clock)
        a11y = AccessibilityService(service)

        # 基础登记：枢纽管理方、两家维护机构、角色与场所
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="hub", name="示范枢纽")
        service.register_organization(request_id="req-org2", actor_id="bootstrap",
                                      organization_id="maint-a", name="甲电梯维保")
        service.register_organization(request_id="req-org3", actor_id="bootstrap",
                                      organization_id="maint-b", name="乙电梯维保")
        service.register_actor(request_id="req-admin", actor_id="bootstrap",
                               new_actor_id="admin-001", display_name="系统管理员",
                               role="admin", organization_id="hub")
        service.register_actor(request_id="req-operator", actor_id="admin-001",
                               new_actor_id="op-001", display_name="值班员",
                               role="operator", organization_id="hub")
        service.register_actor(request_id="req-inspector", actor_id="admin-001",
                               new_actor_id="rev-001", display_name="独立复检员",
                               role="reviewer", organization_id="hub")
        service.register_actor(request_id="req-tech-a", actor_id="admin-001",
                               new_actor_id="tech-a", display_name="甲维保技工",
                               role="operator", organization_id="maint-a")
        service.register_actor(request_id="req-tech-b", actor_id="admin-001",
                               new_actor_id="tech-b", display_name="乙维保技工",
                               role="operator", organization_id="maint-b")
        service.register_site(request_id="req-site", actor_id="op-001", site_id="hub-s1",
                              organization_id="hub", name="一号综合客运枢纽",
                              timezone_name="Asia/Shanghai")

        # 无障碍设施（唯一无障碍通道）与两份维护合同
        a11y.register_facility(
            request_id="req-facility", actor_id="op-001", site_id="hub-s1",
            facility_id="elev-001", facility_type="accessible_elevator",
            location="一层站台无障碍电梯", description="站台至站厅唯一无障碍电梯",
            affected_groups=["wheelchair", "mobility_impaired"],
            sole_accessible_route=True)
        a11y.register_maintenance_contract(
            request_id="req-contract-a", actor_id="admin-001", site_id="hub-s1",
            contract_id="contract-a", contractor_org_id="maint-a",
            valid_from=iso(days=-90), valid_to=iso(days=275))
        a11y.register_maintenance_contract(
            request_id="req-contract-b", actor_id="admin-001", site_id="hub-s1",
            contract_id="contract-b", contractor_org_id="maint-b",
            valid_from=iso(days=-30), valid_to=iso(days=335))

        # 既有基础资料登记链保持可用
        first = service.record_domain_data(
            request_id="req-data", actor_id="op-001", site_id="hub-s1",
            category="operator_profile", external_key="record-001",
            data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(
            request_id="req-data", actor_id="op-001", site_id="hub-s1",
            category="operator_profile", external_key="record-001",
            data={"name": "基础资料", "enabled": True})

        # 1) 报修：低等级因唯一无障碍通道自动升级为 high，冻结责任方与 SLA
        reported = a11y.report_defect(
            request_id="req-report", actor_id="op-001", facility_id="elev-001",
            severity="low", summary="整改验收后不久再次停用",
            impact={"passengers_stranded": True})
        incident_id = reported["incident_id"]
        assert reported["risk_level"] == "high"
        assert reported["frozen_contractor_org"] == "maint-a"

        # 2) 重复报修挂原链并升级，不产生新工单
        repeated = a11y.report_defect(
            request_id="req-report-repeat", actor_id="op-001", facility_id="elev-001",
            severity="critical", summary="旅客仍只能绕行")
        assert repeated["mode"] == "repeat"

        # 3) 责任方响应、启用带到期与交接规则的真实替代服务
        a11y.respond_to_work_order(request_id="req-respond", actor_id="tech-a",
                                   incident_id=incident_id, summary="到场检修")
        measure = a11y.activate_temporary_measure(
            request_id="req-measure", actor_id="op-001", incident_id=incident_id,
            kind="staff_assist", description="双人护送经东侧缓坡绕行",
            valid_from=iso(hours=0), valid_to=iso(hours=36),
            handover_rule="到期前2小时由站务班与维保联合交接")

        # 4) 维修完成，但必须独立复检通过才复开
        a11y.complete_repair(request_id="req-complete", actor_id="tech-a",
                             incident_id=incident_id, summary="更换门机控制器")
        evidence = [{"evidence_id": "photo-1", "type": "photo",
                     "uri": "file://evidence/run-test.jpg",
                     "sha256": "9f8d3c6a1b7e4f2a8c0d5e6f7a8b9c0d"}]
        a11y.submit_reinspection(request_id="req-pass", actor_id="rev-001",
                                 incident_id=incident_id, result="pass",
                                 evidence=evidence, note="连续运行测试合格")
        closed_view = a11y.get_incident(incident_id)
        assert closed_view["status"] == "closed"

        # 5) 五天后质保期内复发：原事件链重开，旧验收单失效
        clock._value = BASE + timedelta(days=5)
        recurred = a11y.report_defect(
            request_id="req-recur", actor_id="op-001", facility_id="elev-001",
            severity="high", summary="同部位再次停用")
        assert recurred["mode"] == "recurrence"

        # 6) 跨单位移交：冻结责任方不变、原 SLA 时钟不变，新机构沿链承接
        transfer = a11y.transfer_responsibility(
            request_id="req-transfer", actor_id="admin-001", incident_id=incident_id,
            target_contract_id="contract-b", reason="原机构备件与技术能力不足")
        view = a11y.get_incident(incident_id)
        assert view["frozen_contractor_org"] == "maint-a"
        assert view["work_orders"][-1]["contractor_org_id"] == "maint-b"

        # 7) 次日新机构修复，独立复检再次通过
        clock._value = BASE + timedelta(days=6)
        a11y.respond_to_work_order(request_id="req-respond-b", actor_id="tech-b",
                                   incident_id=incident_id, summary="乙维保到场")
        a11y.complete_repair(request_id="req-complete-b", actor_id="tech-b",
                             incident_id=incident_id, summary="更换主板并校准")
        a11y.submit_reinspection(request_id="req-pass-b", actor_id="rev-001",
                                 incident_id=incident_id, result="pass",
                                 evidence=evidence, note="复检合格")
        basis_second = a11y.get_incident(incident_id)["basis_reinspection_id"]

        # 8) 第 8 天证据撤回：曾作为关单依据，复开立即作废，原链重开
        clock._value = BASE + timedelta(days=8)
        withdrawn = a11y.withdraw_reinspection(
            request_id="req-withdraw", actor_id="rev-001",
            reinspection_id=basis_second, reason="发现复检视频缺失，证据不足")
        assert withdrawn["reopened"] is True
        view = a11y.get_incident(incident_id)
        assert view["status"] == "reopened"
        assert view["basis_reinspection_id"] is None

        # 9) 第 10 天替代承诺仍失效，值班经理看板出现逾期；扫描升级且重复扫描幂等
        clock._value = BASE + timedelta(days=10)
        sweep_first = a11y.run_sweep()
        sweep_second = a11y.run_sweep()
        assert sweep_second["escalation_count"] == 0
        board = a11y.overdue_board("hub-s1")
        routes = a11y.available_routes("hub-s1")
        expiring = a11y.expiring_commitments("hub-s1", within_hours=48)

        # 10) 监管审计按不同日期还原设施状态、通知对象与验收依据
        snap_day2 = a11y.snapshot_facility_at("elev-001", iso(days=2))
        snap_recur = a11y.snapshot_facility_at("elev-001", iso(days=5, hours=6))
        snap_day7 = a11y.snapshot_facility_at("elev-001", iso(days=7))
        snap_day9 = a11y.snapshot_facility_at("elev-001", iso(days=9))
        assert snap_day2["availability"] == "available"
        assert snap_recur["availability"] == "unavailable"
        assert snap_recur["incidents"][0]["basis_reinspection"] is None
        assert snap_day7["availability"] == "available"
        assert snap_day9["availability"] == "unavailable"

        valid, event_count = service.verify_audit()
        records = len(service.list_domain_data("hub-s1"))
        final_view = a11y.get_incident(incident_id)
        database.close()
        return {
            "status": "ok",
            "records": records,
            "audit_events": event_count,
            "audit_valid": valid,
            "first_replayed": first.replayed,
            "second_replayed": replay.replayed,
            "incident_id": incident_id,
            "frozen_contractor_org": "maint-a",
            "current_contractor_org": "maint-b",
            "risk_level": "high",
            "work_order_count": len(final_view["work_orders"]),  # 初次/复发/移交/撤回返工
            "recurrence_count": final_view["recurrence_count"],
            "escalation_level": final_view["escalation_level"],
            "first_basis_invalidated_after_recurrence": True,
            "routes_status": routes["routes"][0]["status"],
            "overdue_stages": sorted({i["stage"] for i in board["items"]}),
            "expiring_count": len(expiring["items"]),
            "sweep_escalations": sweep_first["escalation_count"],
            "snapshot_day2": snap_day2["availability"],
            "snapshot_recurrence_day": snap_recur["availability"],
            "snapshot_day7": snap_day7["availability"],
            "snapshot_day9": snap_day9["availability"],
            "notification_count": len(final_view["notifications"]),
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
