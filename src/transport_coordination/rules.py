"""旅游包车联审使用的静态规则引擎。

规则只接收结构化快照与当前时间，不访问数据库，因此可以在提交、
重审、执法核验等任何环节重复执行，得到一致结论。所有业务时间使用
统一的业务时钟（提交方保证为同一时区的本地时间）；带偏移量的时间
会被归一化为朴素时间，避免跨区换算掩盖跨日工时问题。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from .errors import ValidationError

# 单日出勤总时长（首次上车到末次下车）上限
DUTY_DAY_LIMIT_HOURS = 13
# 单日累计驾驶上限
DRIVING_DAILY_LIMIT_HOURS = 8
# 连续驾驶上限，超过前必须休息
CONTINUOUS_DRIVING_LIMIT_HOURS = 4
# 阻断连续驾驶所需的最短休息
REST_MIN_MINUTES = 20
# 旅客人数相对原批准值允许直接沿用的波动比例
PAX_CARRY_TOLERANCE = 0.10

BLOCKING = "blocking"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """描述一条规则结论。"""

    code: str
    severity: str
    subject: str
    message: str
    region_code: str | None = None
    segment_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "region_code": self.region_code,
            "segment_id": self.segment_id,
            "detail": self.detail,
        }


def parse_dt(value: Any, field_name: str) -> datetime:
    """把 ISO 文本解析为统一业务时钟下的朴素时间。"""

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} 必须是 ISO 时间字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field_name} 时间格式无效: {value}") from exc
    if parsed.tzinfo is not None:
        # 业务统一按同一时钟填报；丢弃偏移，只保留墙钟时间用于跨日判断
        parsed = parsed.replace(tzinfo=None)
    return parsed


def fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def qualification_findings(subject_type: str, subject_id: str,
                           qualifications: Iterable[dict[str, Any]],
                           window_start: datetime, window_end: datetime,
                           required_types: Iterable[str]) -> list[Finding]:
    """检查资质证照是否齐备且在整个行程窗口内有效。"""

    findings: list[Finding] = []
    qualifications = list(qualifications)
    by_type: dict[str, dict[str, Any]] = {}
    for qualification in qualifications:
        qtype = qualification.get("type")
        if not qtype:
            findings.append(Finding("license.missing_type", BLOCKING, subject_id,
                                    f"{subject_type} 存在未标注类型的证照",
                                    detail={"qualification": qualification}))
            continue
        valid_from = parse_dt(qualification["valid_from"], f"{subject_id}.{qtype}.valid_from") \
            if qualification.get("valid_from") else None
        valid_to = parse_dt(qualification["valid_to"], f"{subject_id}.{qtype}.valid_to") \
            if qualification.get("valid_to") else None
        if valid_from and valid_to and valid_from > valid_to:
            findings.append(Finding("license.inverted_range", BLOCKING, subject_id,
                                    f"{subject_type} 的 {qtype} 证照有效期起止颠倒",
                                    detail={"type": qtype}))
        if valid_to is not None and valid_to < window_start:
            findings.append(Finding("license.expired", BLOCKING, subject_id,
                                    f"{subject_type} 的 {qtype} 证照在行程开始前已到期",
                                    detail={"type": qtype, "valid_to": fmt(valid_to),
                                            "window_start": fmt(window_start)}))
        elif valid_to is not None and valid_to < window_end:
            findings.append(Finding("license.covers_partial", BLOCKING, subject_id,
                                    f"{subject_type} 的 {qtype} 证照不能覆盖全程，"
                                    f"{fmt(valid_to)} 后失效",
                                    detail={"type": qtype, "valid_to": fmt(valid_to),
                                            "window_end": fmt(window_end)}))
        if valid_from is not None and valid_from > window_start:
            findings.append(Finding("license.not_yet_effective", BLOCKING, subject_id,
                                    f"{subject_type} 的 {qtype} 证照在行程开始时尚未生效",
                                    detail={"type": qtype, "valid_from": fmt(valid_from),
                                            "window_start": fmt(window_start)}))
        by_type[qtype] = qualification
    for required in required_types:
        if required not in by_type:
            findings.append(Finding("license.required_missing", BLOCKING, subject_id,
                                    f"{subject_type} 缺少必需证照 {required}",
                                    detail={"type": required}))
    return findings


def _date_range(start: datetime, end: datetime):
    current = datetime(start.year, start.month, start.day)
    last = datetime(end.year, end.month, end.day)
    while current <= last:
        day_end = current + timedelta(days=1)
        yield current, day_end
        current = day_end


def worktime_findings(driver: dict[str, Any], segments: list[dict[str, Any]]) -> list[Finding]:
    """检查驾驶员跨日出勤、单日累计驾驶与连续驾驶。"""

    findings: list[Finding] = []
    driver_id = driver["driver_id"]
    periods: list[tuple[datetime, datetime]] = []
    for period in driver.get("duty_periods", []):
        on = parse_dt(period["on"], f"{driver_id}.duty.on")
        off = parse_dt(period["off"], f"{driver_id}.duty.off")
        if on >= off:
            findings.append(Finding("worktime.inverted_duty", BLOCKING, driver_id,
                                    "驾驶员存在下班不晚于上班的值乘时段",
                                    segment_id=period.get("segment_id")))
            continue
        periods.append((on, off))

    driving_windows = [
        (parse_dt(segment["departure_at"], "segment.departure_at"),
         parse_dt(segment["arrive_at"], "segment.arrive_at"))
        for segment in segments
        if segment.get("driver_id", "default") in (driver_id, "default") or len(periods) == 0
    ]
    if not periods or not driving_windows:
        return findings

    # 申报的满 20 分钟休息会把连续驾驶窗口切开（含跨日行驶途中的休息）
    rest_periods: list[tuple[datetime, datetime]] = []
    for rest in driver.get("rest_periods", []):
        rest_start = parse_dt(rest["start"], f"{driver_id}.rest.start")
        rest_end = parse_dt(rest["end"], f"{driver_id}.rest.end")
        if rest_end <= rest_start:
            findings.append(Finding("worktime.inverted_rest", BLOCKING, driver_id,
                                    "驾驶员存在结束不晚于开始的休息时段"))
            continue
        if (rest_end - rest_start).total_seconds() / 60 < REST_MIN_MINUTES:
            # 不足 20 分钟的停靠不构成法定休息，不切窗
            continue
        rest_periods.append((rest_start, rest_end))

    split_windows: list[tuple[datetime, datetime]] = []
    for drive_start, drive_end in driving_windows:
        cursor = drive_start
        for rest_start, rest_end in sorted(rest_periods):
            clipped_start = max(rest_start, cursor)
            clipped_end = min(rest_end, drive_end)
            if clipped_end > clipped_start:
                if clipped_start > cursor:
                    split_windows.append((cursor, clipped_start))
                cursor = clipped_end
        if drive_end > cursor:
            split_windows.append((cursor, drive_end))
    driving_windows = split_windows

    window_start = min(on for on, _ in periods)
    window_end = max(off for _, off in periods)

    for on, off in periods:
        if (off - on).total_seconds() / 3600 > DUTY_DAY_LIMIT_HOURS:
            findings.append(Finding("worktime.duty_day_exceeded", BLOCKING, driver_id,
                                    f"值乘时段 {fmt(on)} 至 {fmt(off)} 跨日连续出勤 "
                                    f"{(off - on).total_seconds() / 3600:.1f} 小时，"
                                    f"超过 {DUTY_DAY_LIMIT_HOURS} 小时上限",
                                    detail={"on": fmt(on), "off": fmt(off)}))
    for day_start, day_end in _date_range(window_start, window_end):
        day_periods = [(max(on, day_start), min(off, day_end))
                       for on, off in periods if on < day_end and off > day_start]
        if not day_periods:
            continue
        first_on = min(on for on, _ in day_periods)
        last_off = max(off for _, off in day_periods)
        span_hours = (last_off - first_on).total_seconds() / 3600
        if span_hours > DUTY_DAY_LIMIT_HOURS:
            findings.append(Finding("worktime.duty_day_exceeded", BLOCKING, driver_id,
                                    f"{day_start.date()} 单日出勤 {span_hours:.1f} 小时，"
                                    f"超过 {DUTY_DAY_LIMIT_HOURS} 小时上限",
                                    detail={"date": day_start.strftime("%Y-%m-%d"),
                                            "span_hours": round(span_hours, 2),
                                            "first_on": fmt(first_on), "last_off": fmt(last_off)}))
        driving_seconds = 0.0
        for drive_start, drive_end in driving_windows:
            clipped_start = max(drive_start, day_start)
            clipped_end = min(drive_end, day_end)
            if clipped_end > clipped_start:
                driving_seconds += (clipped_end - clipped_start).total_seconds()
        driving_hours = driving_seconds / 3600
        if driving_hours > DRIVING_DAILY_LIMIT_HOURS:
            findings.append(Finding("worktime.daily_driving_exceeded", BLOCKING, driver_id,
                                    f"{day_start.date()} 累计驾驶 {driving_hours:.1f} 小时，"
                                    f"超过 {DRIVING_DAILY_LIMIT_HOURS} 小时上限",
                                    detail={"date": day_start.strftime("%Y-%m-%d"),
                                            "driving_hours": round(driving_hours, 2)}))

    ordered = sorted(driving_windows, key=lambda item: item[0])
    accumulated = timedelta(0)
    previous_end: datetime | None = None
    for drive_start, drive_end in ordered:
        if previous_end is not None:
            gap = drive_start - previous_end
            if gap >= timedelta(minutes=REST_MIN_MINUTES):
                accumulated = timedelta(0)
            else:
                # 跨日零点不构成法定休息：短间隔后继续累计
                pass
        accumulated += drive_end - drive_start
        if accumulated > timedelta(hours=CONTINUOUS_DRIVING_LIMIT_HOURS):
            findings.append(Finding("worktime.continuous_driving_exceeded", BLOCKING, driver_id,
                                    f"截至 {fmt(drive_end)} 连续驾驶超过 "
                                    f"{CONTINUOUS_DRIVING_LIMIT_HOURS} 小时且无满 "
                                    f"{REST_MIN_MINUTES} 分钟休息",
                                    segment_id=next(
                                        (s["segment_id"] for s in segments
                                         if parse_dt(s["arrive_at"], "segment.arrive_at") == drive_end),
                                        None),
                                    detail={"continuous_hours":
                                            round(accumulated.total_seconds() / 3600, 2)}))
            accumulated = timedelta(0)
        previous_end = drive_end
    return findings


def pax_within_tolerance(old_count: int, new_count: int) -> bool:
    """人数变化是否在可直接沿用原批准的容忍区间。"""

    if old_count <= 0:
        return False
    return abs(new_count - old_count) <= max(1, int(old_count * PAX_CARRY_TOLERANCE))
