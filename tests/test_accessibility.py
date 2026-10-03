"""无障碍设施缺陷整改与复开服务的规则测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.accessibility import AccessibilityService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.storage import Database


class MutableClock:
    """测试用可推进时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)

    def iso(self) -> str:
        return self.value.isoformat().replace("+00:00", "Z")


class AccessibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = AccessibilityService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org-hub", actor_id="bootstrap",
                                organization_id="hub", name="枢纽管理方")
        s.register_organization(request_id="org-a", actor_id="bootstrap",
                                organization_id="prov-a", name="维保单位甲")
        s.register_organization(request_id="org-b", actor_id="bootstrap",
                                organization_id="prov-b", name="维保单位乙")
        s.register_actor(request_id="actor-admin", actor_id="bootstrap", new_actor_id="admin1",
                         display_name="管理员", role="admin", organization_id="hub")
        s.register_actor(request_id="actor-mgr", actor_id="admin1", new_actor_id="mgr1",
                         display_name="值班经理", role="operator", organization_id="hub")
        s.register_actor(request_id="actor-a", actor_id="admin1", new_actor_id="pa1",
                         display_name="甲维保员", role="operator", organization_id="prov-a")
        s.register_actor(request_id="actor-b", actor_id="admin1", new_actor_id="pb1",
                         display_name="乙维保员", role="operator", organization_id="prov-b")
        s.register_actor(request_id="actor-rev", actor_id="admin1", new_actor_id="rev1",
                         display_name="独立复检员", role="reviewer", organization_id="hub")
        s.register_actor(request_id="actor-rev-a", actor_id="admin1", new_actor_id="reva",
                         display_name="甲自检员", role="reviewer", organization_id="prov-a")
        s.register_site(request_id="site-1", actor_id="mgr1", site_id="site1",
                        organization_id="hub", name="综合客运枢纽", timezone_name="Asia/Shanghai")
        s.register_contract(request_id="contract-a", actor_id="mgr1", contract_id="contract-a",
                            site_id="site1", provider_organization_id="prov-a",
                            title="电梯维保合同甲", contact="400-1", facility_type="elevator",
                            valid_from="2026-09-01T00:00:00Z", valid_to="2026-12-31T00:00:00Z")
        s.register_contract(request_id="contract-b", actor_id="mgr1", contract_id="contract-b",
                            site_id="site1", provider_organization_id="prov-b",
                            title="电梯维保合同乙", contact="400-2", facility_type="elevator",
                            valid_from="2026-09-01T00:00:00Z", valid_to="2026-12-31T00:00:00Z")
        s.register_facility(request_id="facility-elev", actor_id="mgr1", site_id="site1",
                            facility_id="elev1", facility_type="elevator",
                            location_desc="东广场一层无障碍电梯",
                            affected_groups=["wheelchair", "elderly"])
        s.register_facility(request_id="facility-ramp", actor_id="mgr1", site_id="site1",
                            facility_id="ramp1", facility_type="ramp",
                            location_desc="西广场坡道", affected_groups=["wheelchair"])

    def tearDown(self) -> None:
        self.database.close()

    def _event(self, event_id: str):
        return self.database.connection.execute(
            "SELECT * FROM defect_events WHERE event_id=?", (event_id,)
        ).fetchone()

    def _latest_wo(self, event_id: str):
        return self.database.connection.execute(
            "SELECT * FROM work_orders WHERE event_id=? ORDER BY seq DESC LIMIT 1", (event_id,)
        ).fetchone()

    def _report(self, request_id="rep", **overrides):
        params = {"actor_id": "mgr1", "facility_id": "elev1", "description": "电梯停运",
                  "risk_level": "medium", "impact_groups": ["wheelchair"]}
        params.update(overrides)
        return self.service.report_defect(request_id=request_id, **params)

    # ---- 报修与升级 ----

    def test_report_without_active_contract_is_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.report_defect(request_id="r1", actor_id="mgr1", facility_id="ramp1",
                                       description="坡道破损", risk_level="low")

    def test_wheelchair_impact_auto_escalates_to_high(self):
        receipt = self._report()
        event = self._event(receipt.resource_id)
        self.assertEqual("high", event["risk_level"])
        self.assertEqual("medium", event["reported_risk_level"])
        self.assertEqual(1, event["escalated"])
        self.assertEqual("prov-a", event["frozen_provider_org_id"])
        self.assertEqual("contract-a", event["frozen_contract_id"])
        self.assertEqual("out_of_service", self.database.connection.execute(
            "SELECT status FROM accessibility_facilities WHERE facility_id='elev1'").fetchone()["status"])

    def test_low_impact_without_mobility_group_is_not_escalated(self):
        receipt = self._report(request_id="r2", risk_level="low",
                               impact_groups=["visual_impaired"])
        self.assertEqual("low", self._event(receipt.resource_id)["risk_level"])

    def test_sla_deadlines_follow_risk_level(self):
        receipt = self._report()
        wo = self._latest_wo(receipt.resource_id)
        self.assertEqual("2026-10-01T10:00:00Z", wo["due_response_at"])  # high: 2h
        self.assertEqual("2026-10-02T08:00:00Z", wo["due_repair_at"])    # high: 24h

    def test_repeat_report_attaches_to_open_event_without_new_work_order(self):
        first = self._report(request_id="r3")
        before = self.database.connection.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE event_id=?", (first.resource_id,)).fetchone()["c"]
        self.clock.advance(hours=1)
        repeat = self.service.report_defect(
            request_id="r4", actor_id="mgr1", facility_id="elev1",
            description="仍未恢复", risk_level="high", impact_groups=["wheelchair"])
        self.assertEqual(first.resource_id, repeat.resource_id)
        self.assertTrue(repeat.response["attached"] == "open_event")
        after = self.database.connection.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE event_id=?", (first.resource_id,)).fetchone()["c"]
        self.assertEqual(before, after)
        self.assertEqual(1, self._event(first.resource_id)["repeat_count"])

    def test_repeat_report_with_higher_risk_tightens_deadlines_without_reset(self):
        first = self._report(request_id="rep-low", risk_level="low",
                             impact_groups=["visual_impaired"])
        wo = self._latest_wo(first.resource_id)
        original_response = wo["due_response_at"]
        original_repair = wo["due_repair_at"]
        self.clock.advance(hours=2)
        repeat = self.service.report_defect(
            request_id="rep-crit", actor_id="mgr1", facility_id="elev1",
            description="轮椅旅客也受阻", risk_level="critical",
            impact_groups=["wheelchair"])
        self.assertEqual(first.resource_id, repeat.resource_id)
        wo2 = self._latest_wo(first.resource_id)
        self.assertEqual("critical", self._event(first.resource_id)["risk_level"])
        # 时限以原工单创建时刻为基准收紧，不会因重复报修重新计时
        self.assertGreater(original_response, wo2["due_response_at"])
        self.assertGreater(original_repair, wo2["due_repair_at"])
        self.assertEqual("2026-10-01T09:00:00Z", wo2["due_response_at"])
        self.assertEqual("2026-10-01T12:00:00Z", wo2["due_repair_at"])

    def test_overdue_before_transfer_stays_attributed_to_old_provider(self):
        report = self._report(request_id="rep-to")
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="to-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        measure = self.service.commit_interim(
            request_id="to-m", actor_id="pa1", event_id=report.resource_id, kind="alt_route",
            description="南坡道", expires_at="2026-10-01T18:00:00Z").resource_id
        self.clock.advance(hours=25)  # 修复时限已逾期
        self.service.transfer_responsibility(
            request_id="to-t", actor_id="mgr1", event_id=report.resource_id,
            to_organization_id="prov-b", to_contract_id="contract-b",
            measure_handovers=[{"measure_id": measure,
                                "expires_at": (self.clock.value + timedelta(hours=4)).isoformat()
                                .replace("+00:00", "Z")}],
            reason="甲方逾期，换人")
        result = self.service.sweep()
        self.assertGreaterEqual(result["overdue_work_orders"], 1)
        board = self.service.overdue_board("site1")["items"][0]
        repair_breaches = [b for b in board["breaches"] if b["stage"] == "repair"]
        self.assertTrue(repair_breaches)
        # 逾期的旧工单记在甲方头上；当前责任方是乙方
        self.assertEqual({"prov-a"}, {b["provider_organization_id"] for b in repair_breaches})
        self.assertEqual("prov-b", board["current_provider_org_id"])

    # ---- 维修、临时措施、复检 ----

    def test_only_frozen_provider_may_respond(self):
        report = self._report()
        wo = self._latest_wo(report.resource_id)
        with self.assertRaises(PermissionDenied):
            self.service.respond_work_order(request_id="x1", actor_id="pb1",
                                            work_order_id=wo["work_order_id"])
        self.service.respond_work_order(request_id="x2", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])

    def test_interim_measure_requires_expiry_and_respects_limit(self):
        report = self._report()
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="x3", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        with self.assertRaises(ValidationError):
            self.service.commit_interim(request_id="x4", actor_id="pa1",
                                        event_id=report.resource_id, kind="alt_route",
                                        description="绕行", expires_at=self.clock.iso())
        with self.assertRaises(ConflictError):  # high 等级临时措施上限 12 小时
            self.service.commit_interim(request_id="x5", actor_id="pa1",
                                        event_id=report.resource_id, kind="alt_route",
                                        description="绕行", expires_at="2026-10-03T08:00:00Z")
        self.service.commit_interim(request_id="x6", actor_id="pa1",
                                    event_id=report.resource_id, kind="staff_assist",
                                    description="专人护送", expires_at="2026-10-01T18:00:00Z")

    def test_repair_cannot_reopen_without_independent_reinspection(self):
        report = self._report()
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="x7", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        with self.assertRaises(PermissionDenied):  # 维护方自己的复检员
            self.service.submit_reinspection(request_id="x8", actor_id="reva",
                                             event_id=report.resource_id, result="passed",
                                             summary="修好", evidence_ref="p1", evidence_data={})
        with self.assertRaises(ConflictError):  # 尚未申报完工
            self.service.submit_reinspection(request_id="x9", actor_id="rev1",
                                             event_id=report.resource_id, result="passed",
                                             summary="修好", evidence_ref="p1", evidence_data={})

    def test_full_repair_reinspection_reopen_flow(self):
        report = self._report()
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="rep-a", actor_id="pa1", work_order_id=wo["work_order_id"])
        self.clock.advance(hours=3)
        self.service.complete_work_order(request_id="rep-b", actor_id="pa1",
                                         work_order_id=wo["work_order_id"], repair_summary="更换门机")
        passed = self.service.submit_reinspection(
            request_id="rep-c", actor_id="rev1", event_id=report.resource_id, result="passed",
            summary="运行正常", evidence_ref="photo-1", evidence_data={"trials": 10})
        self.assertEqual("closed", self._event(report.resource_id)["status"])
        self.assertEqual("in_service", self.database.connection.execute(
            "SELECT status FROM accessibility_facilities WHERE facility_id='elev1'").fetchone()["status"])
        self.assertEqual(passed.resource_id, self._event(report.resource_id)["closed_evidence_id"])

    def test_failed_reinspection_opens_new_work_order_and_keeps_old_one(self):
        report = self._report()
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="rep-d", actor_id="pa1", work_order_id=wo["work_order_id"])
        self.service.complete_work_order(request_id="rep-e", actor_id="pa1",
                                         work_order_id=wo["work_order_id"], repair_summary="尝试修复")
        failed = self.service.submit_reinspection(
            request_id="rep-f", actor_id="rev1", event_id=report.resource_id, result="failed",
            summary="仍有异响", evidence_ref="photo-2", evidence_data={})
        self.assertEqual("open", self._event(report.resource_id)["status"])
        self.assertIn("new_work_order_id", failed.response)
        statuses = [(row["seq"], row["status"]) for row in self.database.connection.execute(
            "SELECT seq,status FROM work_orders WHERE event_id=? ORDER BY seq",
            (report.resource_id,))]
        self.assertEqual([(1, "rejected"), (2, "open")], statuses)

    # ---- 复发与证据撤回 ----

    def _close_first_event(self, report, req_prefix="z"):
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id=f"{req_prefix}-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        self.service.complete_work_order(request_id=f"{req_prefix}-c", actor_id="pa1",
                                         work_order_id=wo["work_order_id"], repair_summary="修复")
        return self.service.submit_reinspection(
            request_id=f"{req_prefix}-p", actor_id="rev1", event_id=report.resource_id,
            result="passed", summary="通过", evidence_ref="ev", evidence_data={})

    def test_recurrence_continues_same_chain(self):
        first = self._report(request_id="rep-g")
        self._close_first_event(first, "g")
        self.clock.advance(days=4)
        again = self._report(request_id="g2")
        self.assertEqual(self._event(first.resource_id)["chain_id"],
                         self._event(again.resource_id)["chain_id"])
        second = self._event(again.resource_id)
        self.assertEqual("recurrence", second["report_type"])
        self.assertEqual(2, second["chain_seq"])
        self.assertEqual(first.resource_id, second["parent_event_id"])

    def test_withdrawn_closing_evidence_reopens_facility_on_same_chain(self):
        first = self._report(request_id="rep-h")
        passed = self._close_first_event(first, "h")
        self.clock.advance(days=1)
        receipt = self.service.withdraw_evidence(
            request_id="h-w", actor_id="rev1", evidence_id=passed.resource_id, reason="照片不实")
        new_event_id = receipt.response["new_event_id"]
        self.assertIsNotNone(new_event_id)
        new_event = self._event(new_event_id)
        self.assertEqual("evidence_withdrawn", new_event["report_type"])
        self.assertEqual(self._event(first.resource_id)["chain_id"], new_event["chain_id"])
        self.assertEqual("out_of_service", self.database.connection.execute(
            "SELECT status FROM accessibility_facilities WHERE facility_id='elev1'").fetchone()["status"])
        self.assertEqual(new_event_id, self.database.connection.execute(
            "SELECT current_event_id FROM accessibility_facilities WHERE facility_id='elev1'").fetchone()[
            "current_event_id"])

    def test_withdraw_unrelated_evidence_does_not_spawn_event(self):
        first = self._report(request_id="rep-i")
        wo = self._latest_wo(first.resource_id)
        self.service.respond_work_order(request_id="i-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        self.service.complete_work_order(request_id="i-c", actor_id="pa1",
                                         work_order_id=wo["work_order_id"], repair_summary="x")
        failed = self.service.submit_reinspection(
            request_id="i-f", actor_id="rev1", event_id=first.resource_id, result="failed",
            summary="不合格", evidence_ref="bad", evidence_data={})
        receipt = self.service.withdraw_evidence(
            request_id="i-w", actor_id="rev1", evidence_id=failed.resource_id, reason="误传")
        self.assertIsNone(receipt.response["new_event_id"])
        self.assertEqual("withdrawn", self.database.connection.execute(
            "SELECT status FROM reinspection_evidence WHERE evidence_id=?",
            (failed.resource_id,)).fetchone()["status"])

    # ---- 移交与交接 ----

    def test_transfer_requires_handover_of_every_active_measure(self):
        report = self._report(request_id="rep-j")
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="j-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        self.service.commit_interim(request_id="j-m", actor_id="pa1",
                                    event_id=report.resource_id, kind="alt_route",
                                    description="南坡道", expires_at="2026-10-01T18:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.transfer_responsibility(
                request_id="j-t", actor_id="mgr1", event_id=report.resource_id,
                to_organization_id="prov-b", to_contract_id="contract-b",
                measure_handovers=[], reason="甲方退出")

    def test_transfer_hands_over_measures_and_freezes_new_provider_on_new_work_order(self):
        report = self._report(request_id="rep-k")
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="k-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        measure = self.service.commit_interim(
            request_id="k-m", actor_id="pa1", event_id=report.resource_id, kind="alt_route",
            description="南坡道", expires_at="2026-10-01T18:00:00Z").resource_id
        transfer = self.service.transfer_responsibility(
            request_id="k-t", actor_id="mgr1", event_id=report.resource_id,
            to_organization_id="prov-b", to_contract_id="contract-b",
            measure_handovers=[{"measure_id": measure,
                                "description": "乙方继续引导南坡道",
                                "expires_at": "2026-10-01T20:00:00Z"}],
            reason="甲方退出")
        # 事件上的冻结责任方不可变
        self.assertEqual("prov-a", self._event(report.resource_id)["frozen_provider_org_id"])
        # 旧工单留痕、新工单归属乙方
        statuses = [(row["seq"], row["status"], row["provider_organization_id"])
                    for row in self.database.connection.execute(
                        "SELECT seq,status,provider_organization_id FROM work_orders "
                        "WHERE event_id=? ORDER BY seq", (report.resource_id,))]
        self.assertEqual([(1, "transferred", "prov-a"), (2, "open", "prov-b")], statuses)
        # 旧责任方被冻结出局，乙方可操作
        with self.assertRaises(PermissionDenied):
            self.service.respond_work_order(request_id="k-x", actor_id="pa1",
                                            work_order_id=transfer.response["new_work_order_id"])
        self.service.respond_work_order(request_id="k-y", actor_id="pb1",
                                        work_order_id=transfer.response["new_work_order_id"])
        # 临时措施：旧的 handed_over，新的 active 且归属乙方
        measures = [(row["status"], row["provider_organization_id"], row["handed_to_org_id"])
                    for row in self.database.connection.execute(
                        "SELECT status,provider_organization_id,handed_to_org_id "
                        "FROM interim_measures WHERE event_id=? ORDER BY effective_at",
                        (report.resource_id,))]
        self.assertEqual([("handed_over", "prov-a", "prov-b"), ("active", "prov-b", None)], measures)

    def test_provider_cannot_transfer_to_itself_or_self_transfer(self):
        report = self._report(request_id="rep-l")
        with self.assertRaises(PermissionDenied):
            self.service.transfer_responsibility(
                request_id="l-t1", actor_id="pa1", event_id=report.resource_id,
                to_organization_id="prov-b", to_contract_id="contract-b", reason="x")
        with self.assertRaises(ConflictError):
            self.service.transfer_responsibility(
                request_id="l-t2", actor_id="mgr1", event_id=report.resource_id,
                to_organization_id="prov-a", to_contract_id="contract-a", reason="x")

    # ---- 巡检、逾期、经理视图 ----

    def test_sweep_marks_overdue_and_expired(self):
        report = self._report(request_id="rep-m")
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="m-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        self.service.commit_interim(request_id="m-m", actor_id="pa1",
                                    event_id=report.resource_id, kind="alt_route",
                                    description="南坡道", expires_at="2026-10-01T12:00:00Z")
        self.clock.advance(hours=25)  # 超过修复 24h 与措施 12h 上限
        result = self.service.sweep()
        self.assertEqual(1, result["overdue_work_orders"])
        self.assertEqual(1, result["expired_measures"])
        board = self.service.overdue_board("site1")
        stages = {breach["stage"] for breach in board["items"][0]["breaches"]}
        self.assertIn("repair", stages)
        self.assertIn("interim_expired", stages)
        routes = self.service.accessible_routes("site1")
        self.assertEqual("elev1", routes["out_of_service"][0]["facility_id"])
        self.assertFalse(routes["out_of_service"][0]["has_valid_alternative"])

    def test_expiring_commitments_lists_active_measures_within_window(self):
        report = self._report(request_id="rep-n")
        wo = self._latest_wo(report.resource_id)
        self.service.respond_work_order(request_id="n-r", actor_id="pa1",
                                        work_order_id=wo["work_order_id"])
        self.service.commit_interim(request_id="n-m", actor_id="pa1",
                                    event_id=report.resource_id, kind="alt_route",
                                    description="南坡道", expires_at="2026-10-01T18:00:00Z")
        items = self.service.expiring_commitments("site1", within_hours=24)["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("南坡道", items[0]["description"])
        self.assertEqual([], self.service.expiring_commitments("site1", within_hours=2)["items"])

    # ---- 历史还原 ----

    def test_facility_as_of_reconstructs_notifications_basis_and_responsibility(self):
        first = self._report(request_id="rep-o")
        passed = self._close_first_event(first, "o")
        at_reopen = self.clock.iso()
        self.clock.advance(days=2)
        self._report(request_id="o2")

        # 复开期间：在用、验收依据有效
        snap = self.service.facility_as_of("elev1", at_reopen)
        self.assertEqual("in_service", snap["status"])
        self.assertEqual(passed.resource_id, snap["acceptance_basis"]["evidence_id"])
        self.assertEqual("valid", snap["acceptance_basis"]["state_at"])
        self.assertIsNone(snap["open_event"])
        reasons = {n["reason"] for n in snap["notifications"]}
        self.assertIn("new_defect", reasons)
        self.assertIn("facility_reopened", reasons)

        # 复发后：停用、同一链两个环节，复发责任指向维保方
        snap2 = self.service.facility_as_of("elev1", self.clock.iso())
        self.assertEqual("out_of_service", snap2["status"])
        self.assertIsNone(snap2["acceptance_basis"])
        self.assertEqual(2, len(snap2["chains"]))
        self.assertEqual("prov-a", snap2["chains"][1]["recurrence_responsibility"])

    def test_history_before_facility_exists_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.facility_as_of("elev1", "2026-09-01T00:00:00Z")

    # ---- 幂等 ----

    def test_replayed_request_does_not_duplicate_work_orders(self):
        first = self.service.report_defect(
            request_id="idem-1", actor_id="mgr1", facility_id="elev1",
            description="停运", risk_level="high", impact_groups=["wheelchair"])
        replay = self.service.report_defect(
            request_id="idem-1", actor_id="mgr1", facility_id="elev1",
            description="停运", risk_level="high", impact_groups=["wheelchair"])
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE event_id=?", (first.resource_id,)).fetchone()["c"])
        self.assertEqual(first.response["work_order_id"], replay.response["work_order_id"])

    def test_audit_chain_stays_valid(self):
        first = self._report(request_id="rep-q")
        self._close_first_event(first, "q")
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 5)


if __name__ == "__main__":
    unittest.main()
