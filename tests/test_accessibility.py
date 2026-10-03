import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.accessibility import AccessibilityService, derive_risk, merge_sla
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

BASE = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def iso(**delta):
    return (BASE + timedelta(**delta)).isoformat().replace("+00:00", "Z")


EVIDENCE = [{"evidence_id": "e1", "type": "photo", "uri": "file://e1.jpg", "sha256": "deadbeef"}]


class AccessibilityCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.domain = DomainService(self.database, self.clock)
        self.a11y = AccessibilityService(self.domain)
        self.domain.register_organization(request_id="org-hub", actor_id="bootstrap",
                                          organization_id="hub", name="枢纽管理方")
        self.domain.register_organization(request_id="org-m1", actor_id="bootstrap",
                                          organization_id="maint", name="一维保")
        self.domain.register_organization(request_id="org-m2", actor_id="bootstrap",
                                          organization_id="maint2", name="二维保")
        self.domain.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="adm",
                                   display_name="管理员", role="admin", organization_id="hub")
        self.domain.register_actor(request_id="op", actor_id="adm", new_actor_id="op",
                                   display_name="站务员", role="operator", organization_id="hub")
        self.domain.register_actor(request_id="rev", actor_id="adm", new_actor_id="rev",
                                   display_name="复检员", role="reviewer", organization_id="hub")
        self.domain.register_actor(request_id="rev2", actor_id="adm", new_actor_id="rev2",
                                   display_name="维保机构内审员", role="reviewer",
                                   organization_id="maint")
        self.domain.register_actor(request_id="mop", actor_id="adm", new_actor_id="mop",
                                   display_name="一维保工", role="operator",
                                   organization_id="maint")
        self.domain.register_actor(request_id="mop2", actor_id="adm", new_actor_id="mop2",
                                   display_name="二维保工", role="operator",
                                   organization_id="maint2")
        self.domain.register_site(request_id="site", actor_id="op", site_id="s1",
                                  organization_id="hub", name="综合客运枢纽",
                                  timezone_name="Asia/Shanghai")
        self.a11y.register_facility(
            request_id="fac", actor_id="op", site_id="s1", facility_id="elev-1",
            facility_type="accessible_elevator", location="一层无障碍电梯",
            description="站台至站厅", affected_groups=["wheelchair", "mobility_impaired"],
            sole_accessible_route=True)
        self.a11y.register_facility(
            request_id="fac2", actor_id="op", site_id="s1", facility_id="ramp-1",
            facility_type="ramp", location="东广场坡道", description="缓坡",
            affected_groups=["wheelchair"], sole_accessible_route=False)
        self.a11y.register_maintenance_contract(
            request_id="ct1", actor_id="adm", site_id="s1", contract_id="c-maint",
            contractor_org_id="maint", valid_from=iso(days=-30), valid_to=iso(days=300))
        self.a11y.register_maintenance_contract(
            request_id="ct2", actor_id="adm", site_id="s1", contract_id="c-maint2",
            contractor_org_id="maint2", valid_from=iso(days=-30), valid_to=iso(days=300))

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 登记与规则

    def test_unknown_facility_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.a11y.register_facility(
                request_id="f-bad", actor_id="op", site_id="s1", facility_id="x",
                facility_type="escalator", location="l", description="d",
                affected_groups=["wheelchair"])

    def test_unknown_affected_group_rejected(self):
        with self.assertRaises(ValidationError):
            self.a11y.register_facility(
                request_id="f-bad", actor_id="op", site_id="s1", facility_id="x",
                facility_type="ramp", location="l", description="d",
                affected_groups=["wheelchair", "pets"])

    def test_report_without_active_contract_rejected(self):
        self.domain.register_site(request_id="site2", actor_id="op", site_id="s2",
                                  organization_id="hub", name="无合同场所",
                                  timezone_name="Asia/Shanghai")
        self.a11y.register_facility(
            request_id="f-noc", actor_id="op", site_id="s2", facility_id="elev-noc",
            facility_type="platform_lift", location="北门", description="无合同梯",
            affected_groups=["wheelchair"], sole_accessible_route=False)
        with self.assertRaises(ValidationError):
            self.a11y.report_defect(request_id="rep-noc", actor_id="op",
                                   facility_id="elev-noc", severity="high", summary="坏了")

    def test_severity_auto_escalates_for_sole_route_and_mobility_groups(self):
        self.assertEqual(derive_risk("low", True, frozenset({"wheelchair"})), ("high", "high"))
        self.assertEqual(derive_risk("low", False, frozenset({"mobility_impaired"})),
                         ("medium", "medium"))
        self.assertEqual(derive_risk("low", False, frozenset({"visual_impaired"})),
                         ("low", "low"))

    def test_contract_sla_merge_validates(self):
        sla = merge_sla({"repair_hours": {"critical": 2}, "reinspection_hours": 12})
        self.assertEqual(sla["repair_hours"]["critical"], 2)
        self.assertEqual(sla["repair_hours"]["high"], 24)
        with self.assertRaises(ValidationError):
            merge_sla({"repair_hours": {"critical": 0}})
        with self.assertRaises(ValidationError):
            merge_sla({"unknown_field": 1})

    # ------------------------------------------------------------ 报修链

    def _open_incident(self, request_id="rep1", severity="low"):
        report = self.a11y.report_defect(
            request_id=request_id, actor_id="op", facility_id="elev-1",
            severity=severity, summary="电梯停用")
        return report["incident_id"]

    def test_report_freezes_contractor_and_escalates_risk(self):
        report = self.a11y.report_defect(
            request_id="rep1", actor_id="op", facility_id="elev-1",
            severity="low", summary="电梯停用")
        self.assertEqual("high", report["risk_level"])
        self.assertEqual("maint", report["frozen_contractor_org"])
        incident = self.a11y.get_incident(report["incident_id"])
        self.assertEqual("maint", incident["frozen_contractor_org"])
        self.assertEqual("c-maint", incident["frozen_contract_id"])
        self.assertEqual("unavailable",
                         self.a11y.available_routes("s1")["routes"][0]["status"])

    def test_report_is_idempotent(self):
        first = self.a11y.report_defect(
            request_id="rep1", actor_id="op", facility_id="elev-1",
            severity="low", summary="电梯停用")
        second = self.a11y.report_defect(
            request_id="rep1", actor_id="op", facility_id="elev-1",
            severity="low", summary="电梯停用")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["incident_id"], second["incident_id"])

    def test_repeat_report_continues_chain_and_escalates(self):
        incident_id = self._open_incident()
        repeat = self.a11y.report_defect(
            request_id="rep2", actor_id="op", facility_id="elev-1",
            severity="critical", summary="仍然停用")
        self.assertEqual("repeat", repeat["mode"])
        self.assertEqual(incident_id, repeat["incident_id"])
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("critical", view["risk_level"])
        self.assertEqual(1, view["escalation_level"])
        self.assertEqual(1, len(view["work_orders"]))  # 不允许新工单掩盖
        kinds = [e["event_type"] for e in view["timeline"]]
        self.assertIn("defect.repeat_reported", kinds)

    def test_only_frozen_contractor_may_respond(self):
        incident_id = self._open_incident()
        with self.assertRaises(PermissionDenied):
            self.a11y.respond_to_work_order(
                request_id="bad-respond", actor_id="mop2", incident_id=incident_id,
                summary="越权响应")
        self.a11y.respond_to_work_order(
            request_id="respond2", actor_id="mop", incident_id=incident_id, summary="再次响应")
        # 同一事件不能重复响应（必须更换 request_id 才能真正再次进入状态机）
        with self.assertRaises(ConflictError):
            self.a11y.respond_to_work_order(
                request_id="respond3", actor_id="mop", incident_id=incident_id, summary="第三次")

    # ------------------------------------------------------------ 临时措施

    def _measure(self, incident_id, request_id="m1", kind="staff_assist",
                 valid_to=None, supersedes=None, handover=None):
        return self.a11y.activate_temporary_measure(
            request_id=request_id, actor_id="op", incident_id=incident_id, kind=kind,
            description="工作人员协助走坡道", valid_from=iso(hours=0),
            valid_to=valid_to or iso(hours=12),
            handover_rule=handover or "到期前2小时交接给站务班",
            supersedes_measure_id=supersedes)

    def test_notice_only_rejected_for_sole_route(self):
        incident_id = self._open_incident()
        with self.assertRaises(ValidationError):
            self.a11y.activate_temporary_measure(
                request_id="m-bad", actor_id="op", incident_id=incident_id,
                kind="notice_only", description="仅张贴告示", valid_from=iso(hours=1),
                valid_to=iso(hours=12), handover_rule="无")

    def test_measure_must_expire_and_supersession_forbids_gap(self):
        incident_id = self._open_incident()
        result = self._measure(incident_id)
        measure_id = None
        view = self.a11y.get_incident(incident_id)
        measure_id = view["temporary_measures"][0]["measure_id"]
        self.assertEqual("alternative",
                         self.a11y.available_routes("s1")["routes"][0]["status"])
        # 不允许在没有接替关系的情况下并列第二条承诺
        with self.assertRaises(ConflictError):
            self._measure(incident_id, request_id="m-dup", valid_to=iso(hours=20))
        # 接替措施必须在原承诺到期前生效，不允许空窗
        with self.assertRaises(ValidationError):
            self.a11y.activate_temporary_measure(
                request_id="m-gap", actor_id="op", incident_id=incident_id,
                kind="shuttle", description="接驳车", valid_from=iso(hours=13),
                valid_to=iso(hours=30), handover_rule="交接",
                supersedes_measure_id=measure_id)
        # 正常续设：原措施标记 superseded，新承诺生效
        self.a11y.activate_temporary_measure(
            request_id="m2", actor_id="op", incident_id=incident_id, kind="shuttle",
            description="接驳车", valid_from=iso(hours=10), valid_to=iso(hours=30),
            handover_rule="交接给夜班", supersedes_measure_id=measure_id)
        view = self.a11y.get_incident(incident_id)
        statuses = sorted(m["status"] for m in view["temporary_measures"])
        self.assertEqual(["active", "superseded"], statuses)

    def test_measure_max_duration_limited(self):
        incident_id = self._open_incident()
        with self.assertRaises(ValidationError):
            self.a11y.activate_temporary_measure(
                request_id="m-long", actor_id="op", incident_id=incident_id,
                kind="staff_assist", description="过长承诺", valid_from=iso(hours=1),
                valid_to=iso(hours=80), handover_rule="无")

    def test_handover_recorded(self):
        incident_id = self._open_incident()
        self._measure(incident_id)
        view = self.a11y.get_incident(incident_id)
        measure_id = view["temporary_measures"][0]["measure_id"]
        self.a11y.handover_measure(request_id="h1", actor_id="op", measure_id=measure_id,
                                   to_party="夜班值班组", note="双人护送")
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("夜班值班组", view["temporary_measures"][0]["handed_over_to"])

    def test_sweep_expires_measure_and_notifies(self):
        incident_id = self._open_incident()
        self._measure(incident_id, valid_to=iso(hours=2))
        self.clock._value = BASE + timedelta(hours=3)
        result = self.a11y.run_sweep()
        self.assertEqual(1, len(result["expired_measures"]))
        routes = self.a11y.available_routes("s1")
        self.assertEqual("unavailable", routes["routes"][0]["status"])
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("expired", view["temporary_measures"][0]["status"])
        reasons = {n["reason"] for n in view["notifications"]}
        self.assertTrue(any("到期" in r for r in reasons))

    # ------------------------------------------------------------ 复检与复开

    def _repair_and_pass(self, incident_id, respond_req="resp1", complete_req="comp1",
                         reinspect_req="ri01", actor="rev"):
        self.a11y.respond_to_work_order(request_id=respond_req, actor_id="mop",
                                        incident_id=incident_id, summary="到场")
        self._measure(incident_id)
        self.a11y.complete_repair(request_id=complete_req, actor_id="mop",
                                  incident_id=incident_id, summary="修复完成")
        self.a11y.submit_reinspection(request_id=reinspect_req, actor_id=actor,
                                      incident_id=incident_id, result="pass",
                                      evidence=EVIDENCE, note="复检合格")

    def test_cannot_reopen_without_independent_reinspection(self):
        incident_id = self._open_incident()
        self.a11y.respond_to_work_order(request_id="r1", actor_id="mop",
                                        incident_id=incident_id, summary="到场")
        self.a11y.complete_repair(request_id="c1", actor_id="mop",
                                  incident_id=incident_id, summary="修复完成")
        # 维护机构自己的 reviewer 不独立
        with self.assertRaises(PermissionDenied):
            self.a11y.submit_reinspection(
                request_id="ri-bad", actor_id="rev2", incident_id=incident_id,
                result="pass", evidence=EVIDENCE, note="自检")
        # 证据缺失
        with self.assertRaises(ValidationError):
            self.a11y.submit_reinspection(
                request_id="ri-empty", actor_id="rev", incident_id=incident_id,
                result="pass", evidence=[], note="无证据")
        # 维修方不能直接宣布复开：完成维修后设施仍不可用
        self.assertEqual("unavailable",
                         self.a11y.available_routes("s1")["routes"][0]["status"])

    def test_pass_reopens_and_ends_measures(self):
        incident_id = self._open_incident()
        self._repair_and_pass(incident_id)
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("closed", view["status"])
        self.assertIsNotNone(view["basis_reinspection_id"])
        self.assertIsNotNone(view["guarantee_until"])
        self.assertEqual("ended_on_reopen", view["temporary_measures"][0]["status"])
        self.assertEqual("available",
                         self.a11y.available_routes("s1")["routes"][0]["status"])

    def test_failed_reinspection_creates_rework_on_same_chain(self):
        incident_id = self._open_incident()
        self.a11y.respond_to_work_order(request_id="r1", actor_id="mop",
                                        incident_id=incident_id, summary="到场")
        self.a11y.complete_repair(request_id="c1", actor_id="mop",
                                  incident_id=incident_id, summary="修了")
        self.a11y.submit_reinspection(request_id="ri-fail", actor_id="rev",
                                      incident_id=incident_id, result="fail",
                                      evidence=EVIDENCE, note="仍异常")
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("in_repair", view["status"])
        self.assertEqual(2, len(view["work_orders"]))
        self.assertEqual("rework", view["work_orders"][1]["kind"])
        self.assertEqual(view["work_orders"][0]["work_order_id"],
                         view["work_orders"][1]["continued_from_work_order_id"])
        # 原 SLA 时限不变
        self.assertEqual(view["due_repair_at"],
                         view["work_orders"][1]["due_repair_at"])
        self.assertEqual(1, view["escalation_level"])

    def test_recurrence_within_guarantee_reopens_chain(self):
        incident_id = self._open_incident()
        self._repair_and_pass(incident_id)
        self.clock._value = BASE + timedelta(days=5)
        recur = self.a11y.report_defect(
            request_id="rep-rec", actor_id="op", facility_id="elev-1",
            severity="high", summary="复检后五天又停用")
        self.assertEqual("recurrence", recur["mode"])
        self.assertEqual(incident_id, recur["incident_id"])
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("reopened", view["status"])
        self.assertEqual(1, view["recurrence_count"])
        self.assertIsNone(view["basis_reinspection_id"])  # 旧验收单失效
        self.assertGreaterEqual(view["escalation_level"], 1)
        self.assertEqual("recurrence", view["work_orders"][-1]["kind"])
        self.assertEqual("unavailable",
                         self.a11y.available_routes("s1")["routes"][0]["status"])

    def test_recurrence_after_guarantee_starts_linked_new_incident(self):
        incident_id = self._open_incident()
        self._repair_and_pass(incident_id)
        self.clock._value = BASE + timedelta(days=31)
        report = self.a11y.report_defect(
            request_id="rep-late", actor_id="op", facility_id="elev-1",
            severity="medium", summary="一月后再坏")
        self.assertEqual("new", report["mode"])
        self.assertNotEqual(incident_id, report["incident_id"])
        view = self.a11y.get_incident(report["incident_id"])
        self.assertEqual(incident_id, view["recurrence_of_incident_id"])
        self.assertEqual(1, view["recurrence_count"])

    def test_withdraw_basis_reinspection_reopens(self):
        incident_id = self._open_incident()
        self._repair_and_pass(incident_id)
        view = self.a11y.get_incident(incident_id)
        basis = view["basis_reinspection_id"]
        result = self.a11y.withdraw_reinspection(
            request_id="w1", actor_id="rev", reinspection_id=basis,
            reason="证据照片为旧验收单翻拍")
        self.assertFalse(result["replayed"])
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("reopened", view["status"])
        self.assertIsNone(view["basis_reinspection_id"])
        self.assertEqual("evidence_withdrawn", view["work_orders"][-1]["kind"])
        self.assertEqual("unavailable",
                         self.a11y.available_routes("s1")["routes"][0]["status"])
        reasons = " ".join(n["reason"] for n in view["notifications"])
        self.assertIn("旧验收单", reasons)
        # 撤回操作幂等保护：再次撤回冲突
        with self.assertRaises(ConflictError):
            self.a11y.withdraw_reinspection(
                request_id="w2", actor_id="rev", reinspection_id=basis, reason="再撤")

    # ------------------------------------------------------------ 跨单位移交

    def test_transfer_keeps_chain_and_original_clock(self):
        incident_id = self._open_incident()
        self.a11y.respond_to_work_order(request_id="r1", actor_id="mop",
                                        incident_id=incident_id, summary="到场")
        original_due = self.a11y.get_incident(incident_id)["due_repair_at"]
        self.a11y.transfer_responsibility(
            request_id="t1", actor_id="adm", incident_id=incident_id,
            target_contract_id="c-maint2", reason="原机构备件不足")
        view = self.a11y.get_incident(incident_id)
        self.assertEqual("maint", view["frozen_contractor_org"])  # 原始责任冻结不变
        self.assertEqual("maint2", view["work_orders"][-1]["contractor_org_id"])
        self.assertEqual("transfer", view["work_orders"][-1]["kind"])
        self.assertEqual(original_due, view["due_repair_at"])
        # 新机构承接工单，原机构不能再操作
        with self.assertRaises(PermissionDenied):
            self.a11y.respond_to_work_order(
                request_id="rx", actor_id="mop", incident_id=incident_id, summary="越权")
        self.a11y.respond_to_work_order(request_id="r2", actor_id="mop2",
                                        incident_id=incident_id, summary="承接到场")

    def test_transfer_requires_valid_contract(self):
        incident_id = self._open_incident()
        with self.assertRaises(NotFoundError):
            self.a11y.transfer_responsibility(
                request_id="t-bad", actor_id="adm", incident_id=incident_id,
                target_contract_id="missing", reason="x")

    def test_only_admin_may_transfer(self):
        incident_id = self._open_incident()
        with self.assertRaises(PermissionDenied):
            self.a11y.transfer_responsibility(
                request_id="t-op", actor_id="op", incident_id=incident_id,
                target_contract_id="c-maint2", reason="x")

    # ------------------------------------------------------------ 逾期与看板

    def test_overdue_board_and_sweep_idempotent(self):
        incident_id = self._open_incident(severity="critical")
        # 尚未跑 sweep：看板已经能看到响应逾期
        self.clock._value = BASE + timedelta(hours=1)
        board = self.a11y.overdue_board("s1")
        stages = {item["stage"] for item in board["items"]}
        self.assertIn("response", stages)
        self.assertIn("temporary_measure", stages)
        self.assertFalse(any(item["already_escalated"] for item in board["items"]))
        first = self.a11y.run_sweep()
        self.assertGreaterEqual(first["escalation_count"], 2)
        second = self.a11y.run_sweep()
        self.assertEqual(0, second["escalation_count"])  # 不重复升级
        board = self.a11y.overdue_board("s1")
        self.assertTrue(all(item["already_escalated"] for item in board["items"]))
        view = self.a11y.get_incident(incident_id)
        self.assertGreaterEqual(view["escalation_level"], 2)
        # 响应后该环节逾期标记解除，看板不再出现 response
        self.a11y.respond_to_work_order(request_id="rr", actor_id="mop",
                                        incident_id=incident_id, summary="迟到响应")
        board = self.a11y.overdue_board("s1")
        self.assertNotIn("response", {i["stage"] for i in board["items"]})

    def test_expiring_commitments_listed(self):
        incident_id = self._open_incident()
        self._measure(incident_id, valid_to=iso(hours=10))
        result = self.a11y.expiring_commitments("s1", within_hours=24)
        self.assertEqual(1, len(result["items"]))
        self.assertGreater(result["items"][0]["minutes_remaining"], 0)
        none = self.a11y.expiring_commitments("s1", within_hours=1)
        self.assertEqual(0, len(none["items"]))

    # ------------------------------------------------------------ 时点还原

    def test_snapshot_reconstructs_history(self):
        # 第 1 天：报修 -> 修复 -> 复检通过复开
        incident_id = self._open_incident()
        self._repair_and_pass(incident_id)
        # 第 5 天：质保内复发
        self.clock._value = BASE + timedelta(days=5)
        self.a11y.report_defect(request_id="rep-r", actor_id="op", facility_id="elev-1",
                                severity="high", summary="复发")
        # 第 6 天：跨单位移交后修复、复检通过
        self.clock._value = BASE + timedelta(days=6)
        self.a11y.transfer_responsibility(request_id="tr", actor_id="adm",
                                          incident_id=incident_id,
                                          target_contract_id="c-maint2", reason="移交")
        self.a11y.respond_to_work_order(request_id="rr2", actor_id="mop2",
                                        incident_id=incident_id, summary="到场")
        self.a11y.complete_repair(request_id="cc2", actor_id="mop2",
                                  incident_id=incident_id, summary="修复")
        self.a11y.submit_reinspection(request_id="ri2", actor_id="rev",
                                      incident_id=incident_id, result="pass",
                                      evidence=EVIDENCE, note="合格")
        second_basis = self.a11y.get_incident(incident_id)["basis_reinspection_id"]
        # 第 8 天：复检依据被撤回，复开作废
        self.clock._value = BASE + timedelta(days=8)
        self.a11y.withdraw_reinspection(request_id="wd", actor_id="rev",
                                        reinspection_id=second_basis,
                                        reason="复检录像缺失")

        day2 = self.a11y.snapshot_facility_at("elev-1", iso(days=2))
        self.assertEqual("available", day2["availability"])
        view = day2["incidents"][0]
        self.assertEqual("closed", view["status"])
        self.assertIsNotNone(view["basis_reinspection"])
        self.assertEqual(0, view["recurrence_count_at"])

        day5b = self.a11y.snapshot_facility_at("elev-1", iso(days=5, hours=1))
        self.assertEqual("unavailable", day5b["availability"])
        view = day5b["incidents"][0]
        self.assertEqual("reopened", view["status"])
        self.assertIsNone(view["basis_reinspection"])  # 复发后旧验收单失效
        self.assertGreaterEqual(len(view["notifications"]), 1)

        day7 = self.a11y.snapshot_facility_at("elev-1", iso(days=7))
        self.assertEqual("available", day7["availability"])

        day9 = self.a11y.snapshot_facility_at("elev-1", iso(days=9))
        self.assertEqual("unavailable", day9["availability"])
        self.assertEqual("复检依据撤回", day9["availability_reason"])
        view = day9["incidents"][0]
        self.assertIsNone(view["basis_reinspection"])
        # 通知对象可按时点还原：包含值班经理、责任机构
        recipients = {(n["recipient_kind"], n["recipient_id"]) for n in view["notifications"]}
        self.assertIn(("org", "maint"), recipients)

    # ------------------------------------------------------------ 权限边界

    def test_other_site_operator_cannot_register(self):
        self.domain.register_organization(request_id="org-other", actor_id="adm",
                                          organization_id="other", name="外单位")
        self.domain.register_actor(request_id="oop", actor_id="adm", new_actor_id="oop",
                                   display_name="外单位员", role="operator",
                                   organization_id="other")
        with self.assertRaises(PermissionDenied):
            self.a11y.register_facility(
                request_id="f-other", actor_id="oop", site_id="s1", facility_id="x",
                facility_type="ramp", location="l", description="d",
                affected_groups=["wheelchair"])


if __name__ == "__main__":
    unittest.main()
