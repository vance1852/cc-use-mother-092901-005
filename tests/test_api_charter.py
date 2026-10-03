import unittest
from datetime import datetime, timezone

from transport_coordination.api import build_services, route
from transport_coordination.clock import FixedClock
from transport_coordination.storage import Database
from tests.test_charter import CharterFixture


class CharterApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.base, self.charter = build_services(self.database)
        self.base.clock = clock
        self.charter.clock = clock

        def call(method, path, body=None, actor="a1", key=""):
            headers = {"X-Actor-Id": actor}
            if key:
                headers["X-Inspection-Key"] = key
            return route(self.base, method, path, body, headers, charter=self.charter)

        self.call = call
        call("POST", "/organizations",
             {"request_id": "org", "organization_id": "o1", "name": "假期旅行社"},
             actor="bootstrap")
        call("POST", "/actors",
             {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
              "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        call("POST", "/actors",
             {"request_id": "agent", "new_actor_id": "ag1", "display_name": "经办人",
              "role": "operator", "organization_id": "o1"})
        call("POST", "/actors",
             {"request_id": "rvA", "new_actor_id": "rvA", "display_name": "甲地区审批人",
              "role": "reviewer", "organization_id": "o1"})
        call("POST", "/actors",
             {"request_id": "rvB", "new_actor_id": "rvB", "display_name": "乙地区审批人",
              "role": "reviewer", "organization_id": "o1"})
        call("POST", "/reviewer-regions",
             {"request_id": "ra", "reviewer_actor_id": "rvA", "region_code": "R-A"})
        call("POST", "/reviewer-regions",
             {"request_id": "rb", "reviewer_actor_id": "rvB", "region_code": "R-B"})
        call("POST", "/region-licenses",
             {"request_id": "licA", "license_id": "lic-A",
              "region_code": "R-A", "valid_from": "2026-10-01T00:00:00",
              "valid_to": "2026-10-03T00:00:00", "route_code": "route-A1"},
             actor="rvA")
        call("POST", "/region-licenses",
             {"request_id": "licB", "license_id": "lic-B",
              "region_code": "R-B", "valid_from": "2026-10-01T00:00:00",
              "valid_to": "2026-10-03T00:00:00", "route_code": "route-B1"},
             actor="rvB")

    def tearDown(self):
        self.database.close()

    def test_full_charter_flow_over_http(self):
        payload = {"request_id": "grp", "group_id": "G-1", **CharterFixture().payload}
        status, body = self.call("POST", "/charter-groups", payload, actor="ag1")
        self.assertEqual(201, status)
        self.assertEqual("G-1", body["group_id"])

        status, body = self.call("GET", "/charter-groups?group_id=G-1")
        self.assertEqual(200, status)
        self.assertFalse(body["executable"])
        self.assertEqual("in_review", body["head_status"])

        status, body = self.call("POST", "/segment-decisions",
                                 {"request_id": "apA", "group_id": "G-1", "version": 1,
                                  "segment_id": "seg-A", "decision": "approved"},
                                 actor="rvA")
        self.assertEqual(201, status)
        self.assertFalse(body["executable"])

        # 跨辖区审批被拒
        status, body = self.call("POST", "/segment-decisions",
                                 {"request_id": "cross", "group_id": "G-1", "version": 1,
                                  "segment_id": "seg-B", "decision": "approved"},
                                 actor="rvA")
        self.assertEqual(403, status)

        status, body = self.call("POST", "/segment-decisions",
                                 {"request_id": "apB", "group_id": "G-1", "version": 1,
                                  "segment_id": "seg-B", "decision": "approved"},
                                 actor="rvB")
        self.assertEqual(201, status)
        self.assertTrue(body["executable"])

    def test_inspection_endpoint_requires_key_header(self):
        payload = {"request_id": "grp", "group_id": "G-1", **CharterFixture().payload}
        self.call("POST", "/charter-groups", payload, actor="ag1")
        status, body = self.call("GET", "/inspection/verify?group_id=G-1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", body["error"])

        self.call("POST", "/inspection-keys",
                  {"request_id": "key", "key": "enf-key"})
        status, body = self.call("GET", "/inspection/verify?group_id=G-1", key="enf-key")
        self.assertEqual(200, status)
        self.assertEqual("G-1", body["group_id"])
        self.assertEqual({"R-A", "R-B"}, {s["region_code"] for s in body["segments"]})

    def test_trip_event_and_blocking_reasons_visible_to_agency(self):
        payload = {"request_id": "grp", "group_id": "G-1", **CharterFixture().payload}
        self.call("POST", "/charter-groups", payload, actor="ag1")
        status, body = self.call("GET", "/charter-groups?group_id=G-1")
        self.assertEqual(200, status)
        # 材料齐备、仅待审批：没有阻断原因
        self.assertEqual([], body["blocking_reasons"])
        self.assertIn("refund_responsibility", body)

    def test_unknown_group_returns_404(self):
        status, body = self.call("GET", "/charter-groups?group_id=nope")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])


if __name__ == "__main__":
    unittest.main()
