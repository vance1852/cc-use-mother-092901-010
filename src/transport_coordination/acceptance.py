"""运行基础服务与无障碍整改复开服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .accessibility import AccessibilityService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    """执行登记链与无障碍缺陷整改闭环，返回核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        started = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        clock = FixedClock(started)
        service = AccessibilityService(database, clock)

        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_organization(request_id="req-org-maint", actor_id="bootstrap",
                                      organization_id="org-maint", name="无障碍设施维保单位")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-maintainer", actor_id="admin-001", new_actor_id="maint-001",
                               display_name="维保工程师", role="operator", organization_id="org-maint")
        service.register_actor(request_id="req-reviewer", actor_id="admin-001", new_actor_id="reviewer-001",
                               display_name="独立复检人", role="reviewer", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # 无障碍缺陷整改复开闭环
        service.register_contract(request_id="req-contract", actor_id="operator-001",
                                  contract_id="contract-001", site_id="site-001",
                                  provider_organization_id="org-maint", title="无障碍电梯年度维保",
                                  contact="400-000-0000", facility_type="elevator",
                                  valid_from="2026-09-01T00:00:00Z",
                                  valid_to="2026-12-31T00:00:00Z")
        service.register_facility(request_id="req-facility", actor_id="operator-001", site_id="site-001",
                                  facility_id="elevator-001", facility_type="elevator",
                                  location_desc="东广场一层无障碍电梯",
                                  affected_groups=["wheelchair", "elderly"])
        reported = service.report_defect(request_id="req-defect", actor_id="operator-001",
                                         facility_id="elevator-001", description="整改后再次停用",
                                         risk_level="medium", impact_groups=["wheelchair"])
        assert reported.response["risk_level"] == "high", "轮椅旅客受影响必须自动升级为高风险"
        event_id = reported.resource_id
        work_order_id = reported.response["work_order_id"]
        chain_id = reported.response["chain_id"]

        service.respond_work_order(request_id="req-respond", actor_id="maint-001",
                                   work_order_id=work_order_id)
        service.commit_interim(request_id="req-interim", actor_id="maint-001", event_id=event_id,
                               kind="staff_assist", description="一层安排专人协助走货梯",
                               expires_at="2026-10-01T18:00:00Z")
        service.complete_work_order(request_id="req-complete", actor_id="maint-001",
                                    work_order_id=work_order_id, repair_summary="更换门机控制器")
        passed = service.submit_reinspection(request_id="req-pass", actor_id="reviewer-001",
                                             event_id=event_id, result="passed",
                                             summary="连续试运行正常", evidence_ref="acceptance-photo-001",
                                             evidence_data={"trials": 10})

        routes_reopened = service.accessible_routes("site-001")
        assert routes_reopened["in_service"][0]["facility_id"] == "elevator-001"

        # 复开依据被撤回：必须沿原链重开，不能用旧验收单关单
        clock = FixedClock(started + timedelta(days=2))
        service.clock = clock
        withdrawn = service.withdraw_evidence(request_id="req-withdraw", actor_id="reviewer-001",
                                              evidence_id=passed.resource_id, reason="发现验收照片失实")
        assert withdrawn.response["new_event_id"], "撤回复开依据必须新开整改环节"
        new_event_id = withdrawn.response["new_event_id"]

        chain = service.chain_view(chain_id)
        assert len(chain["events"]) == 2, "复发/撤回必须延续同一事件链"
        assert chain["events"][0]["frozen_provider_org_id"] == "org-maint"

        # 新环节工单逾期，值班经理看板必须暴露
        clock = FixedClock(started + timedelta(days=10))
        service.clock = clock
        service.sweep()
        board = service.overdue_board("site-001")
        assert board["items"] and board["items"][0]["event_id"] == new_event_id

        # 监管审计：复开当天的历史快照应显示在用且验收依据有效
        snapshot = service.facility_as_of("elevator-001", "2026-10-01T20:00:00Z")
        assert snapshot["status"] == "in_service"
        assert snapshot["acceptance_basis"]["evidence_ref"] == "acceptance-photo-001"
        assert snapshot["acceptance_basis"]["state_at"] == "valid"

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "risk_escalated": reported.response["risk_level"],
                  "chain_events": len(chain["events"]),
                  "overdue_items": len(board["items"]),
                  "history_status_at_reopen": snapshot["status"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
