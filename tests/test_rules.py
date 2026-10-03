import unittest

from transport_coordination.errors import ValidationError
from transport_coordination.rules import (
    qualification_findings,
    worktime_findings,
    parse_dt,
    pax_within_tolerance,
    BLOCKING,
)


def dt(text):
    return parse_dt(text, "x")


class QualificationRulesTest(unittest.TestCase):
    def setUp(self):
        self.window = (dt("2026-10-01T08:00:00"), dt("2026-10-02T18:00:00"))

    def test_license_expiring_mid_trip_blocks(self):
        findings = qualification_findings(
            "vehicle", "V1",
            [{"type": "vehicle_license",
              "valid_from": "2026-01-01T00:00:00",
              "valid_to": "2026-10-02T12:00:00"}],
            *self.window, required_types=("vehicle_license",))
        self.assertEqual(["license.covers_partial"], [f.code for f in findings])
        self.assertEqual(BLOCKING, findings[0].severity)

    def test_license_not_yet_effective_at_departure_blocks(self):
        findings = qualification_findings(
            "vehicle", "V1",
            [{"type": "vehicle_license",
              "valid_from": "2026-10-01T10:00:00",
              "valid_to": "2027-01-01T00:00:00"}],
            *self.window, required_types=("vehicle_license",))
        self.assertEqual(["license.not_yet_effective"], [f.code for f in findings])

    def test_missing_required_license_blocks(self):
        findings = qualification_findings("driver", "D1", [], *self.window,
                                          required_types=("driver_license",))
        self.assertEqual(["license.required_missing"], [f.code for f in findings])

    def test_license_covering_full_window_passes(self):
        findings = qualification_findings(
            "vehicle", "V1",
            [{"type": "vehicle_license",
              "valid_from": "2026-09-01T00:00:00",
              "valid_to": "2027-01-01T00:00:00"}],
            *self.window, required_types=("vehicle_license",))
        self.assertEqual([], findings)

    def test_invalid_datetime_text(self):
        with self.assertRaises(ValidationError):
            parse_dt("10月1日", "field")


class WorktimeRulesTest(unittest.TestCase):
    def _driver(self, periods, rests=None):
        return {"driver_id": "D1", "duty_periods": periods, "rest_periods": rests or []}

    def _segments(self, windows):
        return [{"segment_id": f"s{i}", "driver_id": "D1",
                 "departure_at": start, "arrive_at": end}
                for i, (start, end) in enumerate(windows)]

    def test_cross_day_long_duty_is_flagged(self):
        # 跨日连续值乘 14 小时
        driver = self._driver([
            {"on": "2026-10-01T06:00:00", "off": "2026-10-01T20:00:00"}])
        findings = worktime_findings(driver, self._segments([
            ("2026-10-01T08:00:00", "2026-10-01T18:00:00")]))
        self.assertIn("worktime.duty_day_exceeded", [f.code for f in findings])

    def test_two_day_trip_within_limits_passes(self):
        driver = self._driver([
            {"on": "2026-10-01T07:00:00", "off": "2026-10-01T19:00:00"},
            {"on": "2026-10-02T07:00:00", "off": "2026-10-02T18:00:00"}],
            rests=[
                {"start": "2026-10-01T12:00:00", "end": "2026-10-01T12:30:00"},
                {"start": "2026-10-02T12:00:00", "end": "2026-10-02T12:30:00"}])
        findings = worktime_findings(driver, self._segments([
            ("2026-10-01T08:00:00", "2026-10-01T16:00:00"),
            ("2026-10-02T08:00:00", "2026-10-02T16:00:00")]))
        self.assertEqual([], findings)

    def test_daily_driving_limit_enforced_per_calendar_day(self):
        # 每日驾驶 7 小时合规；换成 9 小时则阻断
        driver = self._driver([
            {"on": "2026-10-01T06:00:00", "off": "2026-10-01T20:00:00"}],
            rests=[{"start": "2026-10-01T12:00:00", "end": "2026-10-01T13:00:00"}])
        ok = worktime_findings(driver, self._segments([
            ("2026-10-01T07:00:00", "2026-10-01T11:00:00"),
            ("2026-10-01T13:00:00", "2026-10-01T16:00:00")]))
        self.assertNotIn("worktime.daily_driving_exceeded", [f.code for f in ok])
        bad = worktime_findings(driver, self._segments([
            ("2026-10-01T07:00:00", "2026-10-01T12:00:00"),
            ("2026-10-01T13:00:00", "2026-10-01T17:00:00")]))
        self.assertIn("worktime.daily_driving_exceeded", [f.code for f in bad])

    def test_continuous_driving_requires_twenty_minute_rest(self):
        driver = self._driver([
            {"on": "2026-10-01T06:00:00", "off": "2026-10-01T20:00:00"}])
        # 无休息连续 5 小时
        without_rest = worktime_findings(driver, self._segments([
            ("2026-10-01T08:00:00", "2026-10-01T13:00:00")]))
        self.assertIn("worktime.continuous_driving_exceeded",
                      [f.code for f in without_rest])
        # 仅休息 15 分钟不能切断
        short_rest = worktime_findings(
            {**driver, "rest_periods": [
                {"start": "2026-10-01T10:00:00", "end": "2026-10-01T10:15:00"}]},
            self._segments([("2026-10-01T08:00:00", "2026-10-01T13:00:00")]))
        self.assertIn("worktime.continuous_driving_exceeded",
                      [f.code for f in short_rest])
        # 满 20 分钟休息后两段均不超过 4 小时
        enough_rest = worktime_findings(
            {**driver, "rest_periods": [
                {"start": "2026-10-01T10:30:00", "end": "2026-10-01T11:00:00"}]},
            self._segments([("2026-10-01T08:00:00", "2026-10-01T13:00:00")]))
        self.assertNotIn("worktime.continuous_driving_exceeded",
                         [f.code for f in enough_rest])

    def test_overnight_gap_does_not_count_as_rest_when_duty_spans_midnight(self):
        # 23:00 至次日 03:30 连续驾驶跨过零点，不应因跨日而自动清零
        driver = self._driver([
            {"on": "2026-10-01T20:00:00", "off": "2026-10-02T05:00:00"}])
        findings = worktime_findings(driver, self._segments([
            ("2026-10-01T23:00:00", "2026-10-02T03:30:00")]))
        self.assertIn("worktime.continuous_driving_exceeded", [f.code for f in findings])


class PassengerToleranceTest(unittest.TestCase):
    def test_within_ten_percent_carries(self):
        self.assertTrue(pax_within_tolerance(40, 42))
        self.assertTrue(pax_within_tolerance(40, 38))

    def test_beyond_tolerance_requires_rereview(self):
        self.assertFalse(pax_within_tolerance(40, 50))
        self.assertFalse(pax_within_tolerance(40, 30))


if __name__ == "__main__":
    unittest.main()
