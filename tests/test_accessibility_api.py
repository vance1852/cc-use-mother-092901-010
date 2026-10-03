"""无障碍整改服务的 HTTP 路由测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.accessibility import AccessibilityService
from transport_coordination.api import route
from transport_coordination.storage import Database


class RollingClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


def bootstrap(service: AccessibilityService) -> None:
    """登记一套最小可用的机构、人员、场所、合同与设施。"""

    service.register_organization(request_id="ao1", actor_id="bootstrap",
                                  organization_id="hub", name="枢纽")
    service.register_organization(request_id="ao2", actor_id="bootstrap",
                                  organization_id="prov", name="维保单位")
    service.register_actor(request_id="aa1", actor_id="bootstrap", new_actor_id="admin1",
                           display_name="管理员", role="admin", organization_id="hub")
    service.register_actor(request_id="aa2", actor_id="admin1", new_actor_id="mgr1",
                           display_name="经理", role="operator", organization_id="hub")
    service.register_actor(request_id="aa3", actor_id="admin1", new_actor_id="prov1",
                           display_name="维保员", role="operator", organization_id="prov")
    service.register_actor(request_id="aa4", actor_id="admin1", new_actor_id="rev1",
                           display_name="复检员", role="reviewer", organization_id="hub")
    service.register_site(request_id="as1", actor_id="mgr1", site_id="site1",
                          organization_id="hub", name="枢纽", timezone_name="Asia/Shanghai")
    service.register_contract(request_id="ac1", actor_id="mgr1", contract_id="contract1",
                              site_id="site1", provider_organization_id="prov",
                              title="电梯合同", contact="400", facility_type="elevator",
                              valid_from="2026-09-01T00:00:00Z",
                              valid_to="2026-12-31T00:00:00Z")
    service.register_facility(request_id="af1", actor_id="mgr1", site_id="site1",
                              facility_id="elev1", facility_type="elevator",
                              location_desc="东门电梯", affected_groups=["wheelchair"])
    return {}


class AccessibilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database()
        self.clock = RollingClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = AccessibilityService(self.database, self.clock)
        bootstrap(self.service)

    def tearDown(self) -> None:
        self.database.close()

    def test_full_chain_over_http(self):
        status, body = route(self.service, "POST", "/defects", {
            "request_id": "d1", "facility_id": "elev1", "description": "停运",
            "risk_level": "medium", "impact_groups": ["wheelchair"],
        }, {"X-Actor-Id": "mgr1"})
        self.assertEqual(201, status)
        self.assertEqual("high", body["response"]["risk_level"])
        event_id = body["resource_id"]
        wo_id = body["response"]["work_order_id"]

        status, _ = route(self.service, "POST", "/work-orders/respond",
                          {"request_id": "w1", "work_order_id": wo_id}, {"X-Actor-Id": "prov1"})
        self.assertEqual(201, status)

        status, body = route(self.service, "POST", "/interim-measures", {
            "request_id": "m1", "event_id": event_id, "kind": "staff_assist",
            "description": "专人协助", "expires_at": "2026-10-01T20:00:00Z",
        }, {"X-Actor-Id": "prov1"})
        self.assertEqual(201, status)

        status, _ = route(self.service, "POST", "/work-orders/complete", {
            "request_id": "w2", "work_order_id": wo_id, "repair_summary": "更换门机",
        }, {"X-Actor-Id": "prov1"})
        self.assertEqual(201, status)

        status, body = route(self.service, "POST", "/reinspections", {
            "request_id": "r1", "event_id": event_id, "result": "passed",
            "summary": "正常", "evidence_ref": "photo", "evidence_data": {"ok": True},
        }, {"X-Actor-Id": "rev1"})
        self.assertEqual(201, status)

        status, body = route(self.service, "GET", "/accessible-routes?site_id=site1", None)
        self.assertEqual(200, status)
        self.assertEqual("elev1", body["in_service"][0]["facility_id"])
        self.assertEqual([], body["out_of_service"])

    def test_inspector_from_provider_org_is_forbidden(self):
        self.service.register_actor(request_id="aa5", actor_id="admin1", new_actor_id="revp",
                                    display_name="自检", role="reviewer", organization_id="prov")
        route(self.service, "POST", "/defects", {
            "request_id": "d2", "facility_id": "elev1", "description": "坏",
            "risk_level": "high",
        }, {"X-Actor-Id": "mgr1"})
        status, body = route(self.service, "GET", "/accessible-routes?site_id=site1", None)
        event_id = body["out_of_service"][0]["current_event_id"]
        wo = self.database.connection.execute(
            "SELECT work_order_id FROM work_orders WHERE event_id=?", (event_id,)).fetchone()
        route(self.service, "POST", "/work-orders/respond",
              {"request_id": "w3", "work_order_id": wo["work_order_id"]}, {"X-Actor-Id": "prov1"})
        route(self.service, "POST", "/work-orders/complete",
              {"request_id": "w4", "work_order_id": wo["work_order_id"], "repair_summary": "修"},
              {"X-Actor-Id": "prov1"})
        status, body = route(self.service, "POST", "/reinspections", {
            "request_id": "r2", "event_id": event_id, "result": "passed",
            "summary": "好", "evidence_ref": "p", "evidence_data": {},
        }, {"X-Actor-Id": "revp"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

    def test_overdue_and_history_endpoints(self):
        status, body = route(self.service, "POST", "/defects", {
            "request_id": "d3", "facility_id": "elev1", "description": "坏",
            "risk_level": "critical", "impact_groups": ["wheelchair"],
        }, {"X-Actor-Id": "mgr1"})
        event_id = body["resource_id"]
        self.clock.value += timedelta(hours=5)  # 超过 critical 响应 1h / 修复 4h
        status, body = route(self.service, "POST", "/sweep", {}, {"X-Actor-Id": "mgr1"})
        self.assertEqual(200, status)
        self.assertEqual(1, body["overdue_work_orders"])
        status, body = route(self.service, "GET", "/overdue-board?site_id=site1", None)
        self.assertEqual(200, status)
        self.assertEqual(event_id, body["items"][0]["event_id"])
        self.assertIn("response", {b["stage"] for b in body["items"][0]["breaches"]})

        status, body = route(self.service, "GET",
                             f"/facility-history?facility_id=elev1&at=2030-01-01T00:00:00Z", None)
        self.assertEqual(200, status)
        self.assertEqual("out_of_service", body["status"])
        self.assertEqual(1, len(body["chains"]))

        status, body = route(self.service, "GET", "/facility-history?facility_id=elev1", None)
        self.assertEqual(400, status)

    def test_replayed_write_returns_same_receipt(self):
        payload = {"request_id": "d4", "facility_id": "elev1", "description": "坏",
                   "risk_level": "high", "impact_groups": ["wheelchair"]}
        first_status, first = route(self.service, "POST", "/defects", payload,
                                    {"X-Actor-Id": "mgr1"})
        second_status, second = route(self.service, "POST", "/defects", payload,
                                      {"X-Actor-Id": "mgr1"})
        self.assertEqual(201, first_status)
        self.assertEqual(200, second_status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resource_id"], second["resource_id"])
        self.assertEqual(first["response"]["work_order_id"], second["response"]["work_order_id"])


if __name__ == "__main__":
    unittest.main()
