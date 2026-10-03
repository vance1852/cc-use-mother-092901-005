"""运行跨区域旅游包车联审与履约服务的离线端到端验收。

验收覆盖：基础登记链、辖区许可登记、团次提交与跨辖区许可链签发、
途中封路局部重审且原批准沿用、执法核验与退款责任，最后核对哈希审计链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .charter import CharterService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def _valid_license(qtype: str, **overrides) -> dict:
    license = {"type": qtype, "valid_from": "2026-01-01T00:00:00",
               "valid_to": "2027-01-01T00:00:00"}
    license.update(overrides)
    return license


def run() -> dict[str, object]:
    """执行一条完整联审履约链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        charter = CharterService(database, clock)

        # 基础登记链
        base.register_organization(request_id="req-org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范旅行社")
        base.register_actor(request_id="req-admin", actor_id="bootstrap",
                            new_actor_id="admin-001", display_name="系统管理员",
                            role="admin", organization_id="org-001")
        base.register_actor(request_id="req-agent", actor_id="admin-001",
                            new_actor_id="agent-001", display_name="旅行社经办人",
                            role="operator", organization_id="org-001")
        base.register_actor(request_id="req-rva", actor_id="admin-001",
                            new_actor_id="rva-001", display_name="甲地区审批人",
                            role="reviewer", organization_id="org-001")
        base.register_actor(request_id="req-rvb", actor_id="admin-001",
                            new_actor_id="rvb-001", display_name="乙地区审批人",
                            role="reviewer", organization_id="org-001")
        charter.assign_reviewer_region(request_id="req-region-a", actor_id="admin-001",
                                       reviewer_actor_id="rva-001", region_code="R-A")
        charter.assign_reviewer_region(request_id="req-region-b", actor_id="admin-001",
                                       reviewer_actor_id="rvb-001", region_code="R-B")
        charter.register_region_license(
            request_id="req-lic-a", actor_id="rva-001", license_id="lic-A",
            region_code="R-A", valid_from="2026-10-01T00:00:00",
            valid_to="2026-10-03T00:00:00", route_code="route-A1")
        charter.register_region_license(
            request_id="req-lic-b", actor_id="rvb-001", license_id="lic-B",
            region_code="R-B", valid_from="2026-10-01T00:00:00",
            valid_to="2026-10-03T00:00:00", route_code="route-B1")

        # 团次提交：跨两日、两辖区、含中途休息与停靠计划、含退款承诺
        vehicle = {"vehicle_id": "V-1", "seat_capacity": 45,
                   "qualifications": [_valid_license("vehicle_license"),
                                      _valid_license("carrier_insurance")]}
        driver = {"driver_id": "D-1",
                  "qualifications": [_valid_license("driver_license"),
                                     _valid_license("driver_qualification")],
                  "duty_periods": [
                      {"on": "2026-10-01T07:00:00", "off": "2026-10-01T19:00:00"},
                      {"on": "2026-10-02T07:00:00", "off": "2026-10-02T18:00:00"}],
                  "rest_periods": [
                      {"start": "2026-10-01T12:00:00", "end": "2026-10-01T12:30:00"},
                      {"start": "2026-10-02T12:00:00", "end": "2026-10-02T12:30:00"}]}
        segments = [
            {"segment_id": "seg-A", "region_code": "R-A", "ordinal": 1,
             "route_code": "route-A1", "from_site": "北京", "to_site": "济南",
             "departure_at": "2026-10-01T08:00:00", "arrive_at": "2026-10-01T16:00:00",
             "driver_id": "D-1"},
            {"segment_id": "seg-B", "region_code": "R-B", "ordinal": 2,
             "route_code": "route-B1", "from_site": "济南", "to_site": "南京",
             "departure_at": "2026-10-02T08:00:00", "arrive_at": "2026-10-02T16:00:00",
             "driver_id": "D-1"}]
        contract = {"contract_no": "C-2026-001", "refund_policy": {
            "plan_change": "出发前7日全额退款", "agency_fault": "旅行社承担全额退款",
            "force_majeure": "扣除已发生费用后退款"}}
        charter.submit_charter_group(
            request_id="req-group", actor_id="agent-001", group_id="G-1",
            contract=contract,
            tour={"tour_code": "T-01", "title": "国庆江南游", "passenger_count": 40},
            vehicle=vehicle, drivers=[driver], segments=segments,
            stops=[{"stop_id": "stop1", "segment_id": "seg-A",
                    "planned_at": "2026-10-01T12:00:00"}])
        submitted = charter.get_group("G-1")

        # 各辖区只审批本辖区区段，串联成许可链
        charter.decide_segment(request_id="req-ap-a", actor_id="rva-001", group_id="G-1",
                               version=1, segment_id="seg-A", decision="approved")
        charter.decide_segment(request_id="req-ap-b", actor_id="rvb-001", group_id="G-1",
                               version=1, segment_id="seg-B", decision="approved")
        issued = charter.get_group("G-1")

        # 发车
        charter.mark_trip_started(request_id="req-start", actor_id="agent-001", group_id="G-1")

        # 途中乙地区临时封路：只影响 seg-B，改线后重审，seg-A 的原批准沿用
        charter.register_trip_event(
            request_id="req-closure", actor_id="agent-001", group_id="G-1",
            event_type="road_closure", affected_segment_ids=["seg-B"],
            reroute={"seg-B": {"route_code": "route-B1", "from_site": "济南",
                               "to_site": "南京", "region_code": "R-B", "ordinal": 2,
                               "driver_id": "D-1", "departure_at": "2026-10-02T09:00:00",
                               "arrive_at": "2026-10-02T16:00:00"}})
        closure_view = charter.get_group("G-1")
        charter.decide_segment(request_id="req-ap-b2", actor_id="rvb-001", group_id="G-1",
                               version=2, segment_id="seg-B", decision="approved")
        recovered = charter.get_group("G-1")

        # 执法核验密钥与核验结论
        charter.mint_inspection_key(request_id="req-key", actor_id="admin-001", key="enf-key")
        verification = charter.inspection_verify(api_key="enf-key", group_id="G-1")

        valid, event_count = base.verify_audit()
        seg_a_v2 = next(s for s in closure_view["current"]["segments"]
                        if s["segment_id"] == "seg-A")
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "submitted_executable": submitted["executable"],
            "issued_executable": issued["executable"],
            "issued_status": issued["head_status"],
            "closure_version": closure_view["current_version"],
            "closure_head_status": closure_view["head_status"],
            "seg_a_carried_from_version": seg_a_v2["carried_from_version"],
            "recovered_executable": recovered["executable"],
            "inspection_segments": [
                {"segment_id": s["segment_id"], "region_code": s["region_code"],
                 "effective": s["effective"], "basis_license_id": s["permit"]["basis_license_id"]}
                for s in verification["segments"]],
            "inspection_event_types": [e["event_type"] for e in verification["exception_handling"]],
        }
        database.close()
        expectations = [
            result["submitted_executable"] is False,
            result["issued_executable"] is True,
            result["closure_version"] == 2,
            result["closure_head_status"] == "in_progress",
            result["seg_a_carried_from_version"] == 1,
            result["recovered_executable"] is True,
            len(result["inspection_segments"]) == 2,
            all(s["effective"] for s in result["inspection_segments"]),
        ]
        result["status"] = "ok" if valid and all(expectations) else "failed"
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
