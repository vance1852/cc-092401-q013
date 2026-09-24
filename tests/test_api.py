from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.clock import FrozenClock
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")


class TimeQualityApiTests(unittest.TestCase):
    """通过 HTTP 边界驱动迟到分类与裁决流程。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(TrialService(self.connection, self.clock))
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        for user_id, role in (("op", "operator"), ("stat", "statistician"), ("aud", "auditor")):
            self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        self.post("/robots", {"robot_id": "robot-a", "model_name": "A 型", "vendor": "厂商"}, actor="op")
        self.post(
            "/builds",
            {"build_id": "build-a", "robot_id": "robot-a", "version": "1.0", "content_sha256": "b" * 64},
            actor="op",
        )
        self.post("/protocols", self.protocol, actor="stat")

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str | None = None, idempotency_key: str | None = None):
        headers = {}
        if actor:
            headers["X-Actor-Id"] = actor
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self.app.handle("POST", path, headers, json.dumps(payload).encode())

    def observation_row(self, source_row: str, observed_at: str) -> dict:
        return {
            "source_batch": "api-upload",
            "source_row": source_row,
            "robot_id": "robot-a",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "stratum_key": "clear-aisle",
            "observed_at": observed_at,
            "metrics": {"completed": 1, "completion_seconds": "50.0", "interventions": 0},
            "excluded_reason": None,
        }

    def test_batch_config_classification_and_adjudication_over_http(self) -> None:
        created = self.post(
            "/batches",
            {
                "batch_id": "batch-api",
                "protocol_id": "demo-delivery-v1",
                "protocol_version": 1,
                "build_id": "build-a",
                "late_grace_seconds": 3600,
            },
            actor="op",
        )
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["late_grace_seconds"], 3600)
        self.assertEqual(created.body["future_tolerance_seconds"], 300)
        self.post("/batches/batch-api/start", {"expected_revision": 1}, actor="op")
        self.clock.advance(hours=4)
        self.post("/batches/batch-api/seal", {"expected_revision": 2}, actor="stat")
        self.clock.advance(minutes=30)
        imported = self.post(
            "/batches/batch-api/observations",
            {"observations": [self.observation_row("a1", "2026-09-21T04:10:00Z")]},
            actor="op",
            idempotency_key="api-late-1",
        )
        self.assertEqual(imported.status, 200)
        self.assertEqual(imported.body["counts"], {"normal": 0, "late_pending": 1, "rejected": 0})
        replay = self.post(
            "/batches/batch-api/observations",
            {"observations": [self.observation_row("a1", "2026-09-21T04:10:00Z")]},
            actor="op",
            idempotency_key="api-late-1",
        )
        self.assertEqual(replay.body, imported.body)
        observation_id = imported.body["classifications"][0]["observation_id"]
        adjudicated = self.post(
            f"/observations/{observation_id}/adjudication",
            {"action": "include", "reason": "合法补传"},
            actor="stat",
        )
        self.assertEqual(adjudicated.status, 201)
        self.assertEqual(adjudicated.body["status"], "included")
        denied = self.post(
            f"/observations/{observation_id}/adjudication",
            {"action": "exclude", "reason": "越权"},
            actor="op",
        )
        self.assertEqual(denied.status, 403)
        report = self.app.handle("GET", "/batches/batch-api/report", {"X-Actor-Id": "aud"})
        self.assertEqual(report.body["time_quality"]["summary"]["adjudicated_included"], 1)

    def test_hard_invalid_time_returns_422_and_rolls_back(self) -> None:
        self.post(
            "/batches",
            {"batch_id": "batch-api", "protocol_id": "demo-delivery-v1", "protocol_version": 1, "build_id": "build-a"},
            actor="op",
        )
        self.post("/batches/batch-api/start", {"expected_revision": 1}, actor="op")
        self.clock.advance(hours=4)
        response = self.post(
            "/batches/batch-api/observations",
            {"observations": [
                self.observation_row("h1", "2026-09-21T03:00:00Z"),
                self.observation_row("h2", "2026-09-21T03:05:00"),
            ]},
            actor="op",
            idempotency_key="api-hard-1",
        )
        self.assertEqual(response.status, 422)
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
