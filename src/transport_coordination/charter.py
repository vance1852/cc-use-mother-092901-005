"""跨区域旅游包车联审与履约服务。

在基础服务（组织、操作者、幂等回执、哈希审计链）之上实现：

- 旅行社提交团次、车辆、驾驶员、线路区段、停靠计划与合同承诺；
- 各辖区审批人只处理本辖区区段，区段批准串联成许可链，齐备后签发可执行行程；
- 规则引擎识别证照有效期、跨日工时、车辆/驾驶员重复占用与互斥路线；
- 封路、人数变化、取消、替班按影响范围生成新版本，原批准事实保留、可沿用的沿用；
- 行程开始后普通改动不整体回退；执法接口按密钥核验当前有效许可与处置依据。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import (
    BLOCKING,
    Finding,
    fmt,
    overlaps,
    parse_dt,
    pax_within_tolerance,
    qualification_findings,
    worktime_findings,
)
from .service import IDENTIFIER
from .storage import Database

REVIEWER_ROLE = "reviewer"
AGENT_ROLE = "operator"

HEAD_REVIEW = "in_review"
HEAD_EXECUTABLE = "executable"
HEAD_IN_PROGRESS = "in_progress"
HEAD_COMPLETED = "completed"
HEAD_CANCELLED = "cancelled"

# 事件类型 -> 影响范围
IMPACT_ALL = "all"
IMPACT_SEGMENTS = "segments"
IMPACT_NONE = "none"
EVENT_SCOPE = {
    "road_closure": IMPACT_SEGMENTS,
    "pax_change": IMPACT_ALL,
    "vehicle_change": IMPACT_ALL,
    "driver_change": IMPACT_SEGMENTS,
    "stop_adjustment": IMPACT_SEGMENTS,
    "cancel": IMPACT_ALL,
}
# 行程开始后仅影响局部、不得整体回退的事件
LOCAL_EVENTS = frozenset({"road_closure", "driver_change", "stop_adjustment"})

VEHICLE_REQUIRED_LICENSES = ("vehicle_license", "carrier_insurance")
DRIVER_REQUIRED_LICENSES = ("driver_license", "driver_qualification")


class CharterService:
    """实现联审提交、分区审批、版本修订与执法核验。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require_roles(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    # ----------------------------------------------------------- 辖区与密钥

    def assign_reviewer_region(self, *, request_id: str, actor_id: str,
                               reviewer_actor_id: str, region_code: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "reviewer_actor_id": reviewer_actor_id,
                   "region_code": region_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin")
            reviewer = self._actor(connection, reviewer_actor_id)
            if reviewer["role"] != REVIEWER_ROLE:
                raise ValidationError("只能为审批人分配辖区")
            region_code = self._identifier(region_code, "region_code")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT OR IGNORE INTO reviewer_regions(actor_id,region_code,assigned_at) "
                    "VALUES(?,?,?)",
                    (reviewer_actor_id, region_code, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="reviewer_region.assigned",
                            resource_type="actor", resource_id=reviewer_actor_id,
                            detail={"region_code": region_code})
                return "reviewer_region", f"{reviewer_actor_id}:{region_code}", \
                    {"reviewer_actor_id": reviewer_actor_id, "region_code": region_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_reviewer_region", payload=payload, create=create)

    def _reviewer_regions(self, connection, actor_id: str) -> set[str]:
        rows = connection.execute(
            "SELECT region_code FROM reviewer_regions WHERE actor_id=?", (actor_id,)
        ).fetchall()
        return {row["region_code"] for row in rows}

    def register_region_license(self, *, request_id: str, actor_id: str, license_id: str,
                                region_code: str, valid_from: str, valid_to: str,
                                segment_id: str | None = None,
                                route_code: str | None = None,
                                status: str = "active") -> dict[str, Any]:
        """登记某辖区可用的路线许可（区段级或路线级）。"""

        payload = {"actor_id": actor_id, "license_id": license_id, "region_code": region_code,
                   "valid_from": valid_from, "valid_to": valid_to, "segment_id": segment_id,
                   "route_code": route_code, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", REVIEWER_ROLE)
            license_id = self._identifier(license_id, "license_id")
            region_code = self._identifier(region_code, "region_code")
            start = parse_dt(valid_from, "valid_from")
            end = parse_dt(valid_to, "valid_to")
            if start >= end:
                raise ValidationError("许可有效期起止不合法")
            if status not in ("active", "suspended"):
                raise ValidationError("status 只能是 active 或 suspended")
            if actor["role"] == REVIEWER_ROLE and region_code not in self._reviewer_regions(connection, actor_id):
                raise PermissionDenied("不能登记其他辖区的路线许可")
            if not segment_id and not route_code:
                raise ValidationError("segment_id 与 route_code 至少提供一项")
            if segment_id:
                self._identifier(segment_id, "segment_id")
            if route_code:
                self._identifier(route_code, "route_code")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO region_licenses(license_id,region_code,segment_id,route_code,"
                        "status,valid_from,valid_to,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (license_id, region_code, segment_id, route_code, status,
                         valid_from, valid_to, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该辖区与区段/路线已登记许可，请使用新编号") from exc
                self._audit(connection, actor_id=actor_id, action="region_license.registered",
                            resource_type="region_license", resource_id=license_id,
                            detail={"region_code": region_code, "segment_id": segment_id,
                                    "route_code": route_code, "status": status,
                                    "valid_from": valid_from, "valid_to": valid_to})
                return "region_license", license_id, {"license_id": license_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_region_license", payload=payload, create=create)

    def update_region_license_status(self, *, request_id: str, actor_id: str,
                                     license_id: str, status: str) -> dict[str, Any]:
        """临时封路等情形下暂停/恢复一项辖区许可，已签发行程会在重算时暴露异常。"""

        payload = {"actor_id": actor_id, "license_id": license_id, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", REVIEWER_ROLE)
            if status not in ("active", "suspended"):
                raise ValidationError("status 只能是 active 或 suspended")
            row = connection.execute(
                "SELECT * FROM region_licenses WHERE license_id=?", (license_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("路线许可不存在")
            if actor["role"] == REVIEWER_ROLE and row["region_code"] not in \
                    self._reviewer_regions(connection, actor_id):
                raise PermissionDenied("不能处理其他辖区的许可")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE region_licenses SET status=? WHERE license_id=?",
                    (status, license_id),
                )
                self._audit(connection, actor_id=actor_id,
                            action=f"region_license.{status}",
                            resource_type="region_license", resource_id=license_id,
                            detail={"region_code": row["region_code"], "status": status})
                return "region_license", license_id, {"license_id": license_id, "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="update_region_license_status",
                                    payload=payload, create=create)

    def mint_inspection_key(self, *, request_id: str, actor_id: str,
                            key: str, region_code: str | None = None) -> dict[str, Any]:
        """为执法人员签发核验密钥；限定辖区后只能看到本辖区相关结论。"""

        payload = {"actor_id": actor_id, "key": key, "region_code": region_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin")
            key = self._identifier(key, "key")
            region_code = self._identifier(region_code, "region_code") if region_code else None

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO inspection_keys(api_key,region_code,active,created_by,created_at) "
                        "VALUES(?,?,1,?,?)",
                        (key, region_code, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("核验密钥已存在") from exc
                self._audit(connection, actor_id=actor_id, action="inspection_key.minted",
                            resource_type="inspection_key", resource_id=key,
                            detail={"region_code": region_code})
                return "inspection_key", key, {"api_key": key}

            return self._idempotent(connection, request_id=request_id,
                                    action="mint_inspection_key", payload=payload, create=create)

    # ------------------------------------------------------------- 提交与校验

    def _validate_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract = payload.get("contract")
        if not isinstance(contract, dict):
            raise ValidationError("contract 必须是对象")
        self._text(contract.get("contract_no", ""), "contract.contract_no", 80)
        refund_policy = contract.get("refund_policy")
        if not isinstance(refund_policy, dict) or not refund_policy:
            raise ValidationError("contract.refund_policy 必须明确退款责任承诺")

        tour = payload.get("tour")
        if not isinstance(tour, dict):
            raise ValidationError("tour 必须是对象")
        self._text(tour.get("tour_code", ""), "tour.tour_code", 80)
        self._text(tour.get("title", ""), "tour.title", 200)
        passenger_count = tour.get("passenger_count")
        if not isinstance(passenger_count, int) or passenger_count <= 0:
            raise ValidationError("tour.passenger_count 必须是正整数")

        vehicle = payload.get("vehicle")
        if not isinstance(vehicle, dict):
            raise ValidationError("vehicle 必须是对象")
        self._text(vehicle.get("vehicle_id", ""), "vehicle.vehicle_id", 80)
        capacity = vehicle.get("seat_capacity")
        if not isinstance(capacity, int) or capacity < passenger_count:
            raise ValidationError("车辆座位数必须不少于旅客人数")
        if not isinstance(vehicle.get("qualifications"), list) or not vehicle["qualifications"]:
            raise ValidationError("vehicle.qualifications 必须是非空列表")

        drivers = payload.get("drivers")
        if not isinstance(drivers, list) or not drivers:
            raise ValidationError("drivers 必须是非空列表")
        driver_ids: set[str] = set()
        for driver in drivers:
            driver_id = self._text(driver.get("driver_id", ""), "driver.driver_id", 80)
            if driver_id in driver_ids:
                raise ValidationError(f"驾驶员 {driver_id} 重复填报")
            driver_ids.add(driver_id)
            if not isinstance(driver.get("qualifications"), list) or not driver["qualifications"]:
                raise ValidationError(f"驾驶员 {driver_id} 缺少资质列表")
            duty_periods = driver.get("duty_periods")
            if not isinstance(duty_periods, list) or not duty_periods:
                raise ValidationError(f"驾驶员 {driver_id} 必须填报跨日值乘时段")
            for period in duty_periods:
                parse_dt(period.get("on", ""), f"{driver_id}.duty.on")
                parse_dt(period.get("off", ""), f"{driver_id}.duty.off")
            for rest in driver.get("rest_periods", []):
                parse_dt(rest.get("start", ""), f"{driver_id}.rest.start")
                parse_dt(rest.get("end", ""), f"{driver_id}.rest.end")

        segments = payload.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValidationError("segments 必须是非空列表")
        segment_ids: set[str] = set()
        previous_end: datetime | None = None
        for index, segment in enumerate(sorted(segments, key=lambda item: item.get("ordinal", 0))):
            segment_id = self._text(segment.get("segment_id", ""), "segment.segment_id", 80)
            if segment_id in segment_ids:
                raise ValidationError(f"区段 {segment_id} 重复填报")
            segment_ids.add(segment_id)
            self._text(segment.get("region_code", ""), f"{segment_id}.region_code", 40)
            self._text(segment.get("from_site", ""), f"{segment_id}.from_site", 120)
            self._text(segment.get("to_site", ""), f"{segment_id}.to_site", 120)
            if not isinstance(segment.get("ordinal"), int):
                raise ValidationError(f"{segment_id}.ordinal 必须是整数")
            departure = parse_dt(segment.get("departure_at", ""), f"{segment_id}.departure_at")
            arrive = parse_dt(segment.get("arrive_at", ""), f"{segment_id}.arrive_at")
            if departure >= arrive:
                raise ValidationError(f"区段 {segment_id} 到达时间必须晚于出发时间")
            if previous_end is not None and departure < previous_end:
                raise ValidationError(f"区段 {segment_id} 与前序区段时间重叠")
            previous_end = arrive
            driver_id = segment.get("driver_id")
            if driver_id and driver_id not in driver_ids:
                raise ValidationError(f"区段 {segment_id} 引用了未申报驾驶员 {driver_id}")
            if segment.get("route_code"):
                self._identifier(segment["route_code"], f"{segment_id}.route_code")

        stops = payload.get("stops", [])
        if not isinstance(stops, list):
            raise ValidationError("stops 必须是列表")
        for stop in stops:
            stop_id = self._text(stop.get("stop_id", ""), "stop.stop_id", 80)
            if stop["segment_id"] not in segment_ids:
                raise ValidationError(f"停靠点 {stop_id} 引用了不存在的区段")
            planned_at = parse_dt(stop.get("planned_at", ""), f"{stop_id}.planned_at")
            segment = next(item for item in segments if item["segment_id"] == stop["segment_id"])
            seg_start = parse_dt(segment["departure_at"], "segment.departure_at")
            seg_end = parse_dt(segment["arrive_at"], "segment.arrive_at")
            if not (seg_start <= planned_at <= seg_end):
                raise ValidationError(f"停靠点 {stop_id} 的时间不在所属区段窗口内")

        return {"contract": contract, "tour": tour, "vehicle": vehicle,
                "drivers": drivers, "segments": segments, "stops": stops}

    def _snapshot_window(self, snapshot: dict[str, Any]) -> tuple[datetime, datetime]:
        starts = [parse_dt(s["departure_at"], "segment.departure_at") for s in snapshot["segments"]]
        ends = [parse_dt(s["arrive_at"], "segment.arrive_at") for s in snapshot["segments"]]
        return min(starts), max(ends)

    def _matching_licenses(self, connection, segment: dict[str, Any],
                           window_start: datetime, window_end: datetime) -> list[Any]:
        rows = connection.execute(
            "SELECT * FROM region_licenses WHERE region_code=? "
            "AND (segment_id=? OR (segment_id IS NULL AND route_code=?) "
            "OR (segment_id IS NULL AND route_code IS NULL))",
            (segment["region_code"], segment["segment_id"], segment.get("route_code")),
        ).fetchall()
        return rows

    def _evaluate(self, connection, snapshot: dict[str, Any],
                  exclude_group: str | None = None,
                  agency_org_id: str | None = None) -> list[Finding]:
        """对一份团次快照运行全部规则，返回阻断/警告结论。"""

        findings: list[Finding] = []
        segments = sorted(snapshot["segments"], key=lambda item: item["ordinal"])
        window_start, window_end = self._snapshot_window(snapshot)
        vehicle = snapshot["vehicle"]
        tour = snapshot["tour"]
        if agency_org_id is None and exclude_group:
            group_row = connection.execute(
                "SELECT travel_agency_org_id FROM charter_groups WHERE group_id=?",
                (exclude_group,)).fetchone()
            agency_org_id = group_row["travel_agency_org_id"] if group_row else None

        # 车辆资质：证照必须覆盖全程
        findings.extend(qualification_findings(
            "vehicle", vehicle["vehicle_id"], vehicle.get("qualifications", []),
            window_start, window_end, VEHICLE_REQUIRED_LICENSES))
        if vehicle.get("seat_capacity", 0) < tour["passenger_count"]:
            findings.append(Finding("vehicle.overcapacity", BLOCKING, vehicle["vehicle_id"],
                                    "车辆座位数少于旅客人数"))

        # 驾驶员资质与跨日工时
        for driver in snapshot["drivers"]:
            findings.extend(qualification_findings(
                "driver", driver["driver_id"], driver.get("qualifications", []),
                window_start, window_end, DRIVER_REQUIRED_LICENSES))
            served = [s for s in segments
                      if s.get("driver_id") == driver["driver_id"]
                      or (not s.get("driver_id") and len(snapshot["drivers"]) == 1)]
            findings.extend(worktime_findings(driver, served))

        # 各区段路线许可：存在、生效、覆盖区段窗口
        for segment in segments:
            seg_start = parse_dt(segment["departure_at"], "segment.departure_at")
            seg_end = parse_dt(segment["arrive_at"], "segment.arrive_at")
            rows = self._matching_licenses(connection, segment, seg_start, seg_end)
            active = [row for row in rows if row["status"] == "active"]
            if not rows:
                findings.append(Finding("permit.region_missing", BLOCKING, segment["segment_id"],
                                        f"辖区 {segment['region_code']} 没有任何覆盖该区段的路线许可",
                                        region_code=segment["region_code"],
                                        segment_id=segment["segment_id"]))
                continue
            if not active:
                findings.append(Finding("permit.region_suspended", BLOCKING, segment["segment_id"],
                                        f"辖区 {segment['region_code']} 的路线许可已暂停"
                                        "（如临时封路）",
                                        region_code=segment["region_code"],
                                        segment_id=segment["segment_id"],
                                        detail={"license_ids": [row["license_id"] for row in rows]}))
                continue
            covering = [row for row in active
                        if parse_dt(row["valid_from"], "license.valid_from") <= seg_start
                        and parse_dt(row["valid_to"], "license.valid_to") >= seg_end]
            if not covering:
                findings.append(Finding("permit.window_gap", BLOCKING, segment["segment_id"],
                                        f"辖区 {segment['region_code']} 的路线许可尚未生效或"
                                        "不能覆盖区段全程",
                                        region_code=segment["region_code"],
                                        segment_id=segment["segment_id"],
                                        detail={"segment_start": fmt(seg_start),
                                                "segment_end": fmt(seg_end),
                                                "license_windows": [
                                                    {"license_id": row["license_id"],
                                                     "valid_from": row["valid_from"],
                                                     "valid_to": row["valid_to"]}
                                                    for row in active]}))

        # 互斥路线：同一时间窗、同一辖区路线不得被两个有效团次占用
        for segment in segments:
            if not segment.get("route_code"):
                continue
            seg_start = parse_dt(segment["departure_at"], "segment.departure_at")
            seg_end = parse_dt(segment["arrive_at"], "segment.arrive_at")
            others = connection.execute(
                "SELECT cs.group_id, cs.version, cs.payload_json "
                "FROM charter_segments cs JOIN charter_groups cg ON cs.group_id=cg.group_id "
                "JOIN charter_versions cv ON cs.group_id=cv.group_id AND cs.version=cg.current_version "
                "WHERE cg.executable=1 AND cg.head_status<>? AND cs.route_code=? "
                "AND cs.region_code=? AND cs.group_id<>?",
                (HEAD_CANCELLED, segment["route_code"], segment["region_code"],
                 exclude_group or ""),
            ).fetchall()
            for other in others:
                other_payload = json.loads(other["payload_json"])
                other_start = parse_dt(other_payload["departure_at"], "segment.departure_at")
                other_end = parse_dt(other_payload["arrive_at"], "segment.arrive_at")
                if overlaps(seg_start, seg_end, other_start, other_end):
                    findings.append(Finding(
                        "route.mutex_conflict", BLOCKING, segment["segment_id"],
                        f"路线 {segment['route_code']} 在该时段已被团次 {other['group_id']} "
                        "占用，构成互斥路线冲突",
                        region_code=segment["region_code"], segment_id=segment["segment_id"],
                        detail={"other_group_id": other["group_id"],
                                "other_window": [other_payload["departure_at"],
                                                 other_payload["arrive_at"]]}))

        # 车辆与驾驶员跨团次重复占用（重复申报）
        active_rows = connection.execute(
            "SELECT cg.group_id,cg.current_version AS version,cg.travel_agency_org_id "
            "FROM charter_groups cg "
            "WHERE cg.executable=1 AND cg.head_status<>?",
            (HEAD_CANCELLED,),
        ).fetchall()
        for row in active_rows:
            if row["group_id"] == exclude_group:
                continue
            other_snapshot = json.loads(connection.execute(
                "SELECT payload_json FROM charter_versions WHERE group_id=? AND version=?",
                (row["group_id"], row["version"])).fetchone()["payload_json"])
            other_start, other_end = self._snapshot_window(other_snapshot)
            if not overlaps(window_start, window_end, other_start, other_end):
                continue
            if other_snapshot["vehicle"]["vehicle_id"] == vehicle["vehicle_id"]:
                findings.append(Finding(
                    "vehicle.double_booked", BLOCKING, vehicle["vehicle_id"],
                    f"车辆 {vehicle['vehicle_id']} 在同一时段已被团次 {row['group_id']} 申报",
                    detail={"other_group_id": row["group_id"]}))
            other_drivers = {d["driver_id"] for d in other_snapshot["drivers"]}
            for driver in snapshot["drivers"]:
                if driver["driver_id"] in other_drivers:
                    findings.append(Finding(
                        "driver.double_booked", BLOCKING, driver["driver_id"],
                        f"驾驶员 {driver['driver_id']} 在同一时段已承担团次 "
                        f"{row['group_id']} 的任务",
                        detail={"other_group_id": row["group_id"]}))

        # 同一旅行社重复申报（同合同号 + 同团号）
        dup_query = (
            "SELECT cg.group_id,cg.current_version AS version FROM charter_groups cg "
            "WHERE cg.head_status<>? AND cg.group_id<>?")
        parameters: list[Any] = [HEAD_CANCELLED, exclude_group or ""]
        if agency_org_id:
            dup_query += " AND cg.travel_agency_org_id=?"
            parameters.append(agency_org_id)
        for row in connection.execute(dup_query, parameters):
            other_snapshot = json.loads(connection.execute(
                "SELECT payload_json FROM charter_versions WHERE group_id=? AND version=?",
                (row["group_id"], row["version"])).fetchone()["payload_json"])
            if other_snapshot["contract"]["contract_no"] == snapshot["contract"]["contract_no"] \
                    and other_snapshot["tour"]["tour_code"] == snapshot["tour"]["tour_code"]:
                findings.append(Finding(
                    "duplicate.group_declaration", BLOCKING, snapshot["tour"]["tour_code"],
                    f"相同合同与团号已经以团次 {row['group_id']} 申报，请勿重复提交",
                    detail={"other_group_id": row["group_id"]}))
        return findings

    def _driver_segments(self, snapshot: dict[str, Any], driver_id: str) -> list[str]:
        result = []
        single = len(snapshot["drivers"]) == 1
        for segment in snapshot["segments"]:
            assigned = segment.get("driver_id")
            if assigned == driver_id or (not assigned and single):
                result.append(segment["segment_id"])
        return result

    def _blocking_by_segment(self, snapshot: dict[str, Any],
                            findings: list[Finding]) -> dict[str, list[dict[str, Any]]]:
        """把全局阻断结论映射到区段；区段无阻断结论时审批人方可批准。"""

        vehicle_id = snapshot["vehicle"]["vehicle_id"]
        blocked: dict[str, list[dict[str, Any]]] = {s["segment_id"]: [] for s in snapshot["segments"]}
        attached: set[int] = set()
        for index, finding in enumerate(findings):
            if finding.severity != BLOCKING:
                continue
            if finding.segment_id and finding.segment_id in blocked:
                blocked[finding.segment_id].append(finding.as_dict())
                attached.add(index)
            elif finding.subject == vehicle_id:
                for segment_id in blocked:
                    blocked[segment_id].append(finding.as_dict())
                attached.add(index)
            else:
                mapped = self._driver_segments(snapshot, finding.subject)
                for segment_id in mapped:
                    blocked[segment_id].append(finding.as_dict())
                if mapped:
                    attached.add(index)
        # 其余全局阻断（如重复申报）对所有区段生效
        for index, finding in enumerate(findings):
            if finding.severity == BLOCKING and index not in attached:
                for segment_id in blocked:
                    blocked[segment_id].append(finding.as_dict())
        return blocked

    def submit_charter_group(self, *, request_id: str, actor_id: str, group_id: str,
                             contract: dict[str, Any], tour: dict[str, Any],
                             vehicle: dict[str, Any], drivers: list[dict[str, Any]],
                             segments: list[dict[str, Any]],
                             stops: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body = {"contract": contract, "tour": tour, "vehicle": vehicle, "drivers": drivers,
                "segments": segments, "stops": stops or []}
        payload = {"actor_id": actor_id, "group_id": group_id, **body}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", AGENT_ROLE)
            group_id = self._identifier(group_id, "group_id")
            snapshot = self._validate_snapshot(body)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM charter_groups WHERE group_id=?",
                                      (group_id,)).fetchone():
                    raise ConflictError("团次已存在，变化请通过行程事件申报新版本")
                findings = self._evaluate(connection, snapshot, exclude_group=group_id)
                now = self._now()
                connection.execute(
                    "INSERT INTO charter_groups(group_id,travel_agency_org_id,current_version,"
                    "head_status,executable,created_at,updated_at) VALUES(?,?,1,?,0,?,?)",
                    (group_id, actor["organization_id"], HEAD_REVIEW, now, now),
                )
                self._persist_version(connection, group_id=group_id, version=1,
                                      parent_version=None, trigger_event_id=None,
                                      status=HEAD_REVIEW, snapshot=snapshot,
                                      created_by=actor_id)
                self._audit(connection, actor_id=actor_id, action="charter.submitted",
                            resource_type="charter_group", resource_id=group_id,
                            detail={"version": 1,
                                    "segments": len(snapshot["segments"]),
                                    "blocking": sum(1 for f in findings if f.severity == BLOCKING),
                                    "regions": sorted({s["region_code"] for s in snapshot["segments"]})})
                return "charter_group", group_id, {"group_id": group_id, "version": 1}

            receipt = self._idempotent(connection, request_id=request_id,
                                       action="submit_charter_group", payload=payload,
                                       create=create)
            return receipt

    def _persist_version(self, connection, *, group_id: str, version: int,
                         parent_version: int | None, trigger_event_id: str | None,
                         status: str, snapshot: dict[str, Any], created_by: str) -> None:
        now = self._now()
        connection.execute(
            "INSERT INTO charter_versions(group_id,version,parent_version,trigger_event_id,"
            "status,executable,started_at,payload_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,0,NULL,?,?,?)",
            (group_id, version, parent_version, trigger_event_id, status,
             canonical_json(snapshot), created_by, now),
        )
        for segment in sorted(snapshot["segments"], key=lambda item: item["ordinal"]):
            connection.execute(
                "INSERT INTO charter_segments(group_id,version,segment_id,region_code,ordinal,"
                "route_code,payload_json) VALUES(?,?,?,?,?,?,?)",
                (group_id, version, segment["segment_id"], segment["region_code"],
                 segment["ordinal"], segment.get("route_code"), canonical_json(segment)),
            )

    # --------------------------------------------------------------- 审批链

    def decide_segment(self, *, request_id: str, actor_id: str, group_id: str,
                       version: int, segment_id: str, decision: str,
                       reason: str = "", basis_license_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "group_id": group_id, "version": version,
                   "segment_id": segment_id, "decision": decision, "reason": reason,
                   "basis_license_id": basis_license_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", REVIEWER_ROLE)
            group = self._get_group(connection, group_id)
            snapshot = self._get_snapshot(connection, group_id, version)
            segment = next((s for s in snapshot["segments"] if s["segment_id"] == segment_id), None)
            if segment is None:
                raise NotFoundError("区段不存在")
            if actor["role"] == REVIEWER_ROLE and \
                    segment["region_code"] not in self._reviewer_regions(connection, actor_id):
                raise PermissionDenied("审批人只能处理本辖区责任区段")
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 只能是 approved 或 rejected")

            def create() -> tuple[str, str, dict[str, Any]]:
                if group["head_status"] == HEAD_CANCELLED:
                    raise ConflictError("团次已取消，不能再审批")
                if version != group["current_version"]:
                    raise ConflictError("只能审批团次当前版本")
                existing = connection.execute(
                    "SELECT * FROM segment_reviews WHERE group_id=? AND version=? AND segment_id=?",
                    (group_id, version, segment_id)).fetchone()
                if existing and existing["decision"] == "approved" and decision == "approved":
                    raise ConflictError("该区段已批准；事实批准不可改写")
                findings = self._evaluate(connection, snapshot, exclude_group=group_id)
                blocked = self._blocking_by_segment(snapshot, findings)[segment_id]
                if decision == "approved" and blocked:
                    raise ConflictError("区段存在未消除的阻断项，不能批准")
                basis = None
                if decision == "approved":
                    basis = self._resolve_basis(connection, segment, basis_license_id)
                if existing:
                    connection.execute(
                        "UPDATE segment_reviews SET decision=?,basis_license_id=?,reason=?,"
                        "reviewed_by=?,reviewed_at=? WHERE group_id=? AND version=? AND segment_id=?",
                        (decision, basis, reason or None, actor_id, self._now(),
                         group_id, version, segment_id))
                else:
                    connection.execute(
                        "INSERT INTO segment_reviews(group_id,version,segment_id,region_code,"
                        "decision,basis_license_id,reason,reviewed_by,reviewed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (group_id, version, segment_id, segment["region_code"], decision,
                         basis, reason or None, actor_id, self._now()))
                self._audit(connection, actor_id=actor_id, action=f"segment.{decision}",
                            resource_type="segment_review",
                            resource_id=f"{group_id}:{version}:{segment_id}",
                            detail={"region_code": segment["region_code"],
                                    "basis_license_id": basis, "reason": reason})
                status, executable = self._recompute_head(connection, group_id, version, snapshot)
                return "segment_review", f"{group_id}:{version}:{segment_id}", \
                    {"group_id": group_id, "version": version, "segment_id": segment_id,
                     "decision": decision, "head_status": status, "executable": executable}

            return self._idempotent(connection, request_id=request_id,
                                    action="decide_segment", payload=payload, create=create)

    def _resolve_basis(self, connection, segment: dict[str, Any],
                       basis_license_id: str | None) -> str:
        seg_start = parse_dt(segment["departure_at"], "segment.departure_at")
        seg_end = parse_dt(segment["arrive_at"], "segment.arrive_at")
        rows = self._matching_licenses(connection, segment, seg_start, seg_end)
        rows = [row for row in rows if row["status"] == "active"
                and parse_dt(row["valid_from"], "license.valid_from") <= seg_start
                and parse_dt(row["valid_to"], "license.valid_to") >= seg_end]
        if not rows:
            raise ConflictError("辖区内没有覆盖该区段的有效路线许可，无法作为批准依据")
        if basis_license_id is None:
            return sorted(rows, key=lambda row: row["license_id"])[0]["license_id"]
        basis = next((row for row in rows if row["license_id"] == basis_license_id), None)
        if basis is None:
            raise NotFoundError("指定的许可依据不存在或不覆盖该区段")
        return basis_license_id

    def _recompute_head(self, connection, group_id: str, version: int,
                        snapshot: dict[str, Any]) -> tuple[str, bool]:
        """根据各区段批准与阻断结论重算版本及团次状态。"""

        findings = self._evaluate(connection, snapshot, exclude_group=group_id)
        blocked = self._blocking_by_segment(snapshot, findings)
        rows = connection.execute(
            "SELECT * FROM segment_reviews WHERE group_id=? AND version=?",
            (group_id, version)).fetchall()
        reviews = {row["segment_id"]: row for row in rows}
        effective = True
        statuses: dict[str, str] = {}
        for segment in snapshot["segments"]:
            review = reviews.get(segment["segment_id"])
            if review is None or review["decision"] != "approved" or blocked[segment["segment_id"]]:
                effective = False
            statuses[segment["segment_id"]] = review["decision"] if review else "pending"
        rejected = any(row["decision"] == "rejected" for row in rows)
        if rejected and not any(value == "pending" for value in statuses.values()) and not effective:
            new_status = "rejected"
        else:
            new_status = HEAD_EXECUTABLE if effective else HEAD_REVIEW
        group = connection.execute("SELECT * FROM charter_groups WHERE group_id=?",
                                   (group_id,)).fetchone()
        head_status = group["head_status"]
        if head_status in (HEAD_IN_PROGRESS, HEAD_COMPLETED):
            # 行程已开始：状态不回退，只更新可执行标志（局部异常时为 False）
            pass
        else:
            head_status = HEAD_CANCELLED if new_status == HEAD_CANCELLED else new_status
        connection.execute(
            "UPDATE charter_versions SET status=?,executable=? WHERE group_id=? AND version=?",
            (new_status, 1 if effective else 0, group_id, version))
        connection.execute(
            "UPDATE charter_groups SET head_status=?,executable=?,updated_at=? WHERE group_id=?",
            (head_status, 1 if effective else 0, self._now(), group_id))
        return head_status, effective

    # ----------------------------------------------------------- 版本与查询

    def _get_group(self, connection, group_id: str):
        group = connection.execute("SELECT * FROM charter_groups WHERE group_id=?",
                                   (group_id,)).fetchone()
        if group is None:
            raise NotFoundError("团次不存在")
        return group

    def _get_snapshot(self, connection, group_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM charter_versions WHERE group_id=? AND version=?",
            (group_id, version)).fetchone()
        if row is None:
            raise NotFoundError("团次版本不存在")
        return json.loads(row["payload_json"])

    def _version_view(self, connection, group_id: str, version: int,
                      snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        version_row = connection.execute(
            "SELECT * FROM charter_versions WHERE group_id=? AND version=?",
            (group_id, version)).fetchone()
        snapshot = snapshot or json.loads(version_row["payload_json"])
        findings = self._evaluate(connection, snapshot, exclude_group=group_id)
        blocked = self._blocking_by_segment(snapshot, findings)
        review_rows = connection.execute(
            "SELECT * FROM segment_reviews WHERE group_id=? AND version=? ORDER BY rowid",
            (group_id, version)).fetchall()
        reviews = {row["segment_id"]: row for row in review_rows}
        segment_views = []
        for segment in sorted(snapshot["segments"], key=lambda item: item["ordinal"]):
            review = reviews.get(segment["segment_id"])
            blockers = blocked[segment["segment_id"]]
            segment_view = {
                "segment_id": segment["segment_id"],
                "region_code": segment["region_code"],
                "ordinal": segment["ordinal"],
                "from_site": segment["from_site"],
                "to_site": segment["to_site"],
                "departure_at": segment["departure_at"],
                "arrive_at": segment["arrive_at"],
                "route_code": segment.get("route_code"),
                "review_status": review["decision"] if review else "pending",
                "reviewed_by": review["reviewed_by"] if review else None,
                "reviewed_at": review["reviewed_at"] if review else None,
                "basis_license_id": review["basis_license_id"] if review else None,
                "carried_from_version": review["carried_from_version"] if review else None,
                "effective": bool(review and review["decision"] == "approved" and not blockers),
                "blocking_findings": blockers,
            }
            segment_views.append(segment_view)
        return {
            "group_id": group_id,
            "version": version,
            "parent_version": version_row["parent_version"],
            "trigger_event_id": version_row["trigger_event_id"],
            "status": version_row["status"],
            "executable": bool(version_row["executable"]),
            "segments": segment_views,
            "findings": [finding.as_dict() for finding in findings],
        }

    def get_group(self, group_id: str) -> dict[str, Any]:
        """旅行社视角：团次状态、阻塞原因、可沿用审批与退款责任。"""

        with self.database.transaction() as connection:
            group = self._get_group(connection, group_id)
            version_rows = connection.execute(
                "SELECT version,parent_version,trigger_event_id,status,executable "
                "FROM charter_versions WHERE group_id=? ORDER BY version", (group_id,)).fetchall()
            current = self._version_view(connection, group_id, group["current_version"])
            current_snapshot = self._get_snapshot(connection, group_id, group["current_version"])
            carried = [s for s in current["segments"] if s["carried_from_version"]]
            response = {
                "group_id": group_id,
                "current_version": group["current_version"],
                "head_status": group["head_status"],
                "executable": bool(group["executable"]),
                "started": group["head_status"] in (HEAD_IN_PROGRESS, HEAD_COMPLETED),
                "versions": [dict(row) for row in version_rows],
                "current": current,
                "carried_approvals": [
                    {"segment_id": s["segment_id"], "region_code": s["region_code"],
                     "carried_from_version": s["carried_from_version"]} for s in carried],
                "blocking_reasons": self._blocking_summary(current),
                "refund_responsibility": self._refund_view(connection, group, current_snapshot),
                "snapshot": current_snapshot,
            }
            return response

    def _blocking_summary(self, version_view: dict[str, Any]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        summary: list[dict[str, Any]] = []
        for finding in version_view["findings"]:
            if finding["severity"] != BLOCKING:
                continue
            key = f"{finding['code']}:{finding['subject']}:{finding.get('segment_id')}"
            if key in seen:
                continue
            seen.add(key)
            summary.append({"code": finding["code"], "subject": finding["subject"],
                            "segment_id": finding.get("segment_id"),
                            "region_code": finding.get("region_code"),
                            "message": finding["message"]})
        return summary

    def _refund_view(self, connection, group, snapshot: dict[str, Any]) -> dict[str, Any]:
        policy = snapshot["contract"].get("refund_policy", {})
        head_status = group["head_status"]
        if head_status == HEAD_CANCELLED:
            event = connection.execute(
                "SELECT * FROM trip_events WHERE group_id=? AND event_type='cancel' "
                "ORDER BY created_at DESC LIMIT 1", (group["group_id"],)).fetchone()
            cause = json.loads(event["payload_json"]).get("cause", "plan_change") if event else "plan_change"
            return {"applicable": True, "cause": cause,
                    "term": policy.get(cause) or policy.get("plan_change"),
                    "policy": policy}
        blockers = self._blocking_summary(
            self._version_view(connection, group["group_id"], group["current_version"]))
        if not group["executable"] and blockers:
            agency_fault = {b["code"] for b in blockers}
            if agency_fault & {"license.expired", "license.required_missing",
                               "license.not_yet_effective", "worktime.duty_day_exceeded",
                               "worktime.daily_driving_exceeded",
                               "worktime.continuous_driving_exceeded",
                               "duplicate.group_declaration", "vehicle.double_booked",
                               "driver.double_booked"}:
                return {"applicable": False, "cause": "agency_fault",
                        "term": policy.get("agency_fault"), "policy": policy,
                        "note": "阻塞由旅行社材料或排班造成，按合同由旅行社承担退款责任"}
            return {"applicable": False, "cause": "pending_review",
                    "term": policy.get("plan_change"), "policy": policy,
                    "note": "等待辖区审批，暂无退款责任结论"}
        return {"applicable": False, "cause": None, "term": None, "policy": policy}

    # ------------------------------------------------------------- 行程事件

    def register_trip_event(self, *, request_id: str, actor_id: str, group_id: str,
                            event_type: str, **changes: Any) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "group_id": str(group_id),
                   "event_type": event_type, "changes": changes}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", AGENT_ROLE)
            group = self._get_group(connection, group_id)
            if event_type not in EVENT_SCOPE:
                raise ValidationError(f"未知事件类型 {event_type}")

            if event_type == "cancel":
                return self._cancel(connection, actor=actor, group=group,
                                    request_id=request_id, changes=changes, payload=payload)

            def create() -> tuple[str, str, dict[str, Any]]:
                if group["head_status"] == HEAD_CANCELLED:
                    raise ConflictError("团次已取消，不能再申报变更")
                started = group["head_status"] in (HEAD_IN_PROGRESS, HEAD_COMPLETED)
                if not group["executable"] and not started and event_type != "road_closure":
                    raise ConflictError("行程尚未签发可执行许可，不能申报履约事件")
                base_snapshot = self._get_snapshot(connection, group_id, group["current_version"])
                new_snapshot, affected, scope_detail = self._apply_event(
                    event_type, base_snapshot, changes)
                new_snapshot = self._validate_snapshot(new_snapshot)
                event_id = uuid.uuid4().hex
                new_version = group["current_version"] + 1
                # 已开始行程的局部事件只重审受影响区段，其余批准沿用原事实
                if started and event_type in LOCAL_EVENTS:
                    impact_scope = IMPACT_SEGMENTS
                else:
                    impact_scope = EVENT_SCOPE[event_type]
                if event_type == "pax_change" and scope_detail.get("within_tolerance"):
                    impact_scope = IMPACT_NONE
                connection.execute(
                    "INSERT INTO trip_events(event_id,group_id,version,event_type,impact_scope,"
                    "affected_segments_json,payload_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, group_id, new_version, event_type, impact_scope,
                     canonical_json(affected), canonical_json(changes), actor_id, self._now()))
                self._persist_version(connection, group_id=group_id, version=new_version,
                                      parent_version=group["current_version"],
                                      trigger_event_id=event_id,
                                      status=HEAD_REVIEW if impact_scope != IMPACT_NONE
                                      else HEAD_EXECUTABLE,
                                      snapshot=new_snapshot, created_by=actor_id)
                self._carry_approvals(connection, group_id=group_id,
                                      old_version=group["current_version"],
                                      new_version=new_version, snapshot=new_snapshot,
                                      affected=set(affected), impact_scope=impact_scope,
                                      actor_id=actor_id)
                head_status = group["head_status"]
                if impact_scope == IMPACT_NONE:
                    head_status = HEAD_IN_PROGRESS if started else HEAD_EXECUTABLE
                elif started:
                    head_status = HEAD_IN_PROGRESS
                else:
                    head_status = HEAD_REVIEW
                connection.execute(
                    "UPDATE charter_groups SET current_version=?,head_status=?,"
                    "updated_at=? WHERE group_id=?",
                    (new_version, head_status, self._now(), group_id))
                status, executable = self._recompute_head(
                    connection, group_id, new_version, new_snapshot)
                self._audit(connection, actor_id=actor_id, action=f"trip_event.{event_type}",
                            resource_type="trip_event", resource_id=event_id,
                            detail={"group_id": group_id, "old_version": group["current_version"],
                                    "new_version": new_version, "impact_scope": impact_scope,
                                    "affected_segments": sorted(affected),
                                    "head_status": status, "executable": executable})
                carried = [s["segment_id"] for s in
                           self._version_view(connection, group_id, new_version)["segments"]
                           if s["carried_from_version"]]
                return "trip_event", event_id, {"event_id": event_id, "group_id": group_id,
                                                "version": new_version,
                                                "impact_scope": impact_scope,
                                                "affected_segments": sorted(affected),
                                                "head_status": status, "executable": executable,
                                                "carried_segments": carried}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_trip_event", payload=payload, create=create)

    def _apply_event(self, event_type: str, snapshot: dict[str, Any],
                     changes: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        import copy
        new_snapshot = copy.deepcopy(snapshot)
        detail: dict[str, Any] = {}
        if event_type == "pax_change":
            new_count = changes.get("passenger_count")
            if not isinstance(new_count, int) or new_count <= 0:
                raise ValidationError("passenger_count 必须是正整数")
            old_count = snapshot["tour"]["passenger_count"]
            within = pax_within_tolerance(old_count, new_count)
            new_snapshot["tour"]["passenger_count"] = new_count
            detail = {"within_tolerance": within, "old_count": old_count, "new_count": new_count}
            affected = [] if within else [s["segment_id"] for s in snapshot["segments"]]
            return new_snapshot, affected, detail
        if event_type == "vehicle_change":
            vehicle = changes.get("vehicle")
            if not isinstance(vehicle, dict):
                raise ValidationError("vehicle_change 必须提供新的 vehicle")
            new_snapshot["vehicle"] = vehicle
            return new_snapshot, [s["segment_id"] for s in snapshot["segments"]], detail
        if event_type == "driver_change":
            old_driver_id = self._text(changes.get("old_driver_id", ""), "old_driver_id", 80)
            driver = changes.get("driver")
            if not isinstance(driver, dict):
                raise ValidationError("driver_change 必须提供替班 driver")
            if old_driver_id not in {d["driver_id"] for d in snapshot["drivers"]}:
                raise NotFoundError("被替换驾驶员不在原申报中")
            new_snapshot["drivers"] = [
                driver if d["driver_id"] == old_driver_id else d for d in snapshot["drivers"]
            ]
            for segment in new_snapshot["segments"]:
                if segment.get("driver_id") == old_driver_id:
                    segment["driver_id"] = driver["driver_id"]
            affected = [s["segment_id"] for s in snapshot["segments"]
                        if s.get("driver_id") == old_driver_id
                        or (not s.get("driver_id") and len(snapshot["drivers"]) == 1)]
            detail = {"old_driver_id": old_driver_id, "new_driver_id": driver["driver_id"]}
            return new_snapshot, affected, detail
        if event_type == "road_closure":
            affected = changes.get("affected_segment_ids")
            reroute = changes.get("reroute", {})
            if not isinstance(affected, list) or not affected:
                raise ValidationError("road_closure 必须提供 affected_segment_ids")
            known = {s["segment_id"] for s in snapshot["segments"]}
            if not set(affected) <= known:
                raise ValidationError("受影响区段包含未申报的区段")
            for segment in new_snapshot["segments"]:
                if segment["segment_id"] in affected:
                    patch = reroute.get(segment["segment_id"], {})
                    if not isinstance(patch, dict):
                        raise ValidationError("reroute 必须按区段提供修改")
                    segment.update(patch)
                    segment["rerouted"] = True
            return new_snapshot, list(affected), {"cause": "road_closure"}
        if event_type == "stop_adjustment":
            affected = changes.get("affected_segment_ids")
            stops = changes.get("stops")
            if not isinstance(affected, list) or not isinstance(stops, list):
                raise ValidationError("stop_adjustment 必须提供 affected_segment_ids 与 stops")
            new_snapshot["stops"] = stops
            return new_snapshot, list(affected), {"cause": "stop_adjustment"}
        raise ValidationError(f"事件类型 {event_type} 无法应用")

    def _carry_approvals(self, connection, *, group_id: str, old_version: int,
                         new_version: int, snapshot: dict[str, Any], affected: set[str],
                         impact_scope: str, actor_id: str) -> None:
        """把不受影响区段的原批准复制到新版本，并记录来自哪个版本。"""

        if impact_scope == IMPACT_ALL:
            return
        old_rows = connection.execute(
            "SELECT * FROM segment_reviews WHERE group_id=? AND version=?",
            (group_id, old_version)).fetchall()
        for row in old_rows:
            if row["segment_id"] in affected:
                continue
            connection.execute(
                "INSERT INTO segment_reviews(group_id,version,segment_id,region_code,decision,"
                "basis_license_id,reason,reviewed_by,reviewed_at,carried_from_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (group_id, new_version, row["segment_id"], row["region_code"], row["decision"],
                 row["basis_license_id"], row["reason"], row["reviewed_by"],
                 self._now(), old_version))
            self._audit(connection, actor_id=actor_id, action="approval.carried",
                        resource_type="segment_review",
                        resource_id=f"{group_id}:{new_version}:{row['segment_id']}",
                        detail={"from_version": old_version, "to_version": new_version,
                                "original_reviewed_by": row["reviewed_by"],
                                "original_reviewed_at": row["reviewed_at"]})

    def _cancel(self, connection, *, actor, group, request_id: str,
                changes: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        cause = self._text(changes.get("cause", "plan_change"), "cause", 40)
        old_version = group["current_version"]
        snapshot = self._get_snapshot(connection, group["group_id"], old_version)

        def create() -> tuple[str, str, dict[str, Any]]:
            event_id = uuid.uuid4().hex
            new_version = old_version + 1
            connection.execute(
                "INSERT INTO trip_events(event_id,group_id,version,event_type,impact_scope,"
                "affected_segments_json,payload_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (event_id, group["group_id"], new_version, "cancel", IMPACT_ALL,
                 canonical_json([s["segment_id"] for s in snapshot["segments"]]),
                 canonical_json(changes), actor["actor_id"], self._now()))
            # 取消版本保留全部原批准事实（只读复制），但团次不再可执行
            self._persist_version(connection, group_id=group["group_id"], version=new_version,
                                  parent_version=old_version, trigger_event_id=event_id,
                                  status=HEAD_CANCELLED, snapshot=snapshot,
                                  created_by=actor["actor_id"])
            old_rows = connection.execute(
                "SELECT * FROM segment_reviews WHERE group_id=? AND version=?",
                (group["group_id"], old_version)).fetchall()
            for row in old_rows:
                connection.execute(
                    "INSERT INTO segment_reviews(group_id,version,segment_id,region_code,decision,"
                    "basis_license_id,reason,reviewed_by,reviewed_at,carried_from_version) "
                    "VALUES(?,?,?,?,'revoked',?,?,?,?,?)",
                    (group["group_id"], new_version, row["segment_id"], row["region_code"],
                     row["basis_license_id"], f"团次取消（{cause}），原批准事实保留",
                     row["reviewed_by"], self._now(), old_version))
            connection.execute(
                "UPDATE charter_groups SET current_version=?,head_status=?,executable=0,"
                "updated_at=? WHERE group_id=?",
                (new_version, HEAD_CANCELLED, self._now(), group["group_id"]))
            connection.execute(
                "UPDATE charter_versions SET status=?,executable=0 WHERE group_id=? AND version=?",
                (HEAD_CANCELLED, group["group_id"], new_version))
            self._audit(connection, actor_id=actor["actor_id"], action="trip_event.cancel",
                        resource_type="trip_event", resource_id=event_id,
                        detail={"group_id": group["group_id"], "cause": cause,
                                "old_version": old_version, "new_version": new_version})
            refund = self._refund_view(
                connection,
                connection.execute("SELECT * FROM charter_groups WHERE group_id=?",
                                   (group["group_id"],)).fetchone(),
                snapshot)
            return "trip_event", event_id, {"event_id": event_id,
                                            "group_id": group["group_id"],
                                            "version": new_version,
                                            "impact_scope": IMPACT_ALL,
                                            "refund_responsibility": refund}

        return self._idempotent(connection, request_id=request_id, action="cancel_charter",
                                payload=payload, create=create)

    def mark_trip_started(self, *, request_id: str, actor_id: str, group_id: str) -> dict[str, Any]:
        """行程实际发车：锁定为履约中，此后普通改动不得整体回退。"""

        payload = {"actor_id": actor_id, "group_id": group_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_roles(actor, "admin", AGENT_ROLE)
            group = self._get_group(connection, group_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                if group["head_status"] != HEAD_EXECUTABLE:
                    raise ConflictError("只有已签发可执行许可的团次才能开始行程")
                now = self._now()
                connection.execute(
                    "UPDATE charter_groups SET head_status=?,updated_at=? WHERE group_id=?",
                    (HEAD_IN_PROGRESS, now, group_id))
                connection.execute(
                    "UPDATE charter_versions SET status=?,started_at=? WHERE group_id=? AND version=?",
                    (HEAD_IN_PROGRESS, now, group_id, group["current_version"]))
                event_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO trip_events(event_id,group_id,version,event_type,impact_scope,"
                    "affected_segments_json,payload_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, group_id, group["current_version"], "start", IMPACT_NONE, "[]",
                     canonical_json({}), actor_id, now))
                self._audit(connection, actor_id=actor_id, action="trip_event.start",
                            resource_type="trip_event", resource_id=event_id,
                            detail={"group_id": group_id, "version": group["current_version"]})
                return "trip_event", event_id, {"event_id": event_id, "group_id": group_id,
                                                "head_status": HEAD_IN_PROGRESS}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_trip_started", payload=payload, create=create)

    # --------------------------------------------------------------- 执法核验

    def inspection_verify(self, *, api_key: str, group_id: str) -> dict[str, Any]:
        """执法接口：看到当前有效许可、责任地区与异常处置依据。"""

        with self.database.transaction() as connection:
            key_row = connection.execute(
                "SELECT * FROM inspection_keys WHERE api_key=? AND active=1", (api_key,)
            ).fetchone()
            if key_row is None:
                raise PermissionDenied("核验密钥无效或已停用")
            group = self._get_group(connection, group_id)
            version = group["current_version"]
            snapshot = self._get_snapshot(connection, group_id, version)
            view = self._version_view(connection, group_id, version, snapshot)
            events = connection.execute(
                "SELECT event_id,version,event_type,impact_scope,affected_segments_json,"
                "payload_json,created_at FROM trip_events WHERE group_id=? ORDER BY version,rowid",
                (group_id,)).fetchall()
            region_filter = key_row["region_code"]
            segments = []
            for segment_view in view["segments"]:
                if region_filter and segment_view["region_code"] != region_filter:
                    continue
                segments.append({
                    "segment_id": segment_view["segment_id"],
                    "region_code": segment_view["region_code"],
                    "window": [segment_view["departure_at"], segment_view["arrive_at"]],
                    "route_code": segment_view["route_code"],
                    "effective": segment_view["effective"],
                    "permit": {
                        "review_status": segment_view["review_status"],
                        "basis_license_id": segment_view["basis_license_id"],
                        "approved_by": segment_view["reviewed_by"],
                        "approved_at": segment_view["reviewed_at"],
                        "carried_from_version": segment_view["carried_from_version"],
                    },
                    "blocking_findings": segment_view["blocking_findings"],
                })
            handling = []
            segment_regions = {s["segment_id"]: s["region_code"] for s in snapshot["segments"]}
            for event in events:
                affected = json.loads(event["affected_segments_json"])
                if region_filter:
                    regions = {segment_regions.get(segment_id) for segment_id in affected}
                    if regions and region_filter not in regions:
                        continue
                handling.append({
                    "event_id": event["event_id"],
                    "version": event["version"],
                    "event_type": event["event_type"],
                    "impact_scope": event["impact_scope"],
                    "affected_segments": affected,
                    "basis": json.loads(event["payload_json"]),
                    "created_at": event["created_at"],
                })
            return {
                "group_id": group_id,
                "verified_at": self._now(),
                "head_status": group["head_status"],
                "executable": bool(group["executable"]),
                "started": view is not None and group["head_status"]
                           in (HEAD_IN_PROGRESS, HEAD_COMPLETED),
                "current_version": version,
                "vehicle_id": snapshot["vehicle"]["vehicle_id"],
                "driver_ids": [d["driver_id"] for d in snapshot["drivers"]],
                "passenger_count": snapshot["tour"]["passenger_count"],
                "segments": segments,
                "exception_handling": handling,
                "overall_blocking": self._blocking_summary(view),
            }
