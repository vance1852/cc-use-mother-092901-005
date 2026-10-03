import unittest
from datetime import datetime, timezone

from transport_coordination.charter import CharterService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


def valid_license(qtype, **overrides):
    license = {"type": qtype, "valid_from": "2026-01-01T00:00:00",
               "valid_to": "2027-01-01T00:00:00"}
    license.update(overrides)
    return license


class CharterFixture:
    """构造一套跨两个辖区的合法团次数据，测试可按需改动。"""

    def __init__(self, overrides=None):
        vehicle = {
            "vehicle_id": "V-1", "plate_no": "京A12345", "seat_capacity": 45,
            "qualifications": [valid_license("vehicle_license"),
                               valid_license("carrier_insurance")]}
        driver = {
            "driver_id": "D-1", "name": "张师傅",
            "qualifications": [valid_license("driver_license"),
                               valid_license("driver_qualification")],
            "duty_periods": [
                {"on": "2026-10-01T07:00:00", "off": "2026-10-01T19:00:00"},
                {"on": "2026-10-02T07:00:00", "off": "2026-10-02T18:00:00"}],
            "rest_periods": [
                {"start": "2026-10-01T12:00:00", "end": "2026-10-01T12:30:00"},
                {"start": "2026-10-02T12:00:00", "end": "2026-10-02T12:30:00"}]}
        segments = [
            {"segment_id": "seg-A", "region_code": "R-A", "ordinal": 1, "route_code": "route-A1",
             "from_site": "北京", "to_site": "济南",
             "departure_at": "2026-10-01T08:00:00", "arrive_at": "2026-10-01T16:00:00",
             "driver_id": "D-1"},
            {"segment_id": "seg-B", "region_code": "R-B", "ordinal": 2, "route_code": "route-B1",
             "from_site": "济南", "to_site": "南京",
             "departure_at": "2026-10-02T08:00:00", "arrive_at": "2026-10-02T16:00:00",
             "driver_id": "D-1"}]
        stops = [{"stop_id": "stop1", "segment_id": "seg-A", "name": "服务区",
                  "planned_at": "2026-10-01T12:00:00"}]
        contract = {"contract_no": "C-2026-001", "refund_policy": {
            "plan_change": "出发前7日全额退款", "agency_fault": "旅行社承担全额退款",
            "force_majeure": "扣除已发生费用后退款"}}
        tour = {"tour_code": "T-01", "title": "国庆江南游", "passenger_count": 40}
        self.payload = {"contract": contract, "tour": tour, "vehicle": vehicle,
                        "drivers": [driver], "segments": segments, "stops": stops}
        if overrides:
            for key, value in overrides.items():
                self.payload[key] = value


class CharterServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = CharterService(self.database, clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="假期旅行社")
        self.base.register_organization(request_id="org2", actor_id="bootstrap",
                                        organization_id="o2", name="另一旅行社")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="agent", actor_id="a1", new_actor_id="ag1",
                                 display_name="经办人", role="operator", organization_id="o1")
        self.base.register_actor(request_id="rvA", actor_id="a1", new_actor_id="rvA",
                                 display_name="甲地区审批人", role="reviewer", organization_id="o1")
        self.base.register_actor(request_id="rvB", actor_id="a1", new_actor_id="rvB",
                                 display_name="乙地区审批人", role="reviewer", organization_id="o1")
        self.service.assign_reviewer_region(request_id="ra", actor_id="a1",
                                            reviewer_actor_id="rvA", region_code="R-A")
        self.service.assign_reviewer_region(request_id="rb", actor_id="a1",
                                            reviewer_actor_id="rvB", region_code="R-B")
        self.service.register_region_license(
            request_id="licA", actor_id="rvA", license_id="lic-A", region_code="R-A",
            valid_from="2026-10-01T00:00:00", valid_to="2026-10-03T00:00:00",
            route_code="route-A1")
        self.service.register_region_license(
            request_id="licB", actor_id="rvB", license_id="lic-B", region_code="R-B",
            valid_from="2026-10-01T00:00:00", valid_to="2026-10-03T00:00:00",
            route_code="route-B1")

    def tearDown(self):
        self.database.close()

    def _submit(self, group_id="G-1", overrides=None, request_id=None):
        fixture = CharterFixture(overrides)
        return self.service.submit_charter_group(
            request_id=request_id or f"grp-{group_id}", actor_id="ag1",
            group_id=group_id, **fixture.payload)

    def _approve_chain(self, group_id="G-1", version=1):
        self.service.decide_segment(request_id=f"apA-{group_id}-{version}", actor_id="rvA",
                                    group_id=group_id, version=version,
                                    segment_id="seg-A", decision="approved")
        return self.service.decide_segment(request_id=f"apB-{group_id}-{version}", actor_id="rvB",
                                           group_id=group_id, version=version,
                                           segment_id="seg-B", decision="approved")

    # ------------------------------------------------------------ 提交与阻断

    def test_submit_requires_contract_refund_policy(self):
        fixture = CharterFixture()
        del fixture.payload["contract"]["refund_policy"]
        with self.assertRaises(ValidationError):
            self.service.submit_charter_group(
                request_id="bad", actor_id="ag1", group_id="G-9", **fixture.payload)

    def test_expiring_license_blocks_segment(self):
        driver = [{**CharterFixture().payload["drivers"][0]}]
        driver[0]["qualifications"] = [
            valid_license("driver_license", valid_to="2026-10-02T12:00:00"),
            valid_license("driver_qualification")]
        self._submit(overrides={"drivers": driver})
        view = self.service.get_group("G-1")
        codes = {reason["code"] for reason in view["blocking_reasons"]}
        self.assertIn("license.covers_partial", codes)
        self.assertFalse(view["executable"])

    def test_region_license_not_effective_blocks(self):
        # 辖区唯一覆盖许可尚未生效/被暂停时，区段不能获批
        self.service.update_region_license_status(
            request_id="suspA", actor_id="rvA", license_id="lic-A", status="suspended")
        self._submit()
        view = self.service.get_group("G-1")
        codes = {reason["code"] for reason in view["blocking_reasons"]}
        self.assertIn("permit.region_suspended", codes)

    def test_region_license_window_gap_blocks(self):
        self.service.update_region_license_status(
            request_id="suspA", actor_id="rvA", license_id="lic-A", status="suspended")
        self.service.register_region_license(
            request_id="licA2", actor_id="rvA", license_id="lic-A2", region_code="R-A",
            valid_from="2026-10-01T08:00:00", valid_to="2026-10-01T12:00:00",
            route_code="route-A1")
        self._submit()
        view = self.service.get_group("G-1")
        codes = {reason["code"] for reason in view["blocking_reasons"]}
        self.assertIn("permit.window_gap", codes)

    def test_overwork_blocks_approval(self):
        driver = [{**CharterFixture().payload["drivers"][0]}]
        driver[0]["duty_periods"] = [
            {"on": "2026-10-01T05:00:00", "off": "2026-10-01T20:00:00"},
            {"on": "2026-10-02T05:00:00", "off": "2026-10-02T20:00:00"}]
        self._submit(overrides={"drivers": driver})
        with self.assertRaises(ConflictError):
            self.service.decide_segment(request_id="apA", actor_id="rvA", group_id="G-1",
                                        version=1, segment_id="seg-A", decision="approved")

    def test_reviewer_cannot_touch_other_region(self):
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.service.decide_segment(request_id="cross", actor_id="rvB", group_id="G-1",
                                        version=1, segment_id="seg-A", decision="approved")

    def test_agency_cannot_approve(self):
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.service.decide_segment(request_id="self", actor_id="ag1", group_id="G-1",
                                        version=1, segment_id="seg-A", decision="approved")

    # ------------------------------------------------------------ 许可链签发

    def test_permit_chain_issues_only_when_all_regions_approve(self):
        self._submit()
        self.service.decide_segment(request_id="onlyA", actor_id="rvA", group_id="G-1",
                                    version=1, segment_id="seg-A", decision="approved")
        self.assertFalse(self.service.get_group("G-1")["executable"])
        result = self.service.decide_segment(request_id="onlyB", actor_id="rvB", group_id="G-1",
                                             version=1, segment_id="seg-B", decision="approved")
        self.assertTrue(result["executable"])
        self.assertEqual("executable", result["head_status"])
        view = self.service.get_group("G-1")
        self.assertEqual(["seg-A", "seg-B"],
                         [s["segment_id"] for s in view["current"]["segments"]])
        self.assertEqual("lic-A", view["current"]["segments"][0]["basis_license_id"])

    def test_approved_fact_is_immutable(self):
        self._submit()
        self._approve_chain()
        with self.assertRaises(ConflictError):
            self.service.decide_segment(request_id="again", actor_id="rvA", group_id="G-1",
                                        version=1, segment_id="seg-A", decision="approved")

    def test_rejection_keeps_group_nonexecutable(self):
        self._submit()
        self.service.decide_segment(request_id="rjA", actor_id="rvA", group_id="G-1",
                                    version=1, segment_id="seg-A", decision="rejected",
                                    reason="材料不符")
        self.service.decide_segment(request_id="apB", actor_id="rvB", group_id="G-1",
                                    version=1, segment_id="seg-B", decision="approved")
        self.assertFalse(self.service.get_group("G-1")["executable"])

    def test_duplicate_submission_same_contract_detected(self):
        self._submit()
        self._approve_chain()
        # 重复申报不会被受理为新团次之外的独立行程：形成阻断结论且不可签发
        self._submit(group_id="G-2")
        view = self.service.get_group("G-2")
        codes = {reason["code"] for reason in view["blocking_reasons"]}
        self.assertIn("duplicate.group_declaration", codes)
        self.assertFalse(view["executable"])

    def test_mutex_route_conflict_detected(self):
        self._submit(group_id="G-1")
        self._approve_chain("G-1")
        # 另一家旅行社在同一时段使用同一辖区路线
        fixture = CharterFixture()
        fixture.payload["contract"]["contract_no"] = "C-2026-002"
        fixture.payload["tour"]["tour_code"] = "T-02"
        self.service.submit_charter_group(
            request_id="grp2", actor_id="ag1", group_id="G-2", **fixture.payload)
        view = self.service.get_group("G-2")
        codes = {reason["code"] for reason in view["blocking_reasons"]}
        self.assertIn("route.mutex_conflict", codes)

    def test_request_id_replay_is_idempotent(self):
        first = self._submit()
        second = self._submit()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["group_id"], second["group_id"])

    # ------------------------------------------------------------ 履约与版本

    def _start(self, group_id="G-1"):
        self._submit(group_id)
        self._approve_chain(group_id)
        return self.service.mark_trip_started(
            request_id=f"start-{group_id}", actor_id="ag1", group_id=group_id)

    def test_cannot_start_before_executable(self):
        self._submit()
        with self.assertRaises(ConflictError):
            self.service.mark_trip_started(request_id="start-bad", actor_id="ag1", group_id="G-1")

    def test_road_closure_only_rereviews_affected_segment(self):
        self._start()
        self.service.register_trip_event(
            request_id="close", actor_id="ag1", group_id="G-1", event_type="road_closure",
            affected_segment_ids=["seg-B"],
            reroute={"seg-B": {"route_code": "route-B1", "from_site": "济南", "to_site": "南京",
                               "region_code": "R-B", "ordinal": 2, "driver_id": "D-1",
                               "departure_at": "2026-10-02T09:00:00",
                               "arrive_at": "2026-10-02T16:00:00"}})
        view = self.service.get_group("G-1")
        self.assertEqual(2, view["current_version"])
        seg_a, seg_b = view["current"]["segments"]
        self.assertEqual(1, seg_a["carried_from_version"])
        self.assertTrue(seg_a["effective"])
        self.assertIsNone(seg_b["carried_from_version"])
        self.assertEqual("pending", seg_b["review_status"])
        # 行程已开始，头部状态保持履约中、不整体回退
        self.assertEqual("in_progress", view["head_status"])
        self.assertFalse(view["executable"])
        # B 区段重审通过后恢复可执行
        self.service.decide_segment(request_id="apB2", actor_id="rvB", group_id="G-1",
                                    version=2, segment_id="seg-B", decision="approved")
        self.assertTrue(self.service.get_group("G-1")["executable"])

    def test_started_trip_regional_change_does_not_reset_head(self):
        self._start()
        self.service.register_trip_event(
            request_id="close", actor_id="ag1", group_id="G-1", event_type="road_closure",
            affected_segment_ids=["seg-A"],
            reroute={"seg-A": {"route_code": "route-A1", "from_site": "北京", "to_site": "德州",
                               "region_code": "R-A", "ordinal": 1, "driver_id": "D-1",
                               "departure_at": "2026-10-01T08:00:00",
                               "arrive_at": "2026-10-01T15:00:00"}})
        view = self.service.get_group("G-1")
        self.assertEqual("in_progress", view["head_status"])

    def test_driver_replacement_only_rereviews_served_segments(self):
        # 双驾驶员：D-1 负责甲地区段、D-2 负责乙地区段
        fixture = CharterFixture()
        payload = fixture.payload
        driver_one = payload["drivers"][0]
        driver_two = {
            "driver_id": "D-2", "name": "李师傅",
            "qualifications": [valid_license("driver_license"),
                               valid_license("driver_qualification")],
            "duty_periods": [
                {"on": "2026-10-02T07:00:00", "off": "2026-10-02T18:00:00"}],
            "rest_periods": [
                {"start": "2026-10-02T12:00:00", "end": "2026-10-02T12:30:00"}]}
        payload["drivers"] = [
            {**driver_one, "duty_periods": [
                {"on": "2026-10-01T07:00:00", "off": "2026-10-01T19:00:00"}],
             "rest_periods": [
                {"start": "2026-10-01T12:00:00", "end": "2026-10-01T12:30:00"}]},
            driver_two]
        payload["segments"][1]["driver_id"] = "D-2"
        self.service.submit_charter_group(
            request_id="grp-multi", actor_id="ag1", group_id="G-M", **payload)
        self.service.decide_segment(request_id="apA-m", actor_id="rvA", group_id="G-M",
                                    version=1, segment_id="seg-A", decision="approved")
        self.service.decide_segment(request_id="apB-m", actor_id="rvB", group_id="G-M",
                                    version=1, segment_id="seg-B", decision="approved")
        self.service.mark_trip_started(request_id="start-m", actor_id="ag1", group_id="G-M")

        substitute = {
            "driver_id": "D-3", "name": "王师傅",
            "qualifications": [valid_license("driver_license"),
                               valid_license("driver_qualification")],
            "duty_periods": [
                {"on": "2026-10-02T07:00:00", "off": "2026-10-02T18:00:00"}],
            "rest_periods": [
                {"start": "2026-10-02T12:00:00", "end": "2026-10-02T12:30:00"}]}
        self.service.register_trip_event(
            request_id="swap", actor_id="ag1", group_id="G-M", event_type="driver_change",
            old_driver_id="D-2", driver=substitute)
        view = self.service.get_group("G-M")
        seg_a, seg_b = view["current"]["segments"]
        self.assertEqual(1, seg_a["carried_from_version"])
        self.assertIsNone(seg_b["carried_from_version"])
        self.assertEqual("D-3", view["snapshot"]["segments"][1]["driver_id"])

    def test_passenger_change_within_tolerance_keeps_approvals(self):
        self._start()
        result = self.service.register_trip_event(
            request_id="pax", actor_id="ag1", group_id="G-1",
            event_type="pax_change", passenger_count=42)
        self.assertEqual("none", result["impact_scope"])
        self.assertTrue(result["executable"])
        view = self.service.get_group("G-1")
        self.assertTrue(all(s["carried_from_version"] == 1
                            for s in view["current"]["segments"]))

    def test_large_passenger_change_forces_full_rereview(self):
        self._start()
        result = self.service.register_trip_event(
            request_id="pax", actor_id="ag1", group_id="G-1",
            event_type="pax_change", passenger_count=45)
        # 40 -> 45 超出 10%（容差为 4）
        self.assertEqual("all", result["impact_scope"])
        self.assertFalse(result["executable"])

    def test_vehicle_change_before_start_requires_full_rereview(self):
        self._submit()
        self._approve_chain()
        new_vehicle = {"vehicle_id": "V-2", "seat_capacity": 50,
                       "qualifications": [valid_license("vehicle_license"),
                                          valid_license("carrier_insurance")]}
        result = self.service.register_trip_event(
            request_id="veh", actor_id="ag1", group_id="G-1",
            event_type="vehicle_change", vehicle=new_vehicle)
        self.assertEqual("all", result["impact_scope"])
        self.assertEqual([], result["carried_segments"])
        self.assertEqual("in_review", result["head_status"])

    def test_cancel_preserves_approvals_and_states_refund(self):
        self._start()
        result = self.service.register_trip_event(
            request_id="cancel", actor_id="ag1", group_id="G-1",
            event_type="cancel", cause="force_majeure")
        self.assertEqual("all", result["impact_scope"])
        self.assertEqual("扣除已发生费用后退款",
                         result["refund_responsibility"]["term"])
        view = self.service.get_group("G-1")
        self.assertEqual("cancelled", view["head_status"])
        self.assertFalse(view["executable"])
        # 新版本保留了原批准事实（以 revoked 记录且标注来源版本）
        rows = self.database.connection.execute(
            "SELECT decision,carried_from_version FROM segment_reviews "
            "WHERE group_id='G-1' AND version=2").fetchall()
        self.assertTrue(all(row["decision"] == "revoked" for row in rows))
        self.assertTrue(all(row["carried_from_version"] == 1 for row in rows))

    def test_agency_fault_refund_when_blocked_before_departure(self):
        # 驾驶员证照缺失导致无法签发
        driver = [{**CharterFixture().payload["drivers"][0]}]
        driver[0]["qualifications"] = [valid_license("driver_license")]
        self._submit(overrides={"drivers": driver})
        view = self.service.get_group("G-1")
        self.assertEqual("agency_fault", view["refund_responsibility"]["cause"])
        self.assertEqual("旅行社承担全额退款", view["refund_responsibility"]["term"])

    # ------------------------------------------------------------ 执法核验

    def test_inspection_requires_key(self):
        self._start()
        with self.assertRaises(PermissionDenied):
            self.service.inspection_verify(api_key="bad", group_id="G-1")

    def test_inspection_shows_permits_regions_and_exception_basis(self):
        self._start()
        self.service.register_trip_event(
            request_id="close", actor_id="ag1", group_id="G-1", event_type="road_closure",
            affected_segment_ids=["seg-B"],
            reroute={"seg-B": {"route_code": "route-B1", "from_site": "济南", "to_site": "南京",
                               "region_code": "R-B", "ordinal": 2, "driver_id": "D-1",
                               "departure_at": "2026-10-02T09:00:00",
                               "arrive_at": "2026-10-02T16:00:00"}})
        self.service.mint_inspection_key(request_id="key", actor_id="a1", key="enf")
        result = self.service.inspection_verify(api_key="enf", group_id="G-1")
        self.assertEqual(2, result["current_version"])
        self.assertEqual({"R-A", "R-B"}, {s["region_code"] for s in result["segments"]})
        seg_a = next(s for s in result["segments"] if s["segment_id"] == "seg-A")
        self.assertTrue(seg_a["effective"])
        self.assertEqual("lic-A", seg_a["permit"]["basis_license_id"])
        self.assertEqual(1, seg_a["permit"]["carried_from_version"])
        events = {e["event_type"] for e in result["exception_handling"]}
        self.assertIn("road_closure", events)
        closure = next(e for e in result["exception_handling"]
                       if e["event_type"] == "road_closure")
        self.assertEqual(["seg-B"], closure["affected_segments"])

    def test_region_scoped_key_only_sees_own_region(self):
        self._start()
        self.service.mint_inspection_key(request_id="keyB", actor_id="a1",
                                         key="enf-b", region_code="R-B")
        result = self.service.inspection_verify(api_key="enf-b", group_id="G-1")
        self.assertEqual(["seg-B"], [s["segment_id"] for s in result["segments"]])

    def test_suspended_license_surfaces_as_exception(self):
        self._start()
        # 已在途时辖区许可被暂停（如封路），核验应暴露异常
        self.service.update_region_license_status(
            request_id="susp", actor_id="rvB", license_id="lic-B", status="suspended")
        self.service.mint_inspection_key(request_id="key", actor_id="a1", key="enf")
        result = self.service.inspection_verify(api_key="enf", group_id="G-1")
        seg_b = next(s for s in result["segments"] if s["segment_id"] == "seg-B")
        self.assertFalse(seg_b["effective"])
        self.assertEqual("permit.region_suspended",
                         seg_b["blocking_findings"][0]["code"])


if __name__ == "__main__":
    unittest.main()
