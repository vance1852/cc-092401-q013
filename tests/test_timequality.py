from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from robot_trials.contracts import ValidationError, parse_observed_at
from robot_trials.timequality import (
    LATE_PENDING,
    NORMAL,
    REJECTED,
    REASON_AFTER_SEAL_WITHIN_GRACE,
    REASON_BEFORE_BATCH_START,
    REASON_BEYOND_LATE_GRACE,
    REASON_FUTURE_TIMESTAMP,
    classify_observation,
)


UTC = timezone.utc
# 固定参考时刻：2026-09-21T08:00:00Z，全部边界用冻结值构造
NOW = datetime(2026, 9, 21, 8, 0, 0, tzinfo=UTC)
STARTED = datetime(2026, 9, 21, 1, 0, 0, tzinfo=UTC)
SEALED = datetime(2026, 9, 21, 6, 0, 0, tzinfo=UTC)
GRACE = 3600
TOLERANCE = 300


def classify(observed: datetime, *, sealed: datetime | None = SEALED) -> tuple[str, str | None]:
    return classify_observation(
        observed,
        started_at=STARTED,
        sealed_at=sealed,
        now=NOW,
        late_grace_seconds=GRACE,
        future_tolerance_seconds=TOLERANCE,
    )


class ParseObservedAtTests(unittest.TestCase):
    def test_offset_timestamp_parses(self) -> None:
        parsed = parse_observed_at("2026-09-21T09:00:00+08:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))

    def test_zulu_timestamp_parses(self) -> None:
        parsed = parse_observed_at("2026-09-21T01:00:00Z")
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_naive_timestamp_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "时区"):
            parse_observed_at("2026-09-21T09:00:00")

    def test_date_only_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "时区"):
            parse_observed_at("2026-09-21")

    def test_unparseable_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "ISO 8601"):
            parse_observed_at("21/09/2026 09:00 +08:00")

    def test_invalid_calendar_value_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "ISO 8601"):
            parse_observed_at("2026-13-01T00:00:00Z")

    def test_non_string_rejected(self) -> None:
        for value in (None, 1727000000, 17.5, True, ["2026-09-21T09:00:00Z"]):
            with self.assertRaises(ValidationError, msg=repr(value)):
                parse_observed_at(value)

    def test_empty_string_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_observed_at("   ")


class NormalizeTests(unittest.TestCase):
    def test_same_instant_across_zones_normalizes_identically(self) -> None:
        from robot_trials.clock import isoformat

        instants = [
            "2026-09-21T09:00:00+08:00",
            "2026-09-21T03:00:00+02:00",
            "2026-09-20T20:00:00-05:00",
            "2026-09-21T01:00:00Z",
        ]
        normalized = {isoformat(parse_observed_at(text)) for text in instants}
        self.assertEqual(normalized, {"2026-09-21T01:00:00Z"})

    def test_fractional_seconds_preserved(self) -> None:
        from robot_trials.clock import isoformat

        parsed = parse_observed_at("2026-09-21T09:00:00.123456+08:00")
        self.assertEqual(isoformat(parsed), "2026-09-21T01:00:00.123456Z")

    def test_half_hour_zone_normalized(self) -> None:
        from robot_trials.clock import isoformat

        parsed = parse_observed_at("2026-09-21T06:30:00+05:30")
        self.assertEqual(isoformat(parsed), "2026-09-21T01:00:00Z")


class DstTests(unittest.TestCase):
    """夏令时切换前后，同一地区同一挂钟时间对应不同 UTC 时刻。"""

    def test_spring_forward_offsets_normalize_to_distinct_instants(self) -> None:
        from robot_trials.clock import isoformat

        before = isoformat(parse_observed_at("2026-03-08T01:59:59-05:00"))
        after = isoformat(parse_observed_at("2026-03-08T03:00:00-04:00"))
        self.assertEqual(before, "2026-03-08T06:59:59Z")
        self.assertEqual(after, "2026-03-08T07:00:00Z")

    def test_fall_back_same_wall_clock_classified_by_absolute_time(self) -> None:
        # 2026-11-01 美国东部回拨：01:30 出现两次，EDT(-04:00) 与 EST(-05:00) 相差一小时
        edt = parse_observed_at("2026-11-01T01:30:00-04:00")  # 05:30Z
        est = parse_observed_at("2026-11-01T01:30:00-05:00")  # 06:30Z
        started = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        kwargs = dict(
            started_at=started, sealed_at=None, now=now,
            late_grace_seconds=GRACE, future_tolerance_seconds=TOLERANCE,
        )
        self.assertEqual(
            classify_observation(edt, **kwargs),
            (REJECTED, REASON_BEFORE_BATCH_START),
        )
        self.assertEqual(classify_observation(est, **kwargs), (NORMAL, None))


class ClassifyBoundaryTests(unittest.TestCase):
    def test_exactly_at_start_is_normal(self) -> None:
        self.assertEqual(classify(STARTED), (NORMAL, None))

    def test_one_second_before_start_is_rejected(self) -> None:
        self.assertEqual(
            classify(STARTED - timedelta(seconds=1)),
            (REJECTED, REASON_BEFORE_BATCH_START),
        )

    def test_exactly_at_seal_is_normal(self) -> None:
        self.assertEqual(classify(SEALED), (NORMAL, None))

    def test_one_second_after_seal_is_pending(self) -> None:
        self.assertEqual(
            classify(SEALED + timedelta(seconds=1)),
            (LATE_PENDING, REASON_AFTER_SEAL_WITHIN_GRACE),
        )

    def test_exactly_at_grace_end_is_pending(self) -> None:
        self.assertEqual(
            classify(SEALED + timedelta(seconds=GRACE)),
            (LATE_PENDING, REASON_AFTER_SEAL_WITHIN_GRACE),
        )

    def test_one_second_past_grace_is_rejected(self) -> None:
        self.assertEqual(
            classify(SEALED + timedelta(seconds=GRACE + 1)),
            (REJECTED, REASON_BEYOND_LATE_GRACE),
        )

    def test_exactly_at_future_tolerance_is_normal(self) -> None:
        self.assertEqual(classify(NOW + timedelta(seconds=TOLERANCE), sealed=None), (NORMAL, None))

    def test_one_second_past_future_tolerance_is_rejected(self) -> None:
        self.assertEqual(
            classify(NOW + timedelta(seconds=TOLERANCE + 1), sealed=None),
            (REJECTED, REASON_FUTURE_TIMESTAMP),
        )

    def test_future_check_wins_over_grace_window(self) -> None:
        # 封存后 30 分钟上传，观测却写在 45 分钟后：超出未来容忍，按未来时间拒绝
        observed = SEALED + timedelta(minutes=45)
        now = SEALED + timedelta(minutes=30)
        result = classify_observation(
            observed,
            started_at=STARTED,
            sealed_at=SEALED,
            now=now,
            late_grace_seconds=GRACE * 24,
            future_tolerance_seconds=TOLERANCE,
        )
        self.assertEqual(result, (REJECTED, REASON_FUTURE_TIMESTAMP))

    def test_running_batch_never_produces_pending(self) -> None:
        self.assertEqual(classify(NOW, sealed=None), (NORMAL, None))
        self.assertEqual(classify(STARTED, sealed=None), (NORMAL, None))

    def test_zero_grace_rejects_anything_after_seal(self) -> None:
        result = classify_observation(
            SEALED + timedelta(seconds=1),
            started_at=STARTED,
            sealed_at=SEALED,
            now=NOW,
            late_grace_seconds=0,
            future_tolerance_seconds=TOLERANCE,
        )
        self.assertEqual(result, (REJECTED, REASON_BEYOND_LATE_GRACE))

    def test_equivalent_instants_in_other_zones_classify_the_same(self) -> None:
        # 封存时刻本身用不同时区表示，分类结果必须一致
        for text in ("2026-09-21T14:00:00+08:00", "2026-09-21T01:00:00-05:00", "2026-09-21T06:00:00Z"):
            self.assertEqual(classify(parse_observed_at(text)), (NORMAL, None), msg=text)
        for text in ("2026-09-21T14:00:01+08:00", "2026-09-21T01:00:01-05:00", "2026-09-21T06:00:01Z"):
            self.assertEqual(
                classify(parse_observed_at(text)),
                (LATE_PENDING, REASON_AFTER_SEAL_WITHIN_GRACE),
                msg=text,
            )


if __name__ == "__main__":
    unittest.main()
