from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService
from robot_trials.timequality import (
    REASON_BEFORE_START,
    REASON_BEYOND_GRACE,
    REASON_FUTURE,
    REASON_LATE_WITHIN_GRACE,
    TIME_STATUS_NORMAL,
    TIME_STATUS_PENDING,
    TIME_STATUS_REJECTED,
    ObservationTimeError,
    classify_observation_time,
    normalize_observed_at,
    parse_observed_at,
)


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class ParseTests(unittest.TestCase):
    """解析与规范化：不同时区、夏令时偏移和非法输入。"""

    def test_offset_is_normalized_to_utc(self) -> None:
        self.assertEqual(normalize_observed_at("2026-09-21T09:00:00+08:00"), "2026-09-21T01:00:00Z")

    def test_z_suffix_is_accepted(self) -> None:
        self.assertEqual(normalize_observed_at("2026-09-21T01:00:00Z"), "2026-09-21T01:00:00Z")

    def test_same_instant_in_different_offsets_normalizes_identically(self) -> None:
        forms = [
            "2026-09-21T09:00:00+08:00",
            "2026-09-21T01:00:00Z",
            "2026-09-20T20:00:00-05:00",
            "2026-09-21T03:00:00+02:00",
        ]
        normalized = {normalize_observed_at(form) for form in forms}
        self.assertEqual(normalized, {"2026-09-21T01:00:00Z"})

    def test_fractional_seconds_are_preserved(self) -> None:
        self.assertEqual(
            normalize_observed_at("2026-09-21T09:00:00.5+08:00"), "2026-09-21T01:00:00.500000Z"
        )

    def test_dst_offsets_shift_utc_for_same_wall_clock(self) -> None:
        # 美国东部夏令时 UTC-4 与冬令时 UTC-5：同一墙钟时刻对应不同 UTC 瞬间
        summer = parse_observed_at("2026-07-01T12:00:00-04:00")
        winter = parse_observed_at("2026-01-01T12:00:00-05:00")
        self.assertEqual(summer, datetime(2026, 7, 1, 16, 0, tzinfo=UTC))
        self.assertEqual(winter, datetime(2026, 1, 1, 17, 0, tzinfo=UTC))

    def test_explicit_offset_disambiguates_spring_forward_gap(self) -> None:
        # 2026-03-08 02:30 在东部本地不存在，但显式偏移使瞬间唯一
        parsed = parse_observed_at("2026-03-08T02:30:00-05:00")
        self.assertEqual(parsed, datetime(2026, 3, 8, 7, 30, tzinfo=UTC))

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ObservationTimeError, "时区"):
            parse_observed_at("2026-09-21T09:00:00")

    def test_date_only_is_rejected(self) -> None:
        with self.assertRaisesRegex(ObservationTimeError, "时区"):
            parse_observed_at("2026-09-21")

    def test_unparseable_is_rejected(self) -> None:
        with self.assertRaisesRegex(ObservationTimeError, "ISO 8601"):
            parse_observed_at("09/21/2026 09:00 AM")

    def test_non_string_is_rejected(self) -> None:
        for value in (None, 20260921, 12.5, True, ["2026-09-21T09:00:00Z"]):
            with self.assertRaises(ObservationTimeError):
                parse_observed_at(value)

    def test_blank_is_rejected(self) -> None:
        with self.assertRaises(ObservationTimeError):
            parse_observed_at("   ")


class ClassifyTests(unittest.TestCase):
    """分类边界：批次开始、封存、宽限终点和未来时刻的恰好命中。"""

    _UNSET = object()

    def setUp(self) -> None:
        self.started = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
        self.sealed = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
        self.grace = timedelta(hours=6)
        self.now = datetime(2026, 9, 22, 3, 0, tzinfo=UTC)

    def classify(self, observed: datetime, *, sealed=_UNSET, now=None, tolerance=None) -> tuple[str, str | None]:
        return classify_observation_time(
            observed,
            started_at=self.started,
            sealed_at=self.sealed if sealed is self._UNSET else sealed,
            grace=self.grace,
            now=self.now if now is None else now,
            future_tolerance=timedelta(0) if tolerance is None else tolerance,
        )

    def test_exactly_at_batch_start_is_normal(self) -> None:
        self.assertEqual(self.classify(self.started), (TIME_STATUS_NORMAL, None))

    def test_one_second_before_start_is_rejected(self) -> None:
        self.assertEqual(
            self.classify(self.started - timedelta(seconds=1)),
            (TIME_STATUS_REJECTED, REASON_BEFORE_START),
        )

    def test_exactly_at_seal_is_normal(self) -> None:
        self.assertEqual(self.classify(self.sealed), (TIME_STATUS_NORMAL, None))

    def test_one_second_after_seal_is_pending(self) -> None:
        self.assertEqual(
            self.classify(self.sealed + timedelta(seconds=1)),
            (TIME_STATUS_PENDING, REASON_LATE_WITHIN_GRACE),
        )

    def test_exactly_at_grace_deadline_is_pending(self) -> None:
        self.assertEqual(
            self.classify(self.sealed + self.grace, now=self.sealed + self.grace),
            (TIME_STATUS_PENDING, REASON_LATE_WITHIN_GRACE),
        )

    def test_one_second_beyond_grace_is_rejected(self) -> None:
        self.assertEqual(
            self.classify(
                self.sealed + self.grace + timedelta(seconds=1),
                now=self.sealed + self.grace + timedelta(seconds=1),
            ),
            (TIME_STATUS_REJECTED, REASON_BEYOND_GRACE),
        )

    def test_running_batch_accepts_up_to_now(self) -> None:
        self.assertEqual(self.classify(self.now, sealed=None), (TIME_STATUS_NORMAL, None))

    def test_future_beyond_tolerance_is_rejected_in_running_batch(self) -> None:
        self.assertEqual(
            self.classify(self.now + timedelta(seconds=1), sealed=None),
            (TIME_STATUS_REJECTED, REASON_FUTURE),
        )

    def test_exactly_at_future_tolerance_edge_is_accepted(self) -> None:
        self.assertEqual(
            self.classify(self.now + timedelta(minutes=5), sealed=None, tolerance=timedelta(minutes=5)),
            (TIME_STATUS_NORMAL, None),
        )

    def test_future_takes_precedence_over_grace_window(self) -> None:
        # 封存后 1 小时（在宽限内）但晚于当前时刻：按未来时间拒绝而非待裁决
        self.assertEqual(
            self.classify(self.now + timedelta(hours=1)),
            (TIME_STATUS_REJECTED, REASON_FUTURE),
        )

    def test_classification_is_offset_invariant(self) -> None:
        instant = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
        expected = self.classify(instant)
        for form in ("2026-09-21T20:00:00+08:00", "2026-09-21T07:00:00-05:00", "2026-09-21T12:00:00Z"):
            self.assertEqual(self.classify(parse_observed_at(form)), expected)


class LateFlowTests(unittest.TestCase):
    """服务层：迟到数据隔离、裁决、幂等重放与原子回滚。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 21, 0, 0, tzinfo=UTC))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("stat-2", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.service.publish_protocol("stat", self.protocol)
        # 宽限 6 小时：封存于 03:00Z 时补传窗口到 09:00Z
        self.service.create_batch(
            "operator", "batch-a", "demo-delivery-v1", 1, "build-a", late_grace_seconds=21600
        )
        self.service.start_batch("operator", "batch-a", 1)
        self.clock.advance(hours=3)
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)

    def tearDown(self) -> None:
        self.connection.close()

    def _row(self, source_row: str, observed_at: str, stratum: str = "cross-traffic") -> dict:
        return {
            "source_batch": "hall-b-20260921",
            "source_row": source_row,
            "robot_id": "robot-a",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "stratum_key": stratum,
            "observed_at": observed_at,
            "metrics": {"completed": 1, "completion_seconds": "55.0", "interventions": 1},
            "excluded_reason": None,
        }

    def _observation_count(self) -> int:
        return self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]

    def _seal(self) -> None:
        self.service.seal_batch("stat", "batch-a", 2)

    def test_normal_import_marks_all_rows_normal(self) -> None:
        rows = self.connection.execute(
            "SELECT DISTINCT time_status FROM observations"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["normal"])

    def test_naive_timestamp_rolls_back_whole_import(self) -> None:
        bad = [self._row("201", "2026-09-21T02:30:00Z"), self._row("202", "2026-09-21T02:40:00")]
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("operator", "batch-a", "key-bad", bad)
        self.assertEqual(self._observation_count(), 6)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM idempotency_keys WHERE key='key-bad'"
            ).fetchone()
        )

    def test_unparseable_timestamp_rolls_back_whole_import(self) -> None:
        bad = [self._row("203", "不是时间")]
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("operator", "batch-a", "key-bad-2", bad)
        self.assertEqual(self._observation_count(), 6)

    def test_future_timestamp_is_rejected_and_stored(self) -> None:
        response = self.service.import_observations(
            "operator", "batch-a", "key-future", [self._row("204", "2026-09-21T04:30:00Z")]
        )
        self.assertEqual(response["time_status_counts"]["rejected"], 1)
        self.assertEqual(response["records"][0]["time_status_reason"], REASON_FUTURE)
        stored = self.connection.execute(
            "SELECT time_status,observed_at_raw FROM observations WHERE source_row='204'"
        ).fetchone()
        self.assertEqual(stored["time_status"], "rejected")
        self.assertEqual(stored["observed_at_raw"], "2026-09-21T04:30:00Z")

    def test_before_batch_start_is_rejected(self) -> None:
        response = self.service.import_observations(
            "operator", "batch-a", "key-early", [self._row("205", "2026-09-20T23:00:00Z")]
        )
        self.assertEqual(response["records"][0]["time_status"], "rejected")
        self.assertEqual(response["records"][0]["time_status_reason"], REASON_BEFORE_START)

    def test_late_upload_after_seal_is_classified_and_committed_atomically(self) -> None:
        self._seal()
        self.clock.advance(hours=2)  # 05:00Z，宽限到 09:00Z
        late = [
            self._row("206", "2026-09-21T02:30:00Z"),  # 封存前观测：合法补传
            self._row("207", "2026-09-21T04:00:00Z"),  # 封存后观测：待裁决
        ]
        response = self.service.import_observations("operator", "batch-a", "key-late", late)
        self.assertEqual(response["inserted"], 2)
        self.assertEqual(
            response["time_status_counts"], {"normal": 1, "pending_review": 1, "rejected": 0}
        )
        self.assertEqual(self._observation_count(), 8)
        statuses = {
            row["source_row"]: row["time_status"]
            for row in self.connection.execute(
                "SELECT source_row,time_status FROM observations WHERE source_row IN ('206','207')"
            ).fetchall()
        }
        self.assertEqual(statuses, {"206": "normal", "207": "pending_review"})

    def test_import_with_only_quarantinable_late_items_commits(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        late = [self._row("208", "2026-09-21T04:00:00Z"), self._row("209", "2026-09-21T05:00:00Z")]
        response = self.service.import_observations("operator", "batch-a", "key-only-late", late)
        self.assertEqual(
            response["time_status_counts"], {"normal": 0, "pending_review": 2, "rejected": 0}
        )
        self.assertEqual(self._observation_count(), 8)
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["time_quality"]["counts"]["pending_review"], 2)
        self.assertEqual(
            {record["effective_status"] for record in report["time_quality"]["records"]},
            {"pending"},
        )

    def test_hard_invalid_mixed_with_late_rolls_back_everything(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        mixed = [self._row("210", "2026-09-21T04:00:00Z"), self._row("211", "2026-09-21 04:00")]
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("operator", "batch-a", "key-mixed", mixed)
        self.assertEqual(self._observation_count(), 6)

    def test_idempotent_replay_returns_same_classification(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        late = [self._row("212", "2026-09-21T04:00:00Z")]
        first = self.service.import_observations("operator", "batch-a", "key-replay", late)
        second = self.service.import_observations("operator", "batch-a", "key-replay", late)
        self.assertEqual(first, second)
        # 宽限期过后重放仍返回已存分类，而不是重新判定
        self.clock.advance(hours=10)
        third = self.service.import_observations("operator", "batch-a", "key-replay", late)
        self.assertEqual(first, third)
        self.assertEqual(self._observation_count(), 7)

    def test_import_window_closes_exactly_after_grace_deadline(self) -> None:
        self._seal()  # 封存于 03:00Z，宽限 6 小时到 09:00Z
        self.clock.advance(hours=6)  # 恰好 09:00Z
        row = self._row("213", "2026-09-21T08:00:00Z")
        response = self.service.import_observations("operator", "batch-a", "key-edge", [row])
        self.assertEqual(response["records"][0]["time_status"], "pending_review")
        self.clock.advance(seconds=1)  # 09:00:01Z，窗口关闭
        with self.assertRaises(InvalidState):
            self.service.import_observations(
                "operator", "batch-a", "key-closed", [self._row("214", "2026-09-21T08:30:00Z")]
            )

    def test_observed_exactly_at_seal_and_grace_boundaries(self) -> None:
        self._seal()  # 封存于 03:00Z
        self.clock.advance(hours=6)  # 09:00Z
        rows = [
            self._row("215", "2026-09-21T03:00:00Z"),  # 恰好封存时刻 → 正常
            self._row("216", "2026-09-21T09:00:00Z"),  # 恰好宽限终点 → 待裁决
        ]
        response = self.service.import_observations("operator", "batch-a", "key-boundary", rows)
        statuses = {record["source_row"]: record["time_status"] for record in response["records"]}
        self.assertEqual(statuses, {"215": "normal", "216": "pending_review"})

    def test_pending_record_blocks_analysis_until_adjudicated(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-pending", [self._row("217", "2026-09-21T04:00:00Z")]
        )
        job = self.service.claim_job("worker", 60)
        with self.assertRaisesRegex(InvalidState, "待裁决"):
            self.service.complete_job("worker", job["job_id"], "stat")
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='217'"
        ).fetchone()[0]
        self.service.adjudicate_late_observation("stat", observation_id, "excluded", "迟到且无法核实")
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.assertEqual(analysis["result"]["included_count"], 6)

    def test_included_adjudication_enters_analysis(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-include", [self._row("218", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='218'"
        ).fetchone()[0]
        self.service.adjudicate_late_observation("stat", observation_id, "included", "补传凭证齐全")
        job = self.service.claim_job("worker", 60)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.assertEqual(analysis["result"]["included_count"], 7)

    def test_rejected_records_never_enter_analysis(self) -> None:
        self.service.import_observations(
            "operator", "batch-a", "key-rejected", [self._row("219", "2026-09-21T04:30:00Z")]
        )
        self._seal()
        job = self.service.claim_job("worker", 60)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.assertEqual(analysis["result"]["included_count"], 6)

    def test_adjudication_requires_privileged_role(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-role", [self._row("220", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='220'"
        ).fetchone()[0]
        with self.assertRaises(Forbidden):
            self.service.adjudicate_late_observation("operator", observation_id, "included", "越权")
        with self.assertRaises(Forbidden):
            self.service.adjudicate_late_observation("approver", observation_id, "included", "越权")

    def test_importer_cannot_adjudicate_own_late_record(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-self", [self._row("221", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='221'"
        ).fetchone()[0]
        # 白盒构造：导入人临时获得统计负责人角色，独立性校验仍应拦截
        self.connection.execute("UPDATE users SET role='statistician' WHERE user_id='operator'")
        with self.assertRaisesRegex(Forbidden, "导入人"):
            self.service.adjudicate_late_observation("operator", observation_id, "included", "自审")
        # 独立统计负责人可以裁决
        result = self.service.adjudicate_late_observation("stat", observation_id, "included", "独立复核通过")
        self.assertEqual(result["action"], "included")

    def test_adjudication_requires_reason_and_valid_action(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-reason", [self._row("222", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='222'"
        ).fetchone()[0]
        with self.assertRaises(ValidationFailed):
            self.service.adjudicate_late_observation("stat", observation_id, "included", "  ")
        with self.assertRaises(ValidationFailed):
            self.service.adjudicate_late_observation("stat", observation_id, "maybe", "无效动作")

    def test_adjudication_only_for_pending_records(self) -> None:
        normal_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='001'"
        ).fetchone()[0]
        with self.assertRaises(InvalidState):
            self.service.adjudicate_late_observation("stat", normal_id, "excluded", "非迟到记录")

    def test_adjudication_history_is_append_only_and_latest_wins(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-history", [self._row("223", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='223'"
        ).fetchone()[0]
        self.service.adjudicate_late_observation("stat", observation_id, "included", "先纳入")
        self.service.adjudicate_late_observation("stat-2", observation_id, "excluded", "复核后排除")
        rows = self.connection.execute(
            "SELECT action,decided_by FROM late_adjudications WHERE observation_id=? "
            "ORDER BY adjudication_id",
            (observation_id,),
        ).fetchall()
        self.assertEqual([(row["action"], row["decided_by"]) for row in rows],
                         [("included", "stat"), ("excluded", "stat-2")])
        report = self.service.report("auditor", "batch-a")
        record = report["time_quality"]["records"][0]
        self.assertEqual(record["effective_status"], "excluded")
        self.assertEqual([item["action"] for item in record["adjudications"]], ["included", "excluded"])
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='observation' AND entity_id=? "
            "ORDER BY event_id",
            (str(observation_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events],
                         ["late_adjudication.recorded", "late_adjudication.recorded"])

    def test_adjudication_locked_after_analysis(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-lock", [self._row("224", "2026-09-21T04:00:00Z")]
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE source_row='224'"
        ).fetchone()[0]
        self.service.adjudicate_late_observation("stat", observation_id, "excluded", "迟到超窗")
        job = self.service.claim_job("worker", 60)
        self.service.complete_job("worker", job["job_id"], "stat")
        with self.assertRaises(InvalidState):
            self.service.adjudicate_late_observation("stat", observation_id, "included", "事后改判")

    def test_report_preserves_raw_and_normalized_time(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-report", [self._row("225", "2026-09-21T12:00:00+08:00")]
        )
        report = self.service.report("auditor", "batch-a")
        record = report["time_quality"]["records"][0]
        self.assertEqual(record["observed_at_raw"], "2026-09-21T12:00:00+08:00")
        self.assertEqual(record["observed_at"], "2026-09-21T04:00:00Z")
        self.assertEqual(record["time_status"], "pending_review")
        self.assertEqual(report["time_quality"]["late_grace_seconds"], 21600)
        stored = self.connection.execute(
            "SELECT raw_json FROM observations WHERE source_row='225'"
        ).fetchone()
        self.assertEqual(json.loads(stored["raw_json"])["observed_at"], "2026-09-21T12:00:00+08:00")

    def test_same_instant_from_different_regions_classifies_identically(self) -> None:
        self._seal()
        self.clock.advance(hours=2)  # 05:00Z
        rows = [
            self._row("226", "2026-09-21T12:00:00+08:00"),  # 04:00Z
            self._row("227", "2026-09-21T04:00:00Z"),
            self._row("228", "2026-09-20T23:00:00-05:00"),
        ]
        response = self.service.import_observations("operator", "batch-a", "key-tz", rows)
        self.assertEqual(
            [record["time_status"] for record in response["records"]],
            ["pending_review", "pending_review", "pending_review"],
        )
        self.assertEqual(
            {record["observed_at"] for record in response["records"]},
            {"2026-09-21T04:00:00Z"},
        )

    def test_replay_conflict_still_detected(self) -> None:
        self._seal()
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-a", "key-conflict", [self._row("229", "2026-09-21T04:00:00Z")]
        )
        with self.assertRaises(Conflict):
            self.service.import_observations(
                "operator", "batch-a", "key-conflict", [self._row("230", "2026-09-21T04:00:00Z")]
            )


if __name__ == "__main__":
    unittest.main()
