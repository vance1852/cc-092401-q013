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


class LateAdjudicationApiTests(unittest.TestCase):
    """迟到裁决与批次宽限参数的 HTTP 边界。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        self.service.create_user("operator", "操作员", "operator")
        self.service.create_user("stat", "统计负责人", "statistician")
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.service.publish_protocol("stat", self.protocol)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "operator", idempotency_key: str | None = None):
        headers = {"X-Actor-Id": actor}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self.app.handle("POST", path, headers, json.dumps(payload).encode())

    def test_batch_creation_accepts_grace_override(self) -> None:
        response = self._post("/batches", {
            "batch_id": "batch-g",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "build_id": "build-a",
            "late_grace_seconds": 3600,
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["late_grace_seconds"], 3600)

    def test_batch_creation_defaults_grace(self) -> None:
        response = self._post("/batches", {
            "batch_id": "batch-d",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "build_id": "build-a",
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["late_grace_seconds"], 86400)

    def test_late_adjudication_route(self) -> None:
        self._post("/batches", {
            "batch_id": "batch-l",
            "protocol_id": "demo-delivery-v1",
            "protocol_version": 1,
            "build_id": "build-a",
        })
        self._post("/batches/batch-l/start", {"expected_revision": 1})
        self.clock.advance(hours=3)
        row = {
            "source_batch": "hall-b", "source_row": "1", "robot_id": "robot-a",
            "protocol_id": "demo-delivery-v1", "protocol_version": 1,
            "stratum_key": "clear-aisle", "observed_at": "2026-09-21T02:00:00Z",
            "metrics": {"completed": 1, "completion_seconds": "40", "interventions": 0},
            "excluded_reason": None,
        }
        self._post("/batches/batch-l/observations", {"observations": [row]}, idempotency_key="k1")
        self._post("/batches/batch-l/seal", {"expected_revision": 2}, actor="stat")
        self.clock.advance(hours=2)
        late = dict(row, source_row="2", observed_at="2026-09-21T04:00:00Z")
        imported = self._post("/batches/batch-l/observations", {"observations": [late]}, idempotency_key="k2")
        self.assertEqual(imported.body["records"][0]["time_status"], "pending_review")
        observation_id = imported.body["records"][0]["observation_id"]
        adjudicated = self._post("/late-adjudications", {
            "observation_id": observation_id, "action": "included", "reason": "补传凭证齐全",
        }, actor="stat")
        self.assertEqual(adjudicated.status, 201)
        self.assertEqual(adjudicated.body["action"], "included")
        missing_actor = self.app.handle(
            "POST", "/late-adjudications", {},
            json.dumps({"observation_id": observation_id, "action": "included", "reason": "x"}).encode(),
        )
        self.assertEqual(missing_actor.status, 422)


if __name__ == "__main__":
    unittest.main()
