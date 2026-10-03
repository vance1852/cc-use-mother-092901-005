import unittest
from datetime import datetime, timezone

from transport_coordination.charter import CharterService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.storage import Database

CONTRACT = {"signer": "旅行社", "signed_at": "2026-09-20T10:00:00+08:00", "terms": []}
SEGMENTS = [
    {"seq": 1, "region_code": "RA", "road_from": "A站", "road_to": "A界",
     "depart_at": "2026-10-01T06:00:00+08:00", "arrive_at": "2026-10-01T09:00:00+08:00"},
    {"seq": 2, "region_code": "RB", "road_from": "B界", "road_to": "B景区",
     "depart_at": "2026-10-01T09:30:00+08:00", "arrive_at": "2026-10-01T11:30:00+08:00"},
]


class CharterTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CharterService(self.database,
                                      FixedClock(datetime(2026, 9, 28, tzinfo=timezone.utc)))
        s = self.service
        s.register_organization(request_id="o1", actor_id="bootstrap",
                                organization_id="o1", name="旅行社")
        s.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="admin",
                         display_name="管理员", role="admin", organization_id="o1")
        for rid, org, name in [("o2", "o2", "A交通"), ("o3", "o3", "A文旅"),
                               ("o4", "o4", "B交通"), ("o5", "o5", "B文旅")]:
            s.register_organization(request_id=rid, actor_id="admin",
                                    organization_id=org, name=name)
        for aid, role, org in [("ag", "operator", "o1"), ("atr", "reviewer", "o2"),
                               ("atv", "reviewer", "o3"), ("btr", "reviewer", "o4"),
                               ("btv", "reviewer", "o5"), ("police", "enforcer", "o2")]:
            s.register_actor(request_id=f"a-{aid}", actor_id="admin", new_actor_id=aid,
                             display_name=aid, role=role, organization_id=org)
        s.register_region(request_id="ra", actor_id="admin", region_code="RA", name="A市",
                          transport_org_id="o2", tourism_org_id="o3")
        s.register_region(request_id="rb", actor_id="admin", region_code="RB", name="B市",
                          transport_org_id="o4", tourism_org_id="o5")
        s.register_vehicle(request_id="v1", actor_id="admin", vehicle_id="v1", owner_org_id="o1",
                           plate="京A1", seat_count=45, transport_license_no="L1",
                           license_valid_until="2027-01-01", insurance_valid_until="2027-01-01")
        s.register_driver(request_id="d1", actor_id="admin", driver_id="d1", owner_org_id="o1",
                          display_name="张师傅", qualification_no="Q1",
                          qualification_valid_until="2027-01-01")

    def tearDown(self):
        self.database.close()

    def _file(self, request_id="f1", filing_id="f1", tour_code="G1", **overrides):
        params = {"actor_id": "ag", "filing_id": filing_id, "tour_code": tour_code,
                  "passenger_count": 40, "vehicle_id": "v1", "driver_id": "d1",
                  "contract": CONTRACT, "segments": SEGMENTS}
        params.update(overrides)
        return self.service.file_trip(request_id=request_id, **params)

    def _permit_map(self, filing_id="f1"):
        view = self.service.get_filing("ag", filing_id)
        return {(p["seq"], p["region_code"]): p["permit_id"] for p in view["permits"]}

    def _approve_all(self, filing_id="f1"):
        permits = self._permit_map(filing_id)
        self.service.decide_permit(request_id="at", actor_id="atr",
                                   permit_id=permits[(1, "RA")],
                                   decision="approve", department="transport")
        self.service.decide_permit(request_id="av", actor_id="atv",
                                   permit_id=permits[(1, "RA")],
                                   decision="approve", department="tourism")
        self.service.decide_permit(request_id="bt", actor_id="btr",
                                   permit_id=permits[(2, "RB")],
                                   decision="approve", department="transport")
        self.service.decide_permit(request_id="bv", actor_id="btv",
                                   permit_id=permits[(2, "RB")],
                                   decision="approve", department="tourism")

    # ------------------------------------------------------------ 规则识别

    def test_expired_license_blocks_filing(self):
        self.service.register_vehicle(
            request_id="v2", actor_id="admin", vehicle_id="v2", owner_org_id="o1",
            plate="京A2", seat_count=45, transport_license_no="L2",
            license_valid_until="2026-09-30", insurance_valid_until="2027-01-01")
        with self.assertRaises(ConflictError):
            self._file(request_id="fx", filing_id="fx", tour_code="X", vehicle_id="v2")

    def test_over_capacity_blocks_filing(self):
        with self.assertRaises(ConflictError):
            self._file(request_id="fo", filing_id="fo", tour_code="O", passenger_count=99)

    def test_work_hours_violations_appear_as_blocking(self):
        segments = [
            {"seq": 1, "region_code": "RA", "road_from": "x", "road_to": "y",
             "depart_at": "2026-10-01T06:00:00+08:00", "arrive_at": "2026-10-01T12:00:00+08:00"},
            {"seq": 2, "region_code": "RB", "road_from": "y", "road_to": "w",
             "depart_at": "2026-10-01T20:00:00+08:00", "arrive_at": "2026-10-01T23:00:00+08:00"},
        ]
        self._file(request_id="fw", filing_id="fw", tour_code="W", passenger_count=20,
                   driver_id="d1", segments=segments)
        codes = {b["code"] for b in self.service.get_filing("ag", "fw")["blocking"]}
        self.assertIn("continuous_drive_overrun", codes)
        self.assertIn("daily_drive_overrun", codes)
        # 工时问题未消除前不能批准
        permit = self._permit_map("fw")[(1, "RA")]
        with self.assertRaises(ConflictError):
            self.service.decide_permit(request_id="x", actor_id="atr", permit_id=permit,
                                       decision="approve", department="transport")

    def test_duplicate_filing_is_resource_conflict(self):
        self._file()
        self._approve_all()
        with self.assertRaises(ConflictError):
            self._file(request_id="fdup", filing_id="fdup", tour_code="DUP")

    # ------------------------------------------------------------ 联审

    def test_jurisdiction_isolation(self):
        self._file()
        permits = self._permit_map()
        with self.assertRaises(PermissionDenied):
            self.service.decide_permit(request_id="x", actor_id="atr",
                                       permit_id=permits[(2, "RB")],
                                       decision="approve", department="transport")

    def test_both_departments_required(self):
        self._file()
        permits = self._permit_map()
        self.service.decide_permit(request_id="at", actor_id="atr",
                                   permit_id=permits[(1, "RA")],
                                   decision="approve", department="transport")
        view = self.service.get_filing("ag", "f1")
        self.assertEqual("pending", view["permits"][0]["status"])
        self.service.decide_permit(request_id="av", actor_id="atv",
                                   permit_id=permits[(1, "RA")],
                                   decision="approve", department="tourism")
        view = self.service.get_filing("ag", "f1")
        self.assertEqual("approved", view["permits"][0]["status"])

    def test_certificate_only_when_all_permits_ready(self):
        self._file()
        with self.assertRaises(ConflictError):
            self.service.issue_certificate(request_id="cert-1", actor_id="ag", filing_id="f1")
        self._approve_all()
        receipt = self.service.issue_certificate(request_id="cert-1", actor_id="ag", filing_id="f1")
        self.assertEqual("TC-f1-V1", receipt.resource_id)

    # ------------------------------------------------------------ 修订与沿用

    def test_closure_amendment_only_rereviews_affected_segment(self):
        self._file()
        self._approve_all()
        self.service.issue_certificate(request_id="c1", actor_id="ag", filing_id="f1")
        self.service.start_trip(request_id="s1", actor_id="ag", filing_id="f1")
        self.service.register_closure(
            request_id="cl", actor_id="btr", closure_id="c1", region_code="RB",
            route_label="B界", valid_from="2026-10-01T09:00:00+08:00",
            valid_to="2026-10-01T18:00:00+08:00", reason="临时管制")
        amend = self.service.amend_filing(request_id="am", actor_id="ag", filing_id="f1",
                                          kind="road_closure", reason="封路改线",
                                          changes={"closure_id": "c1"})
        self.assertEqual([2], amend.data["affected_seqs"])
        self.assertEqual([1], amend.data["reused_seqs"])
        # 行程已开始：不整体回退，旧证在重审期间仍核验有效
        check = self.service.enforcement_check(actor_id="police", certificate_no="TC-f1-V1",
                                               at="2026-10-01T10:00:00+08:00")
        self.assertTrue(check["valid"])
        # 新版本 A 区段为继承批准
        view = self.service.get_filing("ag", "f1")
        inherited = [d for p in view["permits"] if p["seq"] == 1 for d in p["decisions"]]
        self.assertTrue(inherited and all(d["inherited"] for d in inherited))
        # B 区段重审通过后签发 V2，形成版本链
        new_b = next(p["permit_id"] for p in view["permits"] if p["seq"] == 2)
        self.service.decide_permit(request_id="bt2", actor_id="btr", permit_id=new_b,
                                   decision="approve", department="transport")
        self.service.decide_permit(request_id="bv2", actor_id="btv", permit_id=new_b,
                                   decision="approve", department="tourism")
        v2 = self.service.issue_certificate(request_id="c2", actor_id="ag", filing_id="f1")
        self.assertEqual("TC-f1-V2", v2.data["certificate_no"])
        self.assertEqual("TC-f1-V1", v2.data["chain_head"])

    def test_vehicle_change_rereviews_all_segments(self):
        self._file()
        self._approve_all()
        self.service.issue_certificate(request_id="c1", actor_id="ag", filing_id="f1")
        self.service.register_vehicle(
            request_id="v3", actor_id="admin", vehicle_id="v3", owner_org_id="o1",
            plate="京A3", seat_count=50, transport_license_no="L3",
            license_valid_until="2027-06-01", insurance_valid_until="2027-06-01")
        amend = self.service.amend_filing(request_id="am", actor_id="ag", filing_id="f1",
                                          kind="vehicle_change", reason="原车故障",
                                          changes={"vehicle_id": "v3"})
        self.assertEqual([1, 2], sorted(amend.data["affected_seqs"]))

    def test_cancel_voids_certificate_and_states_refund_basis(self):
        self._file()
        self._approve_all()
        self.service.issue_certificate(request_id="c1", actor_id="ag", filing_id="f1")
        self.service.start_trip(request_id="s1", actor_id="ag", filing_id="f1")
        amend = self.service.amend_filing(request_id="cx", actor_id="ag", filing_id="f1",
                                          kind="cancel", reason="台风")
        self.assertEqual("cancelled", self.service.get_filing("ag", "f1")["status"])
        self.assertEqual(["TC-f1-V1"], amend.data["voided_certificates"])
        self.assertEqual("travel_agency", amend.data["refund"]["responsible_party"])

    # ------------------------------------------------------------ 执法与视图

    def test_enforcement_reports_current_permit_and_regions(self):
        self._file()
        self._approve_all()
        self.service.issue_certificate(request_id="c1", actor_id="ag", filing_id="f1")
        result = self.service.enforcement_check(
            actor_id="police", certificate_no="TC-f1-V1", at="2026-10-01T07:00:00+08:00")
        self.assertTrue(result["valid"])
        self.assertEqual("scheduled", result["phase"])
        self.assertEqual({"RA", "RB"},
                         {r["region_code"] for r in result["responsible_regions"]})

    def test_agency_view_exposes_blocking_and_refund(self):
        self._file()
        view = self.service.agency_view("ag", "f1")
        self.assertIn("blocking", view)
        self.assertIn("pending_seqs", view)
        with self.assertRaises(PermissionDenied):
            self.service.agency_view("atr", "f1")


if __name__ == "__main__":
    unittest.main()
