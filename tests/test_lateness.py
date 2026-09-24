from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class LatenessServiceTests(unittest.TestCase):
    """迟到、未来与硬性无效观测时间的端到端数据质量流程。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        # 批次 2026-09-21 00:00Z 开始；夹具观测在 01:00Z-02:20Z，导入前推进到 04:00Z
        self.clock = FrozenClock(datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.base_rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.service.publish_protocol("stat", self.protocol)
        self.service.create_batch("operator", "batch-a", "demo-delivery-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        self.clock.advance(hours=4)  # 现在 2026-09-21T04:00:00Z
        imported = self.service.import_observations("operator", "batch-a", "base", self.base_rows)
        self.assertEqual(imported["counts"], {"normal": 6, "late_pending": 0, "rejected": 0})
        self.service.seal_batch("stat", "batch-a", 2)  # 封存于 04:00:00Z

    def tearDown(self) -> None:
        self.connection.close()

    # ---------- 辅助 ----------

    def make_row(self, source_row: str, observed_at: str, **overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "source_batch": "late-upload",
            "source_row": source_row,
            "robot_id": "robot-a",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "stratum_key": "clear-aisle",
            "observed_at": observed_at,
            "metrics": {"completed": 1, "completion_seconds": "50.0", "interventions": 0},
            "excluded_reason": None,
        }
        row.update(overrides)
        return row

    def observation_count(self, batch_id: str = "batch-a") -> int:
        return self.connection.execute(
            "SELECT count(*) FROM observations WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]

    def run_analysis(self, batch_id: str = "batch-a") -> dict[str, object]:
        # 任务队列全局共享：先完成排在前面的其它批次任务，直到拿到目标批次
        while True:
            job = self.service.claim_job("worker", 60)
            self.assertIsNotNone(job)
            result = self.service.complete_job("worker", job["job_id"], "stat")
            if job["batch_id"] == batch_id:
                return result

    def new_batch(self, batch_id: str, **config: int) -> None:
        self.service.create_batch("operator", batch_id, "demo-delivery-v1", 1, "build-a", **config)
        self.service.start_batch("operator", batch_id, 1)

    # ---------- 硬性无效时间：整批回滚 ----------

    def test_naive_timestamp_rolls_back_entire_batch(self) -> None:
        self.new_batch("batch-b")
        rows = [
            self.make_row("n1", "2026-09-21T04:30:00Z", source_batch="nb"),
            self.make_row("n2", "2026-09-21T04:31:00", source_batch="nb"),  # 无时区
        ]
        with self.assertRaisesRegex(ValidationFailed, "时区"):
            self.service.import_observations("operator", "batch-b", "nb-key", rows)
        self.assertEqual(self.observation_count("batch-b"), 0)
        # 修正后同一幂等键可以重新导入
        rows[1]["observed_at"] = "2026-09-21T04:31:00Z"
        result = self.service.import_observations("operator", "batch-b", "nb-key", rows)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(self.observation_count("batch-b"), 2)

    def test_unparseable_timestamp_rolls_back_entire_batch(self) -> None:
        self.new_batch("batch-b")
        rows = [
            self.make_row("u1", "2026-09-21T04:30:00Z", source_batch="ub"),
            self.make_row("u2", "昨天早上", source_batch="ub"),
        ]
        with self.assertRaisesRegex(ValidationFailed, "ISO 8601"):
            self.service.import_observations("operator", "batch-b", "ub-key", rows)
        self.assertEqual(self.observation_count("batch-b"), 0)

    def test_mix_of_valid_late_and_hard_invalid_rolls_back(self) -> None:
        # 封存于 04:00Z；推进到 04:30Z 后导入：一条可隔离迟到 + 一条硬性无效
        self.clock.advance(minutes=30)
        rows = [
            self.make_row("m1", "2026-09-21T04:10:00Z"),
            self.make_row("m2", "2026-09-21T04:20:00"),  # 无时区 -> 硬性无效
        ]
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("operator", "batch-a", "mixed", rows)
        self.assertEqual(self.observation_count(), 6)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0], 1
        )

    # ---------- 迟到分类与原子落库 ----------

    def test_late_within_grace_commits_atomically_and_is_quarantined(self) -> None:
        self.clock.advance(minutes=30)  # 现在 04:30Z，封存于 04:00Z
        rows = [
            self.make_row("L1", "2026-09-21T04:10:00Z"),
            self.make_row("L2", "2026-09-21T04:20:00+00:00"),
        ]
        result = self.service.import_observations("operator", "batch-a", "late-1", rows)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["counts"], {"normal": 0, "late_pending": 2, "rejected": 0})
        self.assertEqual(
            [item["classification"] for item in result["classifications"]],
            ["late_pending", "late_pending"],
        )
        self.assertEqual(self.observation_count(), 8)
        stored = self.connection.execute(
            "SELECT time_classification,classification_reason FROM observations "
            "WHERE batch_id='batch-a' AND source_batch='late-upload' ORDER BY source_row"
        ).fetchall()
        self.assertEqual([row["time_classification"] for row in stored], ["late_pending"] * 2)
        self.assertEqual({row["classification_reason"] for row in stored}, {"after_seal_within_grace"})
        report = self.service.report("auditor", "batch-a")
        summary = report["time_quality"]["summary"]
        self.assertEqual(summary["late_pending"], 2)
        self.assertEqual(summary["awaiting_adjudication"], 2)
        self.assertEqual(summary["normal"], 6)
        self.assertEqual(
            [record["status"] for record in report["time_quality"]["records"]],
            ["pending", "pending"],
        )

    def test_exact_seal_instant_is_normal_and_one_second_later_is_pending(self) -> None:
        self.new_batch("batch-b")
        self.clock.advance(hours=1)
        self.service.seal_batch("stat", "batch-b", 2)  # 封存于 05:00:00Z
        self.clock.advance(hours=2)
        rows = [
            self.make_row("b1", "2026-09-21T05:00:00Z", source_batch="bb"),
            self.make_row("b2", "2026-09-21T05:00:01Z", source_batch="bb"),
        ]
        result = self.service.import_observations("operator", "batch-b", "bb-key", rows)
        self.assertEqual(
            [item["classification"] for item in result["classifications"]],
            ["normal", "late_pending"],
        )

    def test_beyond_grace_is_rejected_but_stored(self) -> None:
        self.new_batch("batch-c", late_grace_seconds=3600)
        self.clock.advance(hours=1)
        self.service.seal_batch("stat", "batch-c", 2)  # 封存于 05:00Z，宽限到 06:00Z
        self.clock.advance(hours=2)  # 现在 07:00Z
        rows = [self.make_row("c1", "2026-09-21T06:30:00Z", source_batch="bc")]
        result = self.service.import_observations("operator", "batch-c", "bc-key", rows)
        self.assertEqual(result["counts"]["rejected"], 1)
        self.assertEqual(result["classifications"][0]["reason"], "beyond_late_grace")
        stored = self.connection.execute(
            "SELECT time_classification,classification_reason FROM observations WHERE batch_id='batch-c'"
        ).fetchone()
        self.assertEqual(stored["time_classification"], "rejected")
        report = self.service.report("auditor", "batch-c")
        record = report["time_quality"]["records"][0]
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["classification_reason"], "beyond_late_grace")

    def test_future_timestamp_beyond_tolerance_is_rejected(self) -> None:
        self.new_batch("batch-d", future_tolerance_seconds=300)  # 现在 04:00Z
        rows = [
            self.make_row("d1", "2026-09-21T04:04:00Z", source_batch="bd"),  # 容忍内
            self.make_row("d2", "2026-09-21T04:10:00Z", source_batch="bd"),  # 超出容忍
        ]
        result = self.service.import_observations("operator", "batch-d", "bd-key", rows)
        self.assertEqual(
            [(item["classification"], item["reason"]) for item in result["classifications"]],
            [("normal", None), ("rejected", "future_timestamp")],
        )

    def test_observation_before_batch_start_is_rejected(self) -> None:
        self.new_batch("batch-e")  # 04:00Z 开始
        rows = [self.make_row("e1", "2026-09-21T03:59:59Z", source_batch="be")]
        result = self.service.import_observations("operator", "batch-e", "be-key", rows)
        self.assertEqual(result["classifications"][0]["classification"], "rejected")
        self.assertEqual(result["classifications"][0]["reason"], "before_batch_start")

    def test_dst_same_wall_clock_classified_by_absolute_time(self) -> None:
        # 2026-11-01 美国东部回拨：01:30 在 EDT 与 EST 各出现一次，相差一小时
        self.clock.current = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
        self.new_batch("batch-dst")  # 06:00Z 开始
        self.clock.advance(hours=6)
        rows = [
            self.make_row("dst1", "2026-11-01T01:30:00-04:00", source_batch="dst"),  # 05:30Z
            self.make_row("dst2", "2026-11-01T01:30:00-05:00", source_batch="dst"),  # 06:30Z
        ]
        result = self.service.import_observations("operator", "batch-dst", "dst-key", rows)
        self.assertEqual(
            [(item["classification"], item["reason"]) for item in result["classifications"]],
            [("rejected", "before_batch_start"), ("normal", None)],
        )
        stored = self.connection.execute(
            "SELECT observed_at FROM observations WHERE batch_id='batch-dst' ORDER BY source_row"
        ).fetchall()
        self.assertEqual(
            [row["observed_at"] for row in stored],
            ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"],
        )

    # ---------- 幂等重放 ----------

    def test_idempotent_replay_returns_same_classification(self) -> None:
        self.clock.advance(minutes=30)
        rows = [self.make_row("L1", "2026-09-21T04:10:00Z")]
        first = self.service.import_observations("operator", "batch-a", "late-1", rows)
        second = self.service.import_observations("operator", "batch-a", "late-1", rows)
        self.assertEqual(first, second)
        # 时钟继续推进后重放，分类仍然不变
        self.clock.advance(hours=48)
        third = self.service.import_observations("operator", "batch-a", "late-1", rows)
        self.assertEqual(first, third)
        self.assertEqual(self.observation_count(), 7)

    def test_replay_with_changed_content_conflicts(self) -> None:
        self.clock.advance(minutes=30)
        rows = [self.make_row("L1", "2026-09-21T04:10:00Z")]
        self.service.import_observations("operator", "batch-a", "late-1", rows)
        changed = [self.make_row("L1", "2026-09-21T04:11:00Z")]
        with self.assertRaises(Conflict):
            self.service.import_observations("operator", "batch-a", "late-1", changed)

    # ---------- 隔离不进入分析 ----------

    def test_pending_records_stay_out_of_analysis(self) -> None:
        self.clock.advance(minutes=30)
        self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        analysis = self.run_analysis()
        self.assertEqual(analysis["result"]["included_count"], 6)
        self.assertEqual(analysis["result"]["excluded_count"], 0)

    def test_rejected_records_stay_out_of_analysis(self) -> None:
        self.new_batch("batch-f", late_grace_seconds=3600)
        self.clock.advance(hours=1)
        self.service.seal_batch("stat", "batch-f", 2)  # 封存于 05:00Z
        self.clock.advance(hours=2)
        self.service.import_observations(
            "operator", "batch-f", "bf-key",
            [self.make_row("f1", "2026-09-21T06:30:00Z", source_batch="bf")],  # 超出宽限
        )
        analysis = self.run_analysis("batch-f")
        self.assertEqual(analysis["result"]["included_count"], 0)
        self.assertEqual(analysis["result"]["conclusion"], "insufficient")

    # ---------- 裁决流程 ----------

    def test_adjudicate_include_before_analysis_runs(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        result = self.service.adjudicate_lateness("stat", observation_id, "include", "现场网络中断，属合法补传")
        self.assertEqual(result["status"], "included")
        self.assertFalse(result["reanalysis_queued"])  # 分析尚未执行，直接随队列纳入
        analysis = self.run_analysis()
        self.assertEqual(analysis["result"]["included_count"], 7)

    def test_adjudicate_include_after_analysis_requeues(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        first = self.run_analysis()
        self.assertEqual(first["result"]["included_count"], 6)
        adjudicated = self.service.adjudicate_lateness("stat", observation_id, "include", "确认补传有效")
        self.assertTrue(adjudicated["reanalysis_queued"])
        self.assertEqual(self.service.get_batch("batch-a")["state"], "sealed")
        second = self.run_analysis()
        self.assertEqual(second["result"]["included_count"], 7)
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        decided = self.service.decide("approver", "batch-a", second["analysis_id"], "approved", "补传已纳入")
        self.assertEqual(decided["decision"], "approved")

    def test_adjudicate_exclude_marks_record_excluded(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        self.run_analysis()
        adjudicated = self.service.adjudicate_lateness("stat", observation_id, "exclude", "无法核实来源")
        self.assertEqual(adjudicated["status"], "excluded")
        analysis = self.run_analysis()
        self.assertEqual(analysis["result"]["included_count"], 6)
        self.assertEqual(analysis["result"]["excluded_count"], 1)

    def test_pending_record_blocks_decision(self) -> None:
        self.clock.advance(minutes=30)
        self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        analysis = self.run_analysis()
        with self.assertRaisesRegex(InvalidState, "待裁决"):
            self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "尝试决定")

    def test_importer_role_cannot_adjudicate(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        with self.assertRaises(Forbidden):
            self.service.adjudicate_lateness("operator", observation_id, "include", "越权")
        with self.assertRaises(Forbidden):
            self.service.adjudicate_lateness("auditor", observation_id, "include", "越权")

    def test_adjudication_requires_reason_and_valid_action(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        with self.assertRaises(ValidationFailed):
            self.service.adjudicate_lateness("stat", observation_id, "include", "  ")
        with self.assertRaises(ValidationFailed):
            self.service.adjudicate_lateness("stat", observation_id, "maybe", "理由")

    def test_adjudication_history_is_append_only_and_latest_wins(self) -> None:
        self.clock.advance(minutes=30)
        imported = self.service.import_observations(
            "operator", "batch-a", "late-1", [self.make_row("L1", "2026-09-21T04:10:00Z")]
        )
        observation_id = imported["classifications"][0]["observation_id"]
        self.service.adjudicate_lateness("stat", observation_id, "include", "初步认可")
        self.service.adjudicate_lateness("stat", observation_id, "exclude", "复核后否定")
        history = self.connection.execute(
            "SELECT action,reason FROM lateness_adjudications WHERE observation_id=? "
            "ORDER BY adjudication_id",
            (observation_id,),
        ).fetchall()
        self.assertEqual(
            [(row["action"], row["reason"]) for row in history],
            [("included", "初步认可"), ("excluded", "复核后否定")],
        )
        report = self.service.report("auditor", "batch-a")
        record = report["time_quality"]["records"][0]
        self.assertEqual(record["status"], "excluded")
        self.assertEqual(len(record["adjudications"]), 2)
        self.assertEqual(report["time_quality"]["summary"]["adjudicated_excluded"], 1)

    def test_only_late_pending_records_can_be_adjudicated(self) -> None:
        normal_id = self.connection.execute(
            "SELECT observation_id FROM observations WHERE time_classification='normal' LIMIT 1"
        ).fetchone()[0]
        with self.assertRaises(InvalidState):
            self.service.adjudicate_lateness("stat", normal_id, "include", "正常记录")

    # ---------- 原始内容不可变 ----------

    def test_original_submission_preserved_with_normalized_value(self) -> None:
        self.clock.advance(minutes=30)
        self.service.import_observations(
            "operator", "batch-a", "late-1",
            [self.make_row("L1", "2026-09-21T12:10:00+08:00")],
        )
        row = self.connection.execute(
            "SELECT observed_at,observed_at_raw,raw_json,time_classification FROM observations "
            "WHERE source_batch='late-upload'"
        ).fetchone()
        self.assertEqual(row["observed_at"], "2026-09-21T04:10:00Z")
        self.assertEqual(row["observed_at_raw"], "2026-09-21T12:10:00+08:00")
        self.assertIn("2026-09-21T12:10:00+08:00", row["raw_json"])
        self.assertEqual(row["time_classification"], "late_pending")

    # ---------- 批次窗口配置 ----------

    def test_batch_window_configuration_validated(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_batch(
                "operator", "bad-1", "demo-delivery-v1", 1, "build-a", late_grace_seconds=-1
            )
        with self.assertRaises(ValidationFailed):
            self.service.create_batch(
                "operator", "bad-2", "demo-delivery-v1", 1, "build-a", future_tolerance_seconds=True
            )
        with self.assertRaises(ValidationFailed):
            self.service.create_batch(
                "operator", "bad-3", "demo-delivery-v1", 1, "build-a", late_grace_seconds="3600"
            )
        batch = self.service.create_batch(
            "operator", "good-1", "demo-delivery-v1", 1, "build-a",
            late_grace_seconds=7200, future_tolerance_seconds=60,
        )
        self.assertEqual(batch["late_grace_seconds"], 7200)
        self.assertEqual(batch["future_tolerance_seconds"], 60)
        default_batch = self.service.create_batch("operator", "good-2", "demo-delivery-v1", 1, "build-a")
        self.assertEqual(default_batch["late_grace_seconds"], 86400)
        self.assertEqual(default_batch["future_tolerance_seconds"], 300)

    def test_import_rejected_after_decision(self) -> None:
        analysis = self.run_analysis()
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "完成")
        with self.assertRaises(InvalidState):
            self.service.import_observations(
                "operator", "batch-a", "too-late", [self.make_row("L9", "2026-09-21T04:10:00Z")]
            )


if __name__ == "__main__":
    unittest.main()
