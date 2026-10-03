import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.accessibility import AccessibilityService
from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

BASE = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def iso(**delta):
    return (BASE + timedelta(**delta)).isoformat().replace("+00:00", "Z")


class AccessibilityApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.service = DomainService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="op"):
        return route(self.service, method, path, body, {"X-Actor-Id": actor})

    def _bootstrap(self):
        self._call("POST", "/organizations", {
            "request_id": "org1", "organization_id": "hub", "name": "枢纽"}, "bootstrap")
        self._call("POST", "/organizations", {
            "request_id": "org2", "organization_id": "maint", "name": "维保"}, "bootstrap")
        self._call("POST", "/actors", {
            "request_id": "act1", "new_actor_id": "adm", "display_name": "管理员",
            "role": "admin", "organization_id": "hub"}, "bootstrap")
        self._call("POST", "/actors", {
            "request_id": "act2", "new_actor_id": "op", "display_name": "站务员",
            "role": "operator", "organization_id": "hub"}, "adm")
        self._call("POST", "/actors", {
            "request_id": "act3", "new_actor_id": "rev", "display_name": "复检员",
            "role": "reviewer", "organization_id": "hub"}, "adm")
        self._call("POST", "/actors", {
            "request_id": "act4", "new_actor_id": "mop", "display_name": "维保工",
            "role": "operator", "organization_id": "maint"}, "adm")
        self._call("POST", "/sites", {
            "request_id": "site1", "site_id": "s1", "organization_id": "hub",
            "name": "枢纽", "timezone_name": "Asia/Shanghai"})

    def test_full_chain_over_http(self):
        status, body = self._call("POST", "/facilities", {
            "request_id": "f1", "site_id": "s1", "facility_id": "elev-1",
            "facility_type": "accessible_elevator", "location": "一号梯",
            "description": "站台梯", "affected_groups": ["wheelchair"],
            "sole_accessible_route": True})
        self.assertEqual(201, status)

        status, body = self._call("POST", "/maintenance-contracts", {
            "request_id": "c1", "site_id": "s1", "contract_id": "ct1",
            "contractor_org_id": "maint", "valid_from": iso(days=-1),
            "valid_to": iso(days=30)}, "adm")
        self.assertEqual(201, status)

        status, report = self._call("POST", "/defect-reports", {
            "request_id": "d1", "facility_id": "elev-1", "severity": "low",
            "summary": "停用"})
        self.assertEqual(201, status)
        self.assertEqual("high", report["risk_level"])
        incident_id = report["incident_id"]

        status, routes = self._call("GET", "/routes?site_id=s1")
        self.assertEqual(200, status)
        self.assertEqual("unavailable", routes["routes"][0]["status"])

        status, _ = self._call("POST", "/work-orders/respond", {
            "request_id": "p1", "incident_id": incident_id, "summary": "到场"}, "mop")
        self.assertEqual(200, status)
        status, _ = self._call("POST", "/temporary-measures", {
            "request_id": "m1", "incident_id": incident_id, "kind": "staff_assist",
            "description": "人工协助", "valid_from": iso(hours=0), "valid_to": iso(hours=10),
            "handover_rule": "交接站务班"})
        self.assertEqual(201, status)
        status, routes = self._call("GET", "/routes?site_id=s1")
        self.assertEqual("alternative", routes["routes"][0]["status"])

        status, _ = self._call("POST", "/work-orders/complete", {
            "request_id": "x1", "incident_id": incident_id, "summary": "修复"}, "mop")
        self.assertEqual(200, status)
        status, _ = self._call("POST", "/reinspections", {
            "request_id": "ri1", "incident_id": incident_id, "result": "pass",
            "evidence": [{"evidence_id": "e1", "type": "photo", "uri": "file://e.jpg"}],
            "note": "合格"}, "rev")
        self.assertEqual(201, status)

        status, routes = self._call("GET", "/routes?site_id=s1")
        self.assertEqual("available", routes["routes"][0]["status"])
        status, incident = self._call("GET", f"/incidents?id={incident_id}")
        self.assertEqual(200, status)
        self.assertEqual("closed", incident["status"])
        self.assertGreaterEqual(len(incident["timeline"]), 6)

        self.clock._value = BASE + timedelta(days=3)
        status, rec = self._call("POST", "/defect-reports", {
            "request_id": "d2", "facility_id": "elev-1", "severity": "high",
            "summary": "复发"})
        self.assertEqual("recurrence", rec["mode"])

        status, snap = self._call("GET", f"/facility-snapshot?facility_id=elev-1&at={iso(days=2)}")
        self.assertEqual(200, status)
        self.assertEqual("available", snap["availability"])
        status, snap = self._call("GET", f"/facility-snapshot?facility_id=elev-1&at={iso(days=4)}")
        self.assertEqual("unavailable", snap["availability"])

    def test_overdue_and_expiring_routes(self):
        self._call("POST", "/facilities", {
            "request_id": "f1", "site_id": "s1", "facility_id": "elev-1",
            "facility_type": "accessible_elevator", "location": "一号梯",
            "description": "梯", "affected_groups": ["wheelchair"],
            "sole_accessible_route": True})
        self._call("POST", "/maintenance-contracts", {
            "request_id": "c1", "site_id": "s1", "contract_id": "ct1",
            "contractor_org_id": "maint", "valid_from": iso(days=-1),
            "valid_to": iso(days=30)}, "adm")
        self._call("POST", "/defect-reports", {
            "request_id": "d1", "facility_id": "elev-1", "severity": "critical",
            "summary": "停用"})
        self.clock._value = BASE + timedelta(hours=2)
        status, board = self._call("GET", "/overdue?site_id=s1")
        self.assertEqual(200, status)
        self.assertIn("response", {i["stage"] for i in board["items"]})
        status, sweep = self._call("POST", "/sweeps", {})
        self.assertEqual(200, status)
        self.assertGreaterEqual(sweep["escalation_count"], 2)

    def test_missing_query_params_rejected(self):
        for path in ("/routes", "/overdue", "/expiring-commitments",
                     "/incidents", "/facility-snapshot?facility_id=x"):
            status, body = self._call("GET", path)
            self.assertEqual(400, status, path)
            self.assertEqual("validation_error", body["error"])


if __name__ == "__main__":
    unittest.main()
