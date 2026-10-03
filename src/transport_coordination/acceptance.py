"""运行基础服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .charter import CharterService
from .clock import FixedClock
from .storage import Database


def run() -> dict[str, object]:
    """执行基础登记链与旅游包车联审全链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = CharterService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        # ---- 基础登记链 ----
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范运营机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="运营负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号交通节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        records = service.list_domain_data("site-001")
        charter = _run_charter(service)
        valid, event_count = service.verify_audit()
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, "charter": charter}
        database.close()
        return result


def _run_charter(service: CharterService) -> dict[str, object]:
    """旅游包车：辖区联审、许可链、封路重审、沿用与执法核验。"""

    service.register_organization(request_id="c-org-agency", actor_id="admin-001",
                                  organization_id="org-agency", name="示范旅行社")
    service.register_organization(request_id="c-org-a-tr", actor_id="admin-001",
                                  organization_id="org-a-tr", name="A市交通运输局")
    service.register_organization(request_id="c-org-a-tv", actor_id="admin-001",
                                  organization_id="org-a-tv", name="A市文旅局")
    service.register_actor(request_id="c-ag", actor_id="admin-001", new_actor_id="ag-001",
                           display_name="团调", role="operator", organization_id="org-agency")
    service.register_actor(request_id="c-atr", actor_id="admin-001", new_actor_id="atr-001",
                           display_name="A市交通审批", role="reviewer", organization_id="org-a-tr")
    service.register_actor(request_id="c-atv", actor_id="admin-001", new_actor_id="atv-001",
                           display_name="A市文旅审批", role="reviewer", organization_id="org-a-tv")
    service.register_actor(request_id="c-enf", actor_id="admin-001", new_actor_id="enf-001",
                           display_name="执法员", role="enforcer", organization_id="org-a-tr")
    service.register_region(request_id="c-region-a", actor_id="admin-001", region_code="RA",
                            name="A市", transport_org_id="org-a-tr", tourism_org_id="org-a-tv")
    service.register_vehicle(request_id="c-veh", actor_id="admin-001", vehicle_id="veh-001",
                             owner_org_id="org-agency", plate="京A00001", seat_count=45,
                             transport_license_no="YL-0001",
                             license_valid_until="2027-01-01", insurance_valid_until="2027-01-01")
    service.register_driver(request_id="c-drv", actor_id="admin-001", driver_id="drv-001",
                            owner_org_id="org-agency", display_name="张师傅",
                            qualification_no="ZG-0001", qualification_valid_until="2027-01-01")
    contract = {"signer": "示范旅行社", "signed_at": "2026-09-26T10:00:00+08:00",
                "terms": ["承运人责任险", "节假日运力承诺"]}
    segments = [
        {"seq": 1, "region_code": "RA", "road_from": "A市中心站", "road_to": "A市景区",
         "depart_at": "2026-10-02T07:00:00+08:00", "arrive_at": "2026-10-02T10:00:00+08:00"},
    ]
    service.file_trip(request_id="c-file", actor_id="ag-001", filing_id="trip-001",
                      tour_code="GOLD-001", passenger_count=38, vehicle_id="veh-001",
                      driver_id="drv-001", contract=contract, segments=segments,
                      stops=[{"seq": 1, "segment_seq": 1, "name": "服务区",
                              "region_code": "RA",
                              "arrive_at": "2026-10-02T08:00:00+08:00",
                              "leave_at": "2026-10-02T08:20:00+08:00"}])
    view = service.get_filing("ag-001", "trip-001")
    permit_id = view["permits"][0]["permit_id"]
    service.decide_permit(request_id="c-dec-t", actor_id="atr-001", permit_id=permit_id,
                          decision="approve", department="transport", conditions=["限速80"])
    service.decide_permit(request_id="c-dec-v", actor_id="atv-001", permit_id=permit_id,
                          decision="approve", department="tourism")
    certificate = service.issue_certificate(request_id="c-cert", actor_id="ag-001",
                                            filing_id="trip-001")
    service.start_trip(request_id="c-start", actor_id="ag-001", filing_id="trip-001")
    service.register_closure(request_id="c-closure", actor_id="atr-001", closure_id="cls-001",
                             region_code="RA", route_label="A市景区",
                             valid_from="2026-10-02T09:00:00+08:00",
                             valid_to="2026-10-02T12:00:00+08:00", reason="景区周边临时管制")
    amendment = service.amend_filing(request_id="c-amend", actor_id="ag-001",
                                     filing_id="trip-001", kind="road_closure",
                                     reason="景区道路管制，改停备用停车场",
                                     changes={"closure_id": "cls-001"})
    enforcement = service.enforcement_check(actor_id="enf-001",
                                            certificate_no=certificate.data["certificate_no"],
                                            at="2026-10-02T09:30:00+08:00")
    return {"certificate_no": certificate.data["certificate_no"],
            "permit_chain_head": certificate.data["chain_head"],
            "amendment_affected_seqs": amendment.data["affected_seqs"],
            "amendment_reused_seqs": amendment.data["reused_seqs"],
            "refund_party": amendment.data["refund"]["responsible_party"],
            "enforcement_valid_during_rereview": enforcement["valid"],
            "exceptions": [item["type"] for item in enforcement["exceptions"]]}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
