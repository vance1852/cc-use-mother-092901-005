"""旅游包车联审与履约领域服务。

在基础服务（权限、幂等、事务、哈希审计链）之上实现：

- 辖区责任登记（交通 / 文旅双部门）与车辆、驾驶员证照台账；
- 旅行社团次申报：车辆、驾驶员、线路区段、停靠计划、合同承诺；
- 各辖区审批人只处理本辖区区段，两方决策串联成许可链，齐备后签发可执行行程单；
- 规则识别：证照有效期、连续驾驶与跨日工时、超员、资源互斥与重复申报、临时封路；
- 修订（封路、人数变化、换车、替班、行程调整、取消）按影响范围重审，
  未受影响区段沿用原批准事实，已开始的行程不会被普通改动整体回退；
- 执法核验视图（当前有效许可、责任地区、异常处置依据）与
  旅行社视图（阻塞原因、退款责任、可沿用审批）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService


# 驾驶与任务工时约束（小时）
MAX_CONTINUOUS_DRIVE_HOURS = 4.0
MAX_DAILY_DRIVE_HOURS = 8.0
MIN_CROSS_DAY_REST_HOURS = 11.0
MAX_MISSION_SPAN_HOURS = 48.0

REVIEW_ROLES = ("reviewer", "admin")
CARRIER_ROLES = ("operator", "admin")
ENFORCEMENT_ROLES = ("enforcer", "auditor", "admin")

# 申报或修订时直接拒绝的硬伤；工时类规则允许申报但阻塞许可批准与行程单签发。
HARD_BLOCK_CODES = frozenset({
    "vehicle_unknown", "driver_unknown", "vehicle_inactive", "driver_inactive",
    "road_transport_license_expired", "insurance_expired", "driver_qualification_expired",
    "over_capacity", "vehicle_conflict", "driver_conflict",
})
APPROVAL_GATE_CODES = HARD_BLOCK_CODES | {
    "continuous_drive_overrun", "daily_drive_overrun", "cross_day_rest_insufficient",
    "mission_span_overrun", "new_closure_not_reviewed",
}

FILING_TERMINAL = {"cancelled", "completed"}
DEPARTMENTS = {"transport", "tourism"}
AMEND_KINDS = {"road_closure", "passenger_change", "vehicle_change",
               "driver_change", "itinerary_change", "cancel"}


@dataclass(frozen=True)
class CharterActionResult:
    """一次幂等写操作的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "resource_type": self.resource_type,
                "resource_id": self.resource_id, "replayed": self.replayed, **self.data}


def parse_dt(value: Any, field: str) -> datetime:
    """解析必须携带时区的 ISO-8601 时间。"""

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO-8601 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    return parsed


def parse_day(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{field} 日期格式无效") from exc


def _hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


class CharterService(DomainService):
    """协调旅游包车联审、许可链、行程单与履约变更。"""

    # ------------------------------------------------------------------ 基础

    def _idem(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
              create) -> CharterActionResult:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return CharterActionResult(request_id, row["resource_type"], row["resource_id"],
                                       True, json.loads(row["response_json"]))
        resource_type, resource_id, data = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(data), self._now()),
        )
        return CharterActionResult(request_id, resource_type, resource_id, False, data)

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _region(self, connection, region_code: str):
        row = connection.execute(
            "SELECT * FROM charter_regions WHERE region_code=?", (region_code,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"辖区 {region_code} 尚未登记责任部门")
        return row

    def _region_department(self, connection, actor, region_code: str, department: str):
        """校验操作者代表某辖区的交通或文旅部门。"""

        if department not in DEPARTMENTS:
            raise ValidationError("department 必须是 transport 或 tourism")
        region = self._region(connection, region_code)
        if actor.role != "admin":
            target_org = region[f"{department}_org_id"]
            if target_org is None:
                raise PermissionDenied(f"辖区 {region_code} 未配置文旅责任部门")
            if actor.organization_id != target_org:
                raise PermissionDenied("只能审批本辖区责任区段")
        return region

    # ----------------------------------------------------------- 台账登记

    def register_region(self, *, request_id: str, actor_id: str, region_code: str, name: str,
                        transport_org_id: str, tourism_org_id: str | None = None) -> CharterActionResult:
        payload = {"actor_id": actor_id, "region_code": region_code, "name": name,
                   "transport_org_id": transport_org_id, "tourism_org_id": tourism_org_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            region_code = self._identifier(region_code, "region_code")
            name = self._text(name, "name")
            transport_org_id = self._identifier(transport_org_id, "transport_org_id")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (transport_org_id,)).fetchone() is None:
                raise NotFoundError("交通主管部门组织不存在")
            if tourism_org_id:
                tourism_org_id = self._identifier(tourism_org_id, "tourism_org_id")
                if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                (tourism_org_id,)).fetchone() is None:
                    raise NotFoundError("文旅主管部门组织不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO charter_regions(region_code,name,transport_org_id,"
                        "tourism_org_id,created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (region_code, name, transport_org_id, tourism_org_id, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("辖区编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="charter.region.registered",
                            resource_type="charter_region", resource_id=region_code,
                            detail={"name": name, "transport_org_id": transport_org_id,
                                    "tourism_org_id": tourism_org_id})
                data = {"region_code": region_code, "name": name,
                        "transport_org_id": transport_org_id, "tourism_org_id": tourism_org_id}
                return "charter_region", region_code, data

            return self._idem(conn, request_id=request_id, action="charter_register_region",
                              payload=payload, create=create)

    def register_vehicle(self, *, request_id: str, actor_id: str, vehicle_id: str,
                         owner_org_id: str, plate: str, seat_count: int,
                         transport_license_no: str, license_valid_until: str,
                         insurance_valid_until: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "vehicle_id": vehicle_id, "owner_org_id": owner_org_id,
                   "plate": plate, "seat_count": seat_count,
                   "transport_license_no": transport_license_no,
                   "license_valid_until": license_valid_until,
                   "insurance_valid_until": insurance_valid_until}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            if actor.role != "admin" and actor.organization_id != owner_org_id:
                raise PermissionDenied("不能为其他客运企业登记车辆")
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            owner_org_id = self._identifier(owner_org_id, "owner_org_id")
            plate = self._text(plate, "plate", 20)
            if not isinstance(seat_count, int) or seat_count <= 0:
                raise ValidationError("seat_count 必须是正整数")
            transport_license_no = self._text(transport_license_no, "transport_license_no", 60)
            license_day = parse_day(license_valid_until, "license_valid_until")
            insurance_day = parse_day(insurance_valid_until, "insurance_valid_until")

            def create():
                if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                (owner_org_id,)).fetchone() is None:
                    raise NotFoundError("客运企业组织不存在")
                try:
                    conn.execute(
                        "INSERT INTO charter_vehicles(vehicle_id,owner_org_id,plate,seat_count,"
                        "transport_license_no,license_valid_until,insurance_valid_until,"
                        "active,created_by,created_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (vehicle_id, owner_org_id, plate, seat_count, transport_license_no,
                         license_day.isoformat(), insurance_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("车辆编号或号牌已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="charter.vehicle.registered",
                            resource_type="charter_vehicle", resource_id=vehicle_id,
                            detail={"plate": plate, "owner_org_id": owner_org_id,
                                    "license_valid_until": license_day.isoformat()})
                return "charter_vehicle", vehicle_id, {"vehicle_id": vehicle_id, "plate": plate}

            return self._idem(conn, request_id=request_id, action="charter_register_vehicle",
                              payload=payload, create=create)

    def register_driver(self, *, request_id: str, actor_id: str, driver_id: str,
                        owner_org_id: str, display_name: str, qualification_no: str,
                        qualification_valid_until: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "driver_id": driver_id, "owner_org_id": owner_org_id,
                   "display_name": display_name, "qualification_no": qualification_no,
                   "qualification_valid_until": qualification_valid_until}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            if actor.role != "admin" and actor.organization_id != owner_org_id:
                raise PermissionDenied("不能为其他客运企业登记驾驶员")
            driver_id = self._identifier(driver_id, "driver_id")
            owner_org_id = self._identifier(owner_org_id, "owner_org_id")
            display_name = self._text(display_name, "display_name", 60)
            qualification_no = self._text(qualification_no, "qualification_no", 60)
            valid_day = parse_day(qualification_valid_until, "qualification_valid_until")

            def create():
                if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                (owner_org_id,)).fetchone() is None:
                    raise NotFoundError("客运企业组织不存在")
                try:
                    conn.execute(
                        "INSERT INTO charter_drivers(driver_id,owner_org_id,display_name,"
                        "qualification_no,qualification_valid_until,active,created_by,created_at)"
                        " VALUES(?,?,?,?,?,1,?,?)",
                        (driver_id, owner_org_id, display_name, qualification_no,
                         valid_day.isoformat(), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("驾驶员编号或资格证号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="charter.driver.registered",
                            resource_type="charter_driver", resource_id=driver_id,
                            detail={"display_name": display_name, "owner_org_id": owner_org_id,
                                    "qualification_valid_until": valid_day.isoformat()})
                return "charter_driver", driver_id, {"driver_id": driver_id}

            return self._idem(conn, request_id=request_id, action="charter_register_driver",
                              payload=payload, create=create)

    def register_closure(self, *, request_id: str, actor_id: str, closure_id: str,
                         region_code: str, route_label: str, valid_from: str,
                         valid_to: str, reason: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "closure_id": closure_id, "region_code": region_code,
                   "route_label": route_label, "valid_from": valid_from, "valid_to": valid_to,
                   "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *REVIEW_ROLES)
            region = self._region(conn, region_code)
            if actor.role != "admin" and actor.organization_id not in (
                    region["transport_org_id"], region["tourism_org_id"]):
                raise PermissionDenied("只能登记本辖区的封路信息")
            closure_id = self._identifier(closure_id, "closure_id")
            route_label = self._text(route_label, "route_label", 120)
            reason = self._text(reason, "reason", 300)
            start_dt = parse_dt(valid_from, "valid_from")
            end_dt = parse_dt(valid_to, "valid_to")
            if end_dt <= start_dt:
                raise ValidationError("封路结束时间必须晚于开始时间")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO charter_closures(closure_id,region_code,route_label,"
                        "valid_from,valid_to,reason,active,created_by,created_at)"
                        " VALUES(?,?,?,?,?,?,1,?,?)",
                        (closure_id, region_code, route_label,
                         start_dt.isoformat(), end_dt.isoformat(), reason, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("封路编号已经存在") from exc
                self._audit(conn, actor_id=actor_id, action="charter.closure.registered",
                            resource_type="charter_closure", resource_id=closure_id,
                            detail={"region_code": region_code, "route_label": route_label,
                                    "valid_from": start_dt.isoformat(),
                                    "valid_to": end_dt.isoformat(), "reason": reason})
                data = {"closure_id": closure_id, "region_code": region_code,
                        "route_label": route_label,
                        "valid_from": start_dt.isoformat(), "valid_to": end_dt.isoformat()}
                return "charter_closure", closure_id, data

            return self._idem(conn, request_id=request_id, action="charter_register_closure",
                              payload=payload, create=create)

    # ----------------------------------------------------------- 申报校验

    @staticmethod
    def _normalize_segments(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("segments 必须是非空数组")
        segments: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError(f"segments[{index}] 必须是对象")
            try:
                seq = int(item["seq"])
                road_from = str(item["road_from"]).strip()
                road_to = str(item["road_to"]).strip()
                depart = parse_dt(item["depart_at"], f"segments[{index}].depart_at")
                arrive = parse_dt(item["arrive_at"], f"segments[{index}].arrive_at")
            except KeyError as exc:
                raise ValidationError(f"segments[{index}] 缺少字段 {exc.args[0]}") from exc
            region_code = str(item.get("region_code", "")).strip()
            if not region_code:
                raise ValidationError(f"segments[{index}].region_code 不能为空")
            if not road_from or not road_to:
                raise ValidationError(f"segments[{index}] 起讫点不能为空")
            if arrive <= depart:
                raise ValidationError(f"区段 {seq} 到达时间必须晚于出发时间")
            segments.append({"seq": seq, "region_code": region_code, "road_from": road_from,
                             "road_to": road_to, "depart_at": depart, "arrive_at": arrive})
        segments.sort(key=lambda item: item["seq"])
        seqs = [item["seq"] for item in segments]
        if seqs != list(range(1, len(segments) + 1)):
            raise ValidationError("区段 seq 必须从 1 开始且连续唯一")
        for previous, current in zip(segments, segments[1:]):
            if current["depart_at"] < previous["arrive_at"]:
                raise ValidationError(f"区段 {current['seq']} 出发早于上一区段到达")
        return segments

    def _normalize_stops(self, raw: Any, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValidationError("stops 必须是数组")
        by_seq = {item["seq"]: item for item in segments}
        stops: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValidationError(f"stops[{index}] 必须是对象")
            try:
                seq = int(item["seq"])
                segment_seq = int(item["segment_seq"])
                name = str(item["name"]).strip()
                arrive = parse_dt(item["arrive_at"], f"stops[{index}].arrive_at")
                leave = parse_dt(item["leave_at"], f"stops[{index}].leave_at")
            except KeyError as exc:
                raise ValidationError(f"stops[{index}] 缺少字段 {exc.args[0]}") from exc
            if not name:
                raise ValidationError(f"stops[{index}].name 不能为空")
            if segment_seq not in by_seq:
                raise ValidationError(f"停靠 {seq} 引用了不存在的区段 {segment_seq}")
            segment = by_seq[segment_seq]
            # 停靠点的责任辖区跟随所属区段：联审许可按区段辖区划分，避免责任缺口。
            region_code = segment["region_code"]
            if arrive < segment["depart_at"] or leave > segment["arrive_at"]:
                raise ValidationError(f"停靠 {seq} 不在区段 {segment_seq} 的行驶时间内")
            if leave < arrive:
                raise ValidationError(f"停靠 {seq} 离开时间早于到达时间")
            stops.append({"seq": seq, "segment_seq": segment_seq, "name": name,
                          "region_code": region_code, "arrive_at": arrive, "leave_at": leave})
        stops.sort(key=lambda item: (item["segment_seq"], item["seq"]))
        if [item["seq"] for item in stops] != list(range(1, len(stops) + 1)):
            raise ValidationError("停靠 seq 必须从 1 开始且连续唯一")
        return stops

    @staticmethod
    def _validate_contract(contract: Any) -> dict[str, Any]:
        if not isinstance(contract, dict):
            raise ValidationError("contract 必须是合同承诺对象")
        signer = str(contract.get("signer", "")).strip()
        if not signer:
            raise ValidationError("contract.signer 不能为空")
        signed_at = parse_dt(contract.get("signed_at"), "contract.signed_at")
        terms = contract.get("terms", [])
        if not isinstance(terms, list):
            raise ValidationError("contract.terms 必须是数组")
        return {"signer": signer, "signed_at": signed_at.isoformat(),
                "terms": [str(term) for term in terms]}

    def _work_rule_blockers(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for segment in segments:
            duration = _hours(segment["depart_at"], segment["arrive_at"])
            if duration > MAX_CONTINUOUS_DRIVE_HOURS:
                blockers.append({"code": "continuous_drive_overrun", "segment_seq": segment["seq"],
                                 "message": f"区段 {segment['seq']} 连续驾驶 {duration:.1f} 小时，"
                                            f"超过 {MAX_CONTINUOUS_DRIVE_HOURS:.0f} 小时上限"})
        # 按出发日归集，识别跨日工时
        days: dict[date, list[dict[str, Any]]] = {}
        for segment in segments:
            days.setdefault(segment["depart_at"].date(), []).append(segment)
        day_keys = sorted(days)
        for day_key in day_keys:
            total = sum(_hours(item["depart_at"], item["arrive_at"]) for item in days[day_key])
            if total > MAX_DAILY_DRIVE_HOURS:
                blockers.append({"code": "daily_drive_overrun", "day": day_key.isoformat(),
                                 "message": f"{day_key.isoformat()} 累计驾驶 {total:.1f} 小时，"
                                            f"超过 {MAX_DAILY_DRIVE_HOURS:.0f} 小时上限"})
        for previous_day, next_day in zip(day_keys, day_keys[1:]):
            last_arrive = max(item["arrive_at"] for item in days[previous_day])
            first_depart = min(item["depart_at"] for item in days[next_day])
            rest = _hours(last_arrive, first_depart)
            if rest < MIN_CROSS_DAY_REST_HOURS:
                blockers.append({"code": "cross_day_rest_insufficient",
                                 "from_day": previous_day.isoformat(),
                                 "to_day": next_day.isoformat(),
                                 "message": f"{previous_day.isoformat()} 至 {next_day.isoformat()} "
                                            f"跨日衔接休息仅 {rest:.1f} 小时，不足 "
                                            f"{MIN_CROSS_DAY_REST_HOURS:.0f} 小时"})
        span = _hours(segments[0]["depart_at"], segments[-1]["arrive_at"])
        if span > MAX_MISSION_SPAN_HOURS:
            blockers.append({"code": "mission_span_overrun",
                             "message": f"连续任务跨度 {span:.1f} 小时，超过 "
                                        f"{MAX_MISSION_SPAN_HOURS:.0f} 小时上限"})
        return blockers

    def _active_closures(self, conn, at: datetime | None = None):
        rows = conn.execute("SELECT * FROM charter_closures WHERE active=1 ORDER BY valid_from").fetchall()
        closures = [dict(row) for row in rows]
        if at is not None:
            closures = [item for item in closures
                        if parse_dt(item["valid_from"], "valid_from") <= at
                        <= parse_dt(item["valid_to"], "valid_to")]
        return closures

    def _closure_impacts(self, closures: Iterable[dict[str, Any]],
                         segment: dict[str, Any]) -> list[dict[str, Any]]:
        impacts = []
        for closure in closures:
            valid_from = parse_dt(closure["valid_from"], "valid_from")
            valid_to = parse_dt(closure["valid_to"], "valid_to")
            same_region = closure["region_code"] == segment["region_code"]
            window_overlap = valid_from <= segment["arrive_at"] and valid_to >= segment["depart_at"]
            route_hit = closure["route_label"] in (segment["road_from"], segment["road_to"])
            if same_region and window_overlap and route_hit:
                impacts.append(closure)
        return impacts

    def _resource_blockers(self, conn, *, vehicle_id: str, driver_id: str,
                           passenger_count: int, segments: list[dict[str, Any]],
                           planned_end: datetime, exclude_filing_id: str | None) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        vehicle = conn.execute("SELECT * FROM charter_vehicles WHERE vehicle_id=?",
                               (vehicle_id,)).fetchone()
        driver = conn.execute("SELECT * FROM charter_drivers WHERE driver_id=?",
                              (driver_id,)).fetchone()
        if vehicle is None:
            blockers.append({"code": "vehicle_unknown", "message": f"车辆 {vehicle_id} 未登记"})
        else:
            if not vehicle["active"]:
                blockers.append({"code": "vehicle_inactive", "message": "车辆已被停用"})
            end_day = planned_end.date()
            license_day = date.fromisoformat(vehicle["license_valid_until"])
            insurance_day = date.fromisoformat(vehicle["insurance_valid_until"])
            if license_day < end_day:
                blockers.append({"code": "road_transport_license_expired",
                                 "valid_until": vehicle["license_valid_until"],
                                 "message": "道路运输证有效期不能覆盖整个行程"})
            if insurance_day < end_day:
                blockers.append({"code": "insurance_expired",
                                 "valid_until": vehicle["insurance_valid_until"],
                                 "message": "承运人责任险有效期不能覆盖整个行程"})
            if passenger_count > vehicle["seat_count"]:
                blockers.append({"code": "over_capacity", "vehicle_id": vehicle_id,
                                 "message": f"旅客 {passenger_count} 人超过核定座位 {vehicle['seat_count']} 座"})
        if driver is None:
            blockers.append({"code": "driver_unknown", "message": f"驾驶员 {driver_id} 未登记"})
        else:
            if not driver["active"]:
                blockers.append({"code": "driver_inactive", "message": "驾驶员已被停用"})
            if date.fromisoformat(driver["qualification_valid_until"]) < planned_end.date():
                blockers.append({"code": "driver_qualification_expired",
                                 "valid_until": driver["qualification_valid_until"],
                                 "message": "从业资格证有效期不能覆盖整个行程"})
        blockers.extend(self._schedule_conflicts(conn, vehicle_id=vehicle_id, driver_id=driver_id,
                                                 segments=segments, exclude_filing_id=exclude_filing_id))
        return blockers

    def _schedule_conflicts(self, conn, *, vehicle_id: str, driver_id: str,
                            segments: list[dict[str, Any]],
                            exclude_filing_id: str | None) -> list[dict[str, Any]]:
        # 互斥来源一：当前版本已经取得区段批准，或处于可执行/执行/重审中的团次。
        rows = conn.execute(
            "SELECT s.filing_id,s.seq,s.depart_at,s.arrive_at,f.vehicle_id,f.driver_id,"
            "f.tour_code,f.status FROM charter_segments s "
            "JOIN charter_filings f ON f.filing_id=s.filing_id AND f.revision=s.revision "
            "WHERE f.status NOT IN ('permitting','cancelled','completed') "
            "OR EXISTS (SELECT 1 FROM charter_permits p WHERE p.filing_id=f.filing_id "
            "AND p.revision=f.revision AND p.status='approved')"
        ).fetchall()
        # 互斥来源二：仍有效（或重审中未作废）的行程单快照——修订改车/替班后，
        # 旧行程单在新版本签发前继续占用原车辆与驾驶员。
        cert_rows = conn.execute(
            "SELECT c.filing_id,c.snapshot_json,f.tour_code FROM charter_certificates c "
            "JOIN charter_filings f ON f.filing_id=c.filing_id "
            "WHERE c.status='active' AND f.status NOT IN ('cancelled','completed')"
        ).fetchall()
        candidates: list[dict[str, Any]] = [
            {"filing_id": row["filing_id"], "seq": row["seq"],
             "depart_at": row["depart_at"], "arrive_at": row["arrive_at"],
             "vehicle_id": row["vehicle_id"], "driver_id": row["driver_id"],
             "tour_code": row["tour_code"]} for row in rows]
        for row in cert_rows:
            if exclude_filing_id and row["filing_id"] == exclude_filing_id:
                continue
            snapshot = json.loads(row["snapshot_json"])
            for region in snapshot.get("regions", []):
                candidates.append({"filing_id": row["filing_id"], "seq": region["seq"],
                                   "depart_at": snapshot["planned_start"],
                                   "arrive_at": snapshot["planned_end"],
                                   "vehicle_id": snapshot["vehicle"]["vehicle_id"],
                                   "driver_id": snapshot["driver"]["driver_id"],
                                   "tour_code": row["tour_code"]})
        blockers: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            if exclude_filing_id and candidate["filing_id"] == exclude_filing_id:
                continue
            if candidate["vehicle_id"] != vehicle_id and candidate["driver_id"] != driver_id:
                continue
            for segment in segments:
                overlap = (parse_dt(candidate["depart_at"], "depart_at") < segment["arrive_at"]
                           and segment["depart_at"] < parse_dt(candidate["arrive_at"], "arrive_at"))
                if not overlap:
                    continue
                if candidate["vehicle_id"] == vehicle_id and \
                        ("vehicle", candidate["filing_id"]) not in seen:
                    seen.add(("vehicle", candidate["filing_id"]))
                    blockers.append({"code": "vehicle_conflict", "segment_seq": segment["seq"],
                                     "other_filing_id": candidate["filing_id"],
                                     "tour_code": candidate["tour_code"],
                                     "message": f"车辆在区段 {segment['seq']} 与团次 "
                                                f"{candidate['tour_code']} 时间互斥"})
                if candidate["driver_id"] == driver_id and \
                        ("driver", candidate["filing_id"]) not in seen:
                    seen.add(("driver", candidate["filing_id"]))
                    blockers.append({"code": "driver_conflict", "segment_seq": segment["seq"],
                                     "other_filing_id": candidate["filing_id"],
                                     "tour_code": candidate["tour_code"],
                                     "message": f"驾驶员在区段 {segment['seq']} 与团次 "
                                                f"{candidate['tour_code']} 时间互斥（疑似重复申报）"})
        return blockers

    def _closure_blockers(self, conn, segments: list[dict[str, Any]], revision: int,
                          filing_id: str) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
        closures = self._active_closures(conn)
        linked = {row["seq"]: row["closure_id"] for row in conn.execute(
            "SELECT seq,closure_id FROM charter_segment_closures WHERE filing_id=? AND revision=?",
            (filing_id, revision)).fetchall()}
        blockers: list[dict[str, Any]] = []
        impact_map: dict[int, list[str]] = {}
        for segment in segments:
            impacts = self._closure_impacts(closures, segment)
            impact_map[segment["seq"]] = [item["closure_id"] for item in impacts]
            for closure in impacts:
                if linked.get(segment["seq"]) != closure["closure_id"]:
                    blockers.append({"code": "new_closure_not_reviewed", "segment_seq": segment["seq"],
                                     "closure_id": closure["closure_id"],
                                     "message": f"区段 {segment['seq']} 受到封路 "
                                                f"{closure['closure_id']} 影响，需按封路发起重审"})
        return blockers, impact_map

    # ----------------------------------------------------------- 申报提交

    def file_trip(self, *, request_id: str, actor_id: str, filing_id: str, tour_code: str,
                  passenger_count: int, vehicle_id: str, driver_id: str,
                  contract: dict[str, Any], segments: list[dict[str, Any]],
                  stops: list[dict[str, Any]] | None = None,
                  planned_start: str | None = None,
                  planned_end: str | None = None) -> CharterActionResult:
        payload = {"actor_id": actor_id, "filing_id": filing_id, "tour_code": tour_code,
                   "passenger_count": passenger_count, "vehicle_id": vehicle_id,
                   "driver_id": driver_id, "contract": contract, "segments": segments,
                   "stops": stops or [], "planned_start": planned_start,
                   "planned_end": planned_end}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            filing_id = self._identifier(filing_id, "filing_id")
            tour_code = self._text(tour_code, "tour_code", 60)
            if not isinstance(passenger_count, int) or passenger_count <= 0:
                raise ValidationError("passenger_count 必须是正整数")
            vehicle_id = self._identifier(vehicle_id, "vehicle_id")
            driver_id = self._identifier(driver_id, "driver_id")
            contract_clean = self._validate_contract(contract)
            seg_clean = self._normalize_segments(segments)
            stop_clean = self._normalize_stops(stops, seg_clean)
            for segment in seg_clean:
                self._region(conn, segment["region_code"])
            start_dt = parse_dt(planned_start, "planned_start") if planned_start else seg_clean[0]["depart_at"]
            end_dt = parse_dt(planned_end, "planned_end") if planned_end else seg_clean[-1]["arrive_at"]
            if start_dt > seg_clean[0]["depart_at"] or end_dt < seg_clean[-1]["arrive_at"]:
                raise ValidationError("计划起止时间必须覆盖全部区段")
            hard = self._resource_blockers(
                conn, vehicle_id=vehicle_id, driver_id=driver_id,
                passenger_count=passenger_count, segments=seg_clean, planned_end=end_dt,
                exclude_filing_id=filing_id)
            hard = [item for item in hard if item["code"] in HARD_BLOCK_CODES]
            if hard:
                raise ConflictError("申报不满足基本条件："
                                    + "；".join(item["message"] for item in hard))

            if conn.execute("SELECT 1 FROM charter_filings WHERE filing_id=?",
                            (filing_id,)).fetchone():
                raise ConflictError("团次申报编号已经存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO charter_filings(filing_id,org_id,tour_code,passenger_count,"
                        "planned_start,planned_end,vehicle_id,driver_id,contract_json,status,"
                        "revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,"
                        "'permitting',0,?,?,?)",
                        (filing_id, actor.organization_id, tour_code, passenger_count,
                         start_dt.isoformat(), end_dt.isoformat(), vehicle_id, driver_id,
                         canonical_json(contract_clean), actor_id, self._now(), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("同一旅行社下团次代码已经存在") from exc
                self._persist_revision(conn, filing_id=filing_id, revision=0,
                                       segments=seg_clean, stops=stop_clean)
                self._create_permits(conn, filing_id=filing_id, revision=0,
                                     segments=seg_clean, stops=stop_clean,
                                     top={"vehicle_id": vehicle_id, "driver_id": driver_id,
                                          "passenger_count": passenger_count})
                self._audit(conn, actor_id=actor_id, action="charter.trip.filed",
                            resource_type="charter_filing", resource_id=filing_id,
                            detail={"tour_code": tour_code, "segment_count": len(seg_clean),
                                    "regions": sorted({s["region_code"] for s in seg_clean})})
                return "charter_filing", filing_id, self.filing_view(conn, filing_id)

            return self._idem(conn, request_id=request_id, action="charter_file_trip",
                              payload=payload, create=create)

    # ------------------------------------------------- 版本持久化与许可链

    def _persist_revision(self, conn, *, filing_id: str, revision: int,
                          segments: list[dict[str, Any]], stops: list[dict[str, Any]]) -> None:
        for segment in segments:
            conn.execute(
                "INSERT INTO charter_segments(segment_id,filing_id,revision,seq,region_code,"
                "road_from,road_to,depart_at,arrive_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, filing_id, revision, segment["seq"], segment["region_code"],
                 segment["road_from"], segment["road_to"],
                 segment["depart_at"].isoformat(), segment["arrive_at"].isoformat()),
            )
        for stop in stops:
            conn.execute(
                "INSERT INTO charter_stops(stop_id,filing_id,revision,seq,segment_seq,name,"
                "region_code,arrive_at,leave_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, filing_id, revision, stop["seq"], stop["segment_seq"],
                 stop["name"], stop["region_code"], stop["arrive_at"].isoformat(),
                 stop["leave_at"].isoformat()),
            )
        closures = self._active_closures(conn)
        for segment in segments:
            for closure in self._closure_impacts(closures, segment):
                conn.execute(
                    "INSERT INTO charter_segment_closures(filing_id,revision,seq,closure_id)"
                    " VALUES(?,?,?,?)",
                    (filing_id, revision, segment["seq"], closure["closure_id"]),
                )

    def _scope_hash(self, *, segment: dict[str, Any], stops: list[dict[str, Any]],
                    top: dict[str, Any], closure_ids: list[str]) -> str:
        scope = {"seq": segment["seq"], "region_code": segment["region_code"],
                 "road_from": segment["road_from"], "road_to": segment["road_to"],
                 "depart_at": segment["depart_at"].isoformat(),
                 "arrive_at": segment["arrive_at"].isoformat(),
                 "stops": [{"seq": item["seq"], "name": item["name"],
                            "arrive_at": item["arrive_at"].isoformat(),
                            "leave_at": item["leave_at"].isoformat()} for item in stops],
                 "vehicle_id": top["vehicle_id"], "driver_id": top["driver_id"],
                 "passenger_count": top["passenger_count"],
                 "closures": sorted(closure_ids)}
        return digest(scope)

    def _create_permits(self, conn, *, filing_id: str, revision: int,
                        segments: list[dict[str, Any]], stops: list[dict[str, Any]],
                        top: dict[str, Any]) -> None:
        closure_rows = conn.execute(
            "SELECT seq,closure_id FROM charter_segment_closures WHERE filing_id=? AND revision=?",
            (filing_id, revision)).fetchall()
        closure_by_seq: dict[int, list[str]] = {}
        for row in closure_rows:
            closure_by_seq.setdefault(row["seq"], []).append(row["closure_id"])
        for segment in segments:
            segment_stops = [item for item in stops if item["segment_seq"] == segment["seq"]]
            scope_hash = self._scope_hash(segment=segment, stops=segment_stops, top=top,
                                          closure_ids=closure_by_seq.get(segment["seq"], []))
            conn.execute(
                "INSERT INTO charter_permits(permit_id,filing_id,revision,seq,segment_id,"
                "region_code,scope_hash,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,"
                "'pending',?,?)",
                (uuid.uuid4().hex, filing_id, revision, segment["seq"],
                 self._segment_id(conn, filing_id, revision, segment["seq"]),
                 segment["region_code"], scope_hash, "system", self._now()),
            )

    @staticmethod
    def _segment_id(conn, filing_id: str, revision: int, seq: int) -> str:
        row = conn.execute(
            "SELECT segment_id FROM charter_segments WHERE filing_id=? AND revision=? AND seq=?",
            (filing_id, revision, seq)).fetchone()
        return row["segment_id"]

    # ----------------------------------------------------------- 分区审批

    def decide_permit(self, *, request_id: str, actor_id: str, permit_id: str,
                      decision: str, department: str, conditions: list[str] | None = None,
                      note: str | None = None) -> CharterActionResult:
        payload = {"actor_id": actor_id, "permit_id": permit_id, "decision": decision,
                   "department": department, "conditions": conditions or [], "note": note}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *REVIEW_ROLES)
            permit = conn.execute("SELECT * FROM charter_permits WHERE permit_id=?",
                                  (permit_id,)).fetchone()
            if permit is None:
                raise NotFoundError("许可不存在")
            filing = conn.execute("SELECT * FROM charter_filings WHERE filing_id=?",
                                  (permit["filing_id"],)).fetchone()
            if filing["status"] in FILING_TERMINAL:
                raise ConflictError("团次已结束或取消，不能再审批")
            if filing["revision"] != permit["revision"]:
                raise ConflictError("该许可已被修订替代，请审批最新版本")
            if permit["status"] == "rejected":
                raise ConflictError("该区段许可已被拒绝，旅行社修订后才能重新审批")
            self._region_department(conn, actor, permit["region_code"], department)
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 必须是 approve 或 reject")
            if decision == "approve":
                view = self.filing_view(conn, permit["filing_id"])
                gate = {item["code"] for item in view["blocking"]
                        if item["code"] in APPROVAL_GATE_CODES
                        and ("segment_seq" not in item or item["segment_seq"] == permit["seq"])}
                if gate:
                    raise ConflictError(f"区段 {permit['seq']} 仍存在未解除的合规问题，"
                                        f"不能批准：" + "、".join(sorted(gate)))
            conditions = conditions or []
            if not isinstance(conditions, list) or not all(isinstance(c, str) for c in conditions):
                raise ValidationError("conditions 必须是字符串数组")

            def create():
                duplicate = conn.execute(
                    "SELECT * FROM charter_permit_decisions WHERE permit_id=? AND organization_id=?",
                    (permit_id, actor.organization_id)).fetchone()
                if duplicate:
                    if duplicate["decision"] != decision:
                        raise ConflictError("本部门已作出不同决定，不能更改")
                    return self._replay_permit(conn, permit, filing)
                conn.execute(
                    "INSERT INTO charter_permit_decisions(decision_id,permit_id,organization_id,"
                    "department,decided_by,decision,conditions_json,note_text,decided_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, permit_id, actor.organization_id, department, actor_id,
                     decision, canonical_json(conditions), note, self._now()),
                )
                if decision == "reject":
                    conn.execute("UPDATE charter_permits SET status='rejected',decided_at=?,"
                                 "note=? WHERE permit_id=?", (self._now(), note, permit_id))
                else:
                    decisions = conn.execute(
                        "SELECT * FROM charter_permit_decisions WHERE permit_id=?",
                        (permit_id,)).fetchall()
                    region = self._region(conn, permit["region_code"])
                    required = {"transport"}
                    if region["tourism_org_id"]:
                        required.add("tourism")
                    approved = {row["department"] for row in decisions
                                if row["decision"] == "approve"}
                    if required <= approved:
                        conn.execute("UPDATE charter_permits SET status='approved',decided_at=? "
                                     "WHERE permit_id=?", (self._now(), permit_id))
                self._audit(conn, actor_id=actor_id, action="charter.permit.decided",
                            resource_type="charter_permit", resource_id=permit_id,
                            detail={"filing_id": filing["filing_id"], "seq": permit["seq"],
                                    "region_code": permit["region_code"],
                                    "department": department, "decision": decision,
                                    "conditions": conditions, "note": note})
                return "charter_permit", permit_id, self.filing_view(conn, filing["filing_id"])

            return self._idem(conn, request_id=request_id, action="charter_decide_permit",
                              payload=payload, create=create)

    def _replay_permit(self, conn, permit, filing) -> tuple[str, str, dict[str, Any]]:
        return "charter_permit", permit["permit_id"], self.filing_view(conn, filing["filing_id"])

    def pending_permits(self, actor_id: str) -> dict[str, Any]:
        """返回审批人本辖区待处理的许可（只看最新修订版本）。"""

        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *REVIEW_ROLES, "auditor")
            if actor.role == "admin":
                rows = conn.execute(
                    "SELECT p.* FROM charter_permits p JOIN charter_filings f ON f.filing_id=p.filing_id "
                    "WHERE f.revision=p.revision AND p.status='pending' "
                    "AND f.status NOT IN ('cancelled','completed') ORDER BY p.filing_id,p.seq"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT p.* FROM charter_permits p "
                    "JOIN charter_regions r ON r.region_code=p.region_code "
                    "JOIN charter_filings f ON f.filing_id=p.filing_id "
                    "WHERE f.revision=p.revision AND p.status='pending' "
                    "AND f.status NOT IN ('cancelled','completed') "
                    "AND (? IN (r.transport_org_id,r.tourism_org_id)) "
                    "AND NOT EXISTS (SELECT 1 FROM charter_permit_decisions d "
                    "WHERE d.permit_id=p.permit_id AND d.organization_id=?) "
                    "ORDER BY p.filing_id,p.seq",
                    (actor.organization_id, actor.organization_id)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item.pop("scope_hash", None)
                items.append(item)
            return {"items": items, "count": len(items)}

    # ----------------------------------------------------------- 行程单签发

    def issue_certificate(self, *, request_id: str, actor_id: str,
                          filing_id: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "filing_id": filing_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES, "reviewer")
            filing = self._require_filing(conn, filing_id)
            if actor.role not in ("admin", "reviewer") and actor.organization_id != filing["org_id"]:
                raise PermissionDenied("只能为本旅行社团次签发行程单")
            if filing["status"] in FILING_TERMINAL:
                raise ConflictError("团次已结束或取消")
            view = self.filing_view(conn, filing_id)
            if any(item["status"] != "approved" for item in view["permits"]):
                raise ConflictError("仍有区段许可未齐备，不能签发行程单")
            if view["blocking"]:
                raise ConflictError("存在未解除的阻塞："
                                    + "；".join(item["message"] for item in view["blocking"]))

            def create():
                previous = conn.execute(
                    "SELECT * FROM charter_certificates WHERE filing_id=? ORDER BY version DESC LIMIT 1",
                    (filing_id,)).fetchone()
                version = (previous["version"] + 1) if previous else 1
                chain_head = previous["chain_head"] if previous else None
                certificate_no = f"TC-{filing_id}-V{version}"
                permit_ids = [item["permit_id"] for item in view["permits"]]
                snapshot = self._certificate_snapshot(conn, filing, view)
                conn.execute(
                    "INSERT INTO charter_certificates(certificate_no,filing_id,version,status,"
                    "parent_certificate_no,chain_head,permit_ids_json,snapshot_json,issued_by,"
                    "issued_at) VALUES(?,?,?,'active',?,?,?,?,?,?)",
                    (certificate_no, filing_id, version,
                     previous["certificate_no"] if previous else None,
                     chain_head or certificate_no, canonical_json(permit_ids),
                     canonical_json(snapshot), actor_id, self._now()),
                )
                if previous and previous["status"] == "active":
                    conn.execute("UPDATE charter_certificates SET status='superseded' "
                                 "WHERE certificate_no=?", (previous["certificate_no"],))
                new_status = "executing" if filing["started_at"] else "executable"
                conn.execute("UPDATE charter_filings SET status=?,updated_at=? WHERE filing_id=?",
                             (new_status, self._now(), filing_id))
                for amendment_row in conn.execute(
                        "SELECT amendment_id FROM charter_amendments WHERE filing_id=? AND status='reviewing'",
                        (filing_id,)).fetchall():
                    conn.execute("UPDATE charter_amendments SET status='completed',certificate_no=? "
                                 "WHERE amendment_id=?", (certificate_no, amendment_row["amendment_id"]))
                self._audit(conn, actor_id=actor_id, action="charter.certificate.issued",
                            resource_type="charter_certificate", resource_id=certificate_no,
                            detail={"filing_id": filing_id, "version": version,
                                    "parent": previous["certificate_no"] if previous else None,
                                    "permit_count": len(permit_ids),
                                    "chain_head": chain_head or certificate_no})
                data = {"certificate_no": certificate_no, "version": version,
                        "chain_head": chain_head or certificate_no, "status": new_status,
                        "permits": view["permits"]}
                return "charter_certificate", certificate_no, data

            return self._idem(conn, request_id=request_id, action="charter_issue_certificate",
                              payload=payload, create=create)

    def _certificate_snapshot(self, conn, filing, view) -> dict[str, Any]:
        vehicle = conn.execute("SELECT plate,seat_count FROM charter_vehicles WHERE vehicle_id=?",
                               (filing["vehicle_id"],)).fetchone()
        driver = conn.execute("SELECT display_name,qualification_no FROM charter_drivers "
                              "WHERE driver_id=?", (filing["driver_id"],)).fetchone()
        return {"tour_code": filing["tour_code"], "org_id": filing["org_id"],
                "passenger_count": filing["passenger_count"],
                "planned_start": filing["planned_start"], "planned_end": filing["planned_end"],
                "vehicle": {"vehicle_id": filing["vehicle_id"],
                            "plate": vehicle["plate"] if vehicle else None,
                            "seat_count": vehicle["seat_count"] if vehicle else None},
                "driver": {"driver_id": filing["driver_id"],
                           "display_name": driver["display_name"] if driver else None,
                           "qualification_no": driver["qualification_no"] if driver else None},
                "segments": view["segments"], "stops": view["stops"],
                "regions": [{"seq": item["seq"], "region_code": item["region_code"],
                             "status": item["status"]} for item in view["permits"]]}

    # ----------------------------------------------------------- 履约变更

    def amend_filing(self, *, request_id: str, actor_id: str, filing_id: str, kind: str,
                     reason: str | None = None, changes: dict[str, Any] | None = None) -> CharterActionResult:
        changes = changes or {}
        payload = {"actor_id": actor_id, "filing_id": filing_id, "kind": kind,
                   "reason": reason, "changes": changes}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            filing = self._require_filing(conn, filing_id)
            if actor.role != "admin" and actor.organization_id != filing["org_id"]:
                raise PermissionDenied("只能修订本旅行社的团次")
            if kind not in AMEND_KINDS:
                raise ValidationError(f"kind 必须是 {sorted(AMEND_KINDS)} 之一")
            if filing["status"] in FILING_TERMINAL:
                raise ConflictError("团次已结束或取消，不能再修订")
            reason = self._text(reason or "", "reason", 300) if kind != "cancel" else \
                self._text(reason or "旅客行程取消", "reason", 300)
            old_revision = filing["revision"]
            old_segments = self._load_segments(conn, filing_id, old_revision)
            old_stops = self._load_stops(conn, filing_id, old_revision)
            top = {"vehicle_id": filing["vehicle_id"], "driver_id": filing["driver_id"],
                   "passenger_count": filing["passenger_count"]}

            if kind == "cancel":
                return self._apply_cancel(conn, actor=actor, filing=filing, reason=reason,
                                          request_id=request_id, payload=payload)

            closure = None
            if kind == "road_closure":
                closure_id = self._identifier(changes.get("closure_id", ""), "changes.closure_id")
                closure = conn.execute("SELECT * FROM charter_closures WHERE closure_id=? AND active=1",
                                       (closure_id,)).fetchone()
                if closure is None:
                    raise NotFoundError("封路信息不存在或已失效")
            elif kind == "passenger_change":
                new_count = changes.get("passenger_count")
                if not isinstance(new_count, int) or new_count <= 0:
                    raise ValidationError("changes.passenger_count 必须是正整数")
                top["passenger_count"] = new_count
            elif kind == "vehicle_change":
                top["vehicle_id"] = self._identifier(changes.get("vehicle_id", ""),
                                                      "changes.vehicle_id")
            elif kind == "driver_change":
                top["driver_id"] = self._identifier(changes.get("driver_id", ""),
                                                    "changes.driver_id")

            if kind == "itinerary_change":
                new_segments = self._normalize_segments(changes["segments"])
                new_stops = self._normalize_stops(changes.get("stops", []), new_segments)
                for segment in new_segments:
                    self._region(conn, segment["region_code"])
            else:
                new_segments = old_segments
                new_stops = old_stops

            planned_end = parse_dt(filing["planned_end"], "planned_end")
            planned_start = parse_dt(filing["planned_start"], "planned_start")
            if kind == "itinerary_change":
                planned_start = new_segments[0]["depart_at"]
                planned_end = new_segments[-1]["arrive_at"]
            if kind == "road_closure":
                affected = {segment["seq"] for segment in new_segments
                            if self._closure_impacts([dict(closure)], segment)}
                if not affected:
                    raise ConflictError("该封路不影响本行程的任何区段，无需重审")
            elif kind in ("passenger_change", "vehicle_change", "driver_change"):
                affected = {segment["seq"] for segment in new_segments}
            else:
                affected = self._itinerary_affected(conn, filing_id=filing_id,
                                                    old_revision=old_revision,
                                                    old_segments=old_segments, old_stops=old_stops,
                                                    old_top=top, new_segments=new_segments,
                                                    new_stops=new_stops, new_top=top)
            if not affected:
                raise ConflictError("变更未影响任何区段审批，无需修订")

            # 整体规则重校（硬伤直接拒绝；业务阻塞随视图返回，并在批准/签发时拦截）
            blockers = self._work_rule_blockers(new_segments)
            blockers += self._resource_blockers(
                conn, vehicle_id=top["vehicle_id"], driver_id=top["driver_id"],
                passenger_count=top["passenger_count"], segments=new_segments,
                planned_end=planned_end, exclude_filing_id=filing_id)
            structural = {item["code"] for item in blockers if item["code"] in HARD_BLOCK_CODES}
            if structural:
                raise ConflictError("变更不满足签发条件："
                                    + "；".join(item["message"] for item in blockers
                                               if item["code"] in HARD_BLOCK_CODES))

            new_revision = old_revision + 1

            def create():
                conn.execute(
                    "UPDATE charter_filings SET passenger_count=?,vehicle_id=?,driver_id=?,"
                    "planned_start=?,planned_end=?,revision=?,status='amending',updated_at=? "
                    "WHERE filing_id=?",
                    (top["passenger_count"], top["vehicle_id"], top["driver_id"],
                     planned_start.isoformat(), planned_end.isoformat(),
                     new_revision, self._now(), filing_id))
                self._persist_revision(conn, filing_id=filing_id, revision=new_revision,
                                       segments=new_segments, stops=new_stops)
                self._create_permits(conn, filing_id=filing_id, revision=new_revision,
                                     segments=new_segments, stops=new_stops, top=top)
                reused = self._carry_approvals(conn, filing=filing, old_revision=old_revision,
                                               new_revision=new_revision, old_segments=old_segments,
                                               old_stops=old_stops, old_top=top,
                                               new_segments=new_segments, new_stops=new_stops,
                                               new_top=top, affected=affected)
                amendment_id = uuid.uuid4().hex
                refund = self._refund_terms(kind=kind, reason=reason, started=bool(filing["started_at"]),
                                            cancelled=False)
                conn.execute(
                    "INSERT INTO charter_amendments(amendment_id,filing_id,revision,kind,reason,"
                    "payload_json,status,affected_seqs_json,certificate_no,refund_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,'reviewing',?,?,?,?,?)",
                    (amendment_id, filing_id, new_revision, kind, reason,
                     canonical_json(changes | ({"closure_id": closure["closure_id"]} if closure else {})),
                     canonical_json(sorted(affected)), None, canonical_json(refund),
                     actor_id, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="charter.amendment.created",
                            resource_type="charter_amendment", resource_id=amendment_id,
                            detail={"filing_id": filing_id, "kind": kind, "reason": reason,
                                    "old_revision": old_revision, "new_revision": new_revision,
                                    "affected_seqs": sorted(affected), "reused_seqs": reused,
                                    "already_started": bool(filing["started_at"])})
                data = {"amendment_id": amendment_id, "revision": new_revision, "kind": kind,
                        "affected_seqs": sorted(affected), "reused_seqs": reused,
                        "already_started": bool(filing["started_at"]), "refund": refund,
                        **self.filing_view(conn, filing_id)}
                return "charter_amendment", amendment_id, data

            return self._idem(conn, request_id=request_id, action=f"charter_amend_{kind}",
                              payload=payload, create=create)

    def _itinerary_affected(self, conn, *, filing_id, old_revision, old_segments, old_stops,
                            old_top, new_segments, new_stops, new_top) -> set[int]:
        old_permits = {row["seq"]: dict(row) for row in conn.execute(
            "SELECT * FROM charter_permits WHERE filing_id=? AND revision=?",
            (filing_id, old_revision)).fetchall()}
        closures = self._active_closures(conn)
        closure_by_seq: dict[int, list[str]] = {}
        for segment in new_segments:
            closure_by_seq[segment["seq"]] = [item["closure_id"]
                                              for item in self._closure_impacts(closures, segment)]
        affected: set[int] = set()
        if len(old_segments) != len(new_segments):
            return set(range(1, len(new_segments) + 1))
        for old_segment, new_segment in zip(old_segments, new_segments):
            old_stops_for = [item for item in old_stops if item["segment_seq"] == old_segment["seq"]]
            new_stops_for = [item for item in new_stops if item["segment_seq"] == new_segment["seq"]]
            new_hash = self._scope_hash(segment=new_segment, stops=new_stops_for, top=new_top,
                                        closure_ids=closure_by_seq.get(new_segment["seq"], []))
            old_closure_ids = [row["closure_id"] for row in conn.execute(
                "SELECT closure_id FROM charter_segment_closures WHERE filing_id=? AND revision=? AND seq=?",
                (filing_id, old_revision, old_segment["seq"])).fetchall()]
            old_hash = self._scope_hash(segment=old_segment, stops=old_stops_for, top=old_top,
                                        closure_ids=old_closure_ids)
            if new_hash != old_hash:
                affected.add(new_segment["seq"])
            elif old_permits.get(old_segment["seq"], {}).get("status") == "rejected":
                # 原拒绝虽内容未变，也借修订重新提交
                affected.add(new_segment["seq"])
        return affected

    def _carry_approvals(self, conn, *, filing, old_revision, new_revision, old_segments,
                         old_stops, old_top, new_segments, new_stops, new_top,
                         affected: set[int]) -> list[int]:
        """把未受影响区段的原批准复制为新版本的继承批准。"""

        reused: list[int] = []
        old_permits = {row["seq"]: row for row in conn.execute(
            "SELECT * FROM charter_permits WHERE filing_id=? AND revision=? ORDER BY seq",
            (filing["filing_id"], old_revision)).fetchall()}
        for segment in new_segments:
            if segment["seq"] in affected:
                continue
            previous = old_permits.get(segment["seq"])
            if previous is None or previous["status"] != "approved":
                affected.add(segment["seq"])
                continue
            new_permit = conn.execute(
                "SELECT * FROM charter_permits WHERE filing_id=? AND revision=? AND seq=?",
                (filing["filing_id"], new_revision, segment["seq"])).fetchone()
            for old_decision in conn.execute(
                    "SELECT * FROM charter_permit_decisions WHERE permit_id=? AND decision='approve'",
                    (previous["permit_id"],)).fetchall():
                conn.execute(
                    "INSERT INTO charter_permit_decisions(decision_id,permit_id,organization_id,"
                    "department,decided_by,decision,conditions_json,note_text,inherited_from,"
                    "decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, new_permit["permit_id"], old_decision["organization_id"],
                     old_decision["department"], old_decision["decided_by"], "approve",
                     old_decision["conditions_json"],
                     (old_decision["note_text"] or "") + "（修订沿用原批准）"
                     if old_decision["note_text"] else f"沿用 {old_revision} 版批准",
                     old_decision["decision_id"], old_decision["decided_at"]),
                )
            conn.execute(
                "UPDATE charter_permits SET status='approved',prior_permit_id=?,decided_at=? "
                "WHERE permit_id=?",
                (previous["permit_id"], previous["decided_at"], new_permit["permit_id"]))
            reused.append(segment["seq"])
        return reused

    def _apply_cancel(self, conn, *, actor, filing, reason, request_id, payload):
        amendment_id = uuid.uuid4().hex
        refund = self._refund_terms(kind="cancel", reason=reason,
                                    started=bool(filing["started_at"]), cancelled=True)
        old_revision = filing["revision"]

        def create():
            conn.execute("UPDATE charter_filings SET status='cancelled',updated_at=? WHERE filing_id=?",
                         (self._now(), filing["filing_id"]))
            active = conn.execute("SELECT certificate_no FROM charter_certificates "
                                  "WHERE filing_id=? AND status='active'", (filing["filing_id"],)).fetchall()
            for row in active:
                conn.execute("UPDATE charter_certificates SET status='voided',void_reason=? "
                             "WHERE certificate_no=?", (reason, row["certificate_no"]))
            conn.execute(
                "INSERT INTO charter_amendments(amendment_id,filing_id,revision,kind,reason,"
                "payload_json,status,affected_seqs_json,certificate_no,refund_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,'applied',?,?,?,?,?)",
                (amendment_id, filing["filing_id"], old_revision, "cancel", reason, "{}",
                 canonical_json([item["seq"] for item in
                                 self._load_segments(conn, filing["filing_id"], old_revision)]),
                 None, canonical_json(refund), actor.actor_id, self._now()),
            )
            self._audit(conn, actor_id=actor.actor_id, action="charter.amendment.created",
                        resource_type="charter_amendment", resource_id=amendment_id,
                        detail={"filing_id": filing["filing_id"], "kind": "cancel", "reason": reason,
                                "voided_certificates": [row["certificate_no"] for row in active],
                                "already_started": bool(filing["started_at"])})
            data = {"amendment_id": amendment_id, "kind": "cancel", "refund": refund,
                    "voided_certificates": [row["certificate_no"] for row in active],
                    **self.filing_view(conn, filing["filing_id"])}
            return "charter_amendment", amendment_id, data

        return self._idem(conn, request_id=request_id, action="charter_amend_cancel",
                          payload=payload, create=create)

    @staticmethod
    def _refund_terms(*, kind: str, reason: str, started: bool, cancelled: bool) -> dict[str, Any]:
        """确定性的退款责任口径，供旅行社事先知晓。"""

        if kind == "road_closure":
            return {"refundable": True, "responsible_party": "force_majeure", "rate": 1.0,
                    "basis": "临时封路属不可抗力，未履行区段允许免费改期或全额退款，旅行社不承担违约责"
                             "任"}
        if kind == "cancel":
            if started:
                return {"refundable": True, "responsible_party": "travel_agency", "rate": 0.0,
                        "basis": "行程已开始后取消，未发生费用由旅行社与旅客按合同结算，"
                                 "已履行部分不退"}
            return {"refundable": True, "responsible_party": "travel_agency", "rate": 1.0,
                    "basis": "行程开始前取消，未实际发生的承运与服务费用应予全额退还"}
        return {"refundable": False, "responsible_party": "none", "rate": 0.0,
                "basis": f"{kind} 不直接产生退款，按多退少补或改期处理；原批准结果在影响范围外沿用"}

    # ----------------------------------------------------------- 行程执行

    def start_trip(self, *, request_id: str, actor_id: str, filing_id: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "filing_id": filing_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            filing = self._require_filing(conn, filing_id)
            if actor.role != "admin" and actor.organization_id != filing["org_id"]:
                raise PermissionDenied("只能启动本旅行社的团次")
            if filing["status"] != "executable":
                raise ConflictError("只有持有效行程单的团次才能发车")
            if filing["started_at"]:
                raise ConflictError("团次已经发车")

            def create():
                conn.execute("UPDATE charter_filings SET status='executing',started_at=?,"
                             "updated_at=? WHERE filing_id=?", (self._now(), self._now(), filing_id))
                self._audit(conn, actor_id=actor_id, action="charter.trip.started",
                            resource_type="charter_filing", resource_id=filing_id,
                            detail={"tour_code": filing["tour_code"]})
                return "charter_filing", filing_id, self.filing_view(conn, filing_id)

            return self._idem(conn, request_id=request_id, action="charter_start_trip",
                              payload=payload, create=create)

    def complete_trip(self, *, request_id: str, actor_id: str, filing_id: str) -> CharterActionResult:
        payload = {"actor_id": actor_id, "filing_id": filing_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *CARRIER_ROLES)
            filing = self._require_filing(conn, filing_id)
            if actor.role != "admin" and actor.organization_id != filing["org_id"]:
                raise PermissionDenied("只能完成本旅行社的团次")
            if filing["status"] != "executing":
                raise ConflictError("团次不在执行中")

            def create():
                conn.execute("UPDATE charter_filings SET status='completed',completed_at=?,"
                             "updated_at=? WHERE filing_id=?", (self._now(), self._now(), filing_id))
                self._audit(conn, actor_id=actor_id, action="charter.trip.completed",
                            resource_type="charter_filing", resource_id=filing_id,
                            detail={"tour_code": filing["tour_code"]})
                return "charter_filing", filing_id, self.filing_view(conn, filing_id)

            return self._idem(conn, request_id=request_id, action="charter_complete_trip",
                              payload=payload, create=create)

    # ----------------------------------------------------------- 查询视图

    def _require_filing(self, conn, filing_id: str):
        row = conn.execute("SELECT * FROM charter_filings WHERE filing_id=?", (filing_id,)).fetchone()
        if row is None:
            raise NotFoundError("团次申报不存在")
        return row

    @staticmethod
    def _load_segments(conn, filing_id: str, revision: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM charter_segments WHERE filing_id=? AND revision=? ORDER BY seq",
                            (filing_id, revision)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("segment_id", None)
            item["depart_at"] = parse_dt(row["depart_at"], "depart_at")
            item["arrive_at"] = parse_dt(row["arrive_at"], "arrive_at")
            result.append(item)
        return result

    @staticmethod
    def _load_stops(conn, filing_id: str, revision: int) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM charter_stops WHERE filing_id=? AND revision=? ORDER BY seq",
                            (filing_id, revision)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("stop_id", None)
            item["arrive_at"] = parse_dt(row["arrive_at"], "arrive_at")
            item["leave_at"] = parse_dt(row["leave_at"], "leave_at")
            result.append(item)
        return result

    def _permit_view(self, conn, filing_id: str, revision: int) -> list[dict[str, Any]]:
        permits = []
        for row in conn.execute("SELECT * FROM charter_permits WHERE filing_id=? AND revision=? ORDER BY seq",
                                (filing_id, revision)).fetchall():
            decisions = []
            for decision_row in conn.execute(
                    "SELECT * FROM charter_permit_decisions WHERE permit_id=? ORDER BY decided_at",
                    (row["permit_id"],)).fetchall():
                decisions.append({"organization_id": decision_row["organization_id"],
                                  "department": decision_row["department"],
                                  "decided_by": decision_row["decided_by"],
                                  "decision": decision_row["decision"],
                                  "conditions": json.loads(decision_row["conditions_json"] or "[]"),
                                  "note": decision_row["note_text"],
                                  "inherited": decision_row["inherited_from"] is not None,
                                  "decided_at": decision_row["decided_at"]})
            closure_ids = [item["closure_id"] for item in conn.execute(
                "SELECT closure_id FROM charter_segment_closures WHERE filing_id=? AND revision=? AND seq=?",
                (filing_id, revision, row["seq"])).fetchall()]
            permits.append({"permit_id": row["permit_id"], "seq": row["seq"],
                            "region_code": row["region_code"], "status": row["status"],
                            "prior_permit_id": row["prior_permit_id"],
                            "amendment_id": row["amendment_id"],
                            "decided_at": row["decided_at"], "decisions": decisions,
                            "closure_ids": closure_ids})
        return permits

    def filing_view(self, conn, filing_id: str) -> dict[str, Any]:
        """组装团次的完整联审视图（阻塞、许可、行程单、修订、退款）。"""

        filing = conn.execute("SELECT * FROM charter_filings WHERE filing_id=?",
                              (filing_id,)).fetchone()
        if filing is None:
            raise NotFoundError("团次申报不存在")
        revision = filing["revision"]
        segments = self._load_segments(conn, filing_id, revision)
        stops = self._load_stops(conn, filing_id, revision)
        permits = self._permit_view(conn, filing_id, revision)
        planned_end = parse_dt(filing["planned_end"], "planned_end")
        # 终态团次（已完成/已取消）冻结事实，不再被后登记的封路、资源变化追溯判为异常。
        terminal = filing["status"] in FILING_TERMINAL
        blocking: list[dict[str, Any]] = []
        impact_map: dict[int, list[str]] = {}
        if not terminal:
            blocking = list(self._work_rule_blockers(segments))
            blocking += self._resource_blockers(
                conn, vehicle_id=filing["vehicle_id"], driver_id=filing["driver_id"],
                passenger_count=filing["passenger_count"], segments=segments,
                planned_end=planned_end, exclude_filing_id=filing_id)
            closure_blockers, impact_map = self._closure_blockers(conn, segments, revision, filing_id)
            blocking += closure_blockers
        for permit in permits:
            if permit["status"] == "rejected":
                blocking.append({"code": "permit_rejected", "segment_seq": permit["seq"],
                                 "region_code": permit["region_code"],
                                 "message": f"区段 {permit['seq']}（{permit['region_code']}）"
                                            f"许可被拒，需按修订流程调整后重审"})
        certificate = conn.execute(
            "SELECT certificate_no,version,status,chain_head,parent_certificate_no,issued_at "
            "FROM charter_certificates WHERE filing_id=? ORDER BY version DESC LIMIT 1",
            (filing_id,)).fetchall()
        certificate = dict(certificate[0]) if certificate else None
        amendments = []
        for row in conn.execute("SELECT * FROM charter_amendments WHERE filing_id=? ORDER BY revision",
                                (filing_id,)).fetchall():
            amendments.append({"amendment_id": row["amendment_id"], "revision": row["revision"],
                               "kind": row["kind"], "reason": row["reason"],
                               "status": row["status"],
                               "affected_seqs": json.loads(row["affected_seqs_json"]),
                               "certificate_no": row["certificate_no"],
                               "refund": json.loads(row["refund_json"]) if row["refund_json"] else None,
                               "created_at": row["created_at"]})
        pending = [item["seq"] for item in permits if item["status"] == "pending"]
        reusable = [item["seq"] for item in permits
                    if item["status"] == "approved" and any(d["inherited"] for d in item["decisions"])]
        return {
            "filing_id": filing_id, "tour_code": filing["tour_code"], "org_id": filing["org_id"],
            "status": filing["status"], "revision": revision,
            "passenger_count": filing["passenger_count"],
            "vehicle_id": filing["vehicle_id"], "driver_id": filing["driver_id"],
            "planned_start": filing["planned_start"], "planned_end": filing["planned_end"],
            "started": filing["started_at"] is not None,
            "started_at": filing["started_at"], "completed_at": filing["completed_at"],
            "segments": [{k: (v.isoformat() if isinstance(v, datetime) else v)
                          for k, v in item.items()} for item in segments],
            "stops": [{k: (v.isoformat() if isinstance(v, datetime) else v)
                       for k, v in item.items()} for item in stops],
            "permits": permits,
            "closure_impacts": {str(key): value for key, value in impact_map.items()},
            "blocking": blocking,
            "pending_seqs": pending,
            "reusable_seqs": reusable,
            "certificate": certificate,
            "amendments": amendments,
            "issuable": filing["status"] not in FILING_TERMINAL
                        and all(item["status"] == "approved" for item in permits)
                        and not blocking,
        }

    def get_filing(self, actor_id: str, filing_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            filing = self._require_filing(conn, filing_id)
            if actor.role in ("admin", "auditor") or actor.organization_id == filing["org_id"]:
                return self.filing_view(conn, filing_id)
            own_regions = {row["region_code"] for row in conn.execute(
                "SELECT region_code FROM charter_regions WHERE transport_org_id=? OR tourism_org_id=?",
                (actor.organization_id, actor.organization_id)).fetchall()}
            filing_regions = {row["region_code"] for row in conn.execute(
                "SELECT DISTINCT region_code FROM charter_segments WHERE filing_id=? AND revision=?",
                (filing_id, filing["revision"])).fetchall()}
            if not (own_regions & filing_regions):
                raise PermissionDenied("只能查看本旅行社团次或本辖区区段")
            return self.filing_view(conn, filing_id)

    def agency_view(self, actor_id: str, filing_id: str) -> dict[str, Any]:
        """旅行社视角：阻塞原因、退款责任与可沿用审批。"""

        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            filing = self._require_filing(conn, filing_id)
            if actor.role != "admin" and actor.organization_id != filing["org_id"]:
                raise PermissionDenied("只能查看本旅行社的团次")
            view = self.filing_view(conn, filing_id)
            view["refund_summary"] = self._agency_refund_summary(view)
            return view

    @staticmethod
    def _agency_refund_summary(view: dict[str, Any]) -> dict[str, Any] | None:
        if view["status"] == "cancelled" or view["amendments"]:
            latest = view["amendments"][-1]
            return {"trigger": latest["kind"], **(latest["refund"] or {})}
        if view["blocking"]:
            force_majeure = any(item["code"] in {"new_closure_not_reviewed"} for item in view["blocking"])
            return {"trigger": "blocked",
                    "responsible_party": "force_majeure" if force_majeure else "travel_agency",
                    "refundable": False,
                    "basis": "当前阻塞源于封路等不可抗力的，可依封路修订申请免责改期；"
                             "源于证照、工时或超员的，由旅行社自行承担停运损失"}
        return None

    def enforcement_check(self, *, actor_id: str, certificate_no: str | None = None,
                          plate: str | None = None, at: str | None = None) -> dict[str, Any]:
        """执法核验：当前有效许可、责任地区与异常处置依据。"""

        with self.database.transaction() as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *ENFORCEMENT_ROLES)
            moment = parse_dt(at, "at") if at else self.clock.now()
            query = conn.execute(
                "SELECT c.*,f.tour_code,f.status AS filing_status,f.started_at,f.planned_start,"
                "f.planned_end,f.filing_id FROM charter_certificates c JOIN charter_filings f "
                "ON f.filing_id=c.filing_id WHERE 1=1"
                + (" AND c.certificate_no=?" if certificate_no else "")
                + (" AND json_extract(c.snapshot_json,'$.vehicle.plate')=?" if plate else "")
                + " ORDER BY c.version DESC",
                tuple(value for value in (certificate_no, plate) if value)).fetchall()
            if not query:
                return {"valid": False, "reason": "certificate_not_found",
                        "checked_at": moment.isoformat()}
            row = query[0]
            snapshot = json.loads(row["snapshot_json"])
            filing_id = row["filing_id"]
            view = self.filing_view(conn, filing_id)
            reasons: list[str] = []
            if row["status"] == "voided":
                reasons.append("certificate_voided")
            elif row["status"] == "superseded":
                reasons.append("certificate_superseded")
            if view["status"] == "cancelled":
                reasons.append("filing_cancelled")
            # amending（修订重审中）不否决整体有效性：旧证仍 active、未受影响区段沿用，
            # 待审修订与影响范围通过 exceptions 展示给执法人员。
            planned_start = parse_dt(row["planned_start"], "planned_start")
            planned_end = parse_dt(row["planned_end"], "planned_end")
            if moment < planned_start:
                reasons.append("not_yet_effective")
            elif moment > planned_end:
                reasons.append("trip_ended")
            phase = "ended" if moment > planned_end else ("started" if row["started_at"] else "scheduled")
            valid = row["status"] == "active" and view["status"] != "cancelled" and not reasons
            exceptions = []
            for amendment in view["amendments"]:
                exceptions.append({"type": "amendment", "kind": amendment["kind"],
                                   "reason": amendment["reason"], "status": amendment["status"],
                                   "affected_seqs": amendment["affected_seqs"],
                                   "basis": (amendment["refund"] or {}).get("basis")})
            for blocker in view["blocking"]:
                exceptions.append({"type": "blocking", **blocker})
            for permit in view["permits"]:
                if permit["status"] != "approved":
                    exceptions.append({"type": "permit_pending_or_rejected",
                                       "seq": permit["seq"], "region_code": permit["region_code"],
                                       "status": permit["status"]})
            return {
                "valid": valid, "reasons": reasons, "checked_at": moment.isoformat(), "phase": phase,
                "certificate_no": row["certificate_no"], "version": row["version"],
                "chain_head": row["chain_head"], "certificate_status": row["status"],
                "tour_code": row["tour_code"], "filing_id": filing_id,
                "filing_status": view["status"], "revision": view["revision"],
                "vehicle": snapshot["vehicle"], "driver": snapshot["driver"],
                "passenger_count": snapshot["passenger_count"],
                "planned_start": snapshot["planned_start"], "planned_end": snapshot["planned_end"],
                "responsible_regions": [{"seq": item["seq"], "region_code": item["region_code"],
                                         "status": item["status"],
                                         "decisions": item["decisions"]}
                                        for item in view["permits"]],
                "exceptions": exceptions,
            }

