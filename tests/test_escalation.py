import json
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.http_api import make_handler
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class EscalationConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "escalation item",
            "description": "estimate crosses escalation threshold",
            "severity": "major",
            "quantity": 12,
            "threshold": 6,
        }, "observer", "observer")
        self.item = self.service.transition(
            self.item["id"], STATES[1], self.item["version"],
            "commander", TRANSITION_ROLES[STATES[1]][0])

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_confirmation_records_estimate_criterion_and_version(self):
        confirmed = self.service.confirm_escalation(
            self.item["id"], "commander", "response_commander")
        confirmation = confirmed["escalation_confirmation"]
        self.assertEqual(confirmation["estimated_quantity"], 12)
        self.assertEqual(confirmation["threshold"], 6)
        self.assertEqual(confirmation["severity"], "major")
        self.assertEqual(confirmation["item_version"], 2)
        self.assertEqual(confirmation["status"], "active")
        self.assertEqual(confirmation["criterion"]["type"], "quantity_at_or_over_threshold")
        self.assertEqual(confirmation["criterion"]["expression"], "quantity >= threshold")
        self.assertEqual(confirmed["escalation_confirmation_status"], "active")

        current = self.service.transition(
            self.item["id"], "containing", self.item["version"],
            "commander", "response_commander")
        self.assertEqual(current["status"], "containing")
        self.assertEqual(current["escalation_confirmation_status"], "active")

    def test_missing_confirmation_blocks_containing_with_step(self):
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.item["id"], "containing", self.item["version"],
                "commander", "response_commander")
        self.assertEqual(caught.exception.details["blocked_step"], "escalation_confirmation")
        self.assertEqual(caught.exception.details["required_before_transition"], "containing")
        self.assertEqual(caught.exception.details["reason"], "missing_confirmation")

    def test_correction_invalidates_old_confirmation_and_reconfirmation_unblocks(self):
        self.service.confirm_escalation(
            self.item["id"], "commander", "response_commander")
        corrected = self.service.correct_estimate(
            self.item["id"], {"quantity": 13, "expected_version": 2},
            "observer", "observer")
        self.assertEqual(corrected["version"], 3)
        self.assertEqual(corrected["escalation_confirmation_status"], "invalidated")
        self.assertIn("由12更正为13", corrected["escalation_confirmation_invalidated_reason"])
        self.assertEqual(corrected["escalation_confirmation"]["item_version"], 2)

        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                self.item["id"], "containing", 3,
                "commander", "response_commander")
        self.assertEqual(caught.exception.details["blocked_step"], "escalation_confirmation")
        self.assertEqual(caught.exception.details["reason"], "confirmation_invalidated")
        self.assertIn("重新提交升级确认", str(caught.exception))

        detail = self.service.get_item(self.item["id"], "viewer")
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(detail["escalation_confirmation_status"], "invalidated")
        self.assertEqual(listed["escalation_confirmation_status"], "invalidated")
        self.assertTrue(detail["escalation_confirmation_invalidated_reason"])
        self.assertTrue(listed["escalation_confirmation_invalidated_reason"])

        self.service.confirm_escalation(
            self.item["id"], "commander", "response_commander")
        current = self.service.transition(
            self.item["id"], "containing", 3,
            "commander", "response_commander")
        self.assertEqual(current["status"], "containing")

    def test_confirmation_and_correction_are_audited(self):
        self.service.confirm_escalation(
            self.item["id"], "commander", "response_commander")
        self.service.correct_estimate(
            self.item["id"], {"quantity": 14, "expected_version": 2},
            "observer", "observer")
        actions = [event["action"] for event in self.service.audit("viewer", self.item["id"])]
        self.assertIn("escalation_confirmation", actions)
        self.assertIn("estimate_correction", actions)
        self.assertIn("escalation_confirmation_invalidated", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_catastrophic_severity_uses_severity_criterion(self):
        item = self.service.create_item({
            "title": "catastrophic", "description": "severity criterion",
            "severity": "catastrophic", "quantity": 0.1, "threshold": 10,
        }, "observer", "observer")
        item = self.service.transition(
            item["id"], "assessing", item["version"],
            "commander", "response_commander")
        with self.assertRaises(ConflictError):
            self.service.transition(
                item["id"], "containing", item["version"],
                "commander", "response_commander")
        confirmed = self.service.confirm_escalation(
            item["id"], "commander", "response_commander")
        self.assertEqual(
            confirmed["escalation_confirmation"]["criterion"]["type"],
            "severity_catastrophic")

    def test_permission_and_estimation_guards(self):
        with self.assertRaises(PermissionDenied):
            self.service.confirm_escalation(self.item["id"], "ops", "operations")
        self.service.confirm_escalation(
            self.item["id"], "commander", "response_commander")
        with self.assertRaises(ConflictError):
            self.service.confirm_escalation(
                self.item["id"], "commander", "response_commander")
        with self.assertRaises(ValidationError):
            self.service.correct_estimate(
                self.item["id"], {"quantity": 12, "expected_version": 2},
                "observer", "observer")
        current = self.service.transition(
            self.item["id"], "containing", 2,
            "commander", "response_commander")
        with self.assertRaises(ConflictError):
            self.service.correct_estimate(
                current["id"], {"quantity": 15, "expected_version": 3},
                "observer", "observer")


class EscalationHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        item = self.service.create_item({
            "title": "http item", "description": "http escalation",
            "severity": "major", "quantity": 10, "threshold": 5,
        }, "observer", "observer")
        self.item = self.service.transition(
            item["id"], "assessing", item["version"],
            "commander", "response_commander")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.repo.close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None, role="response_commander"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-Actor": "tester",
                     "X-Role": role})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_http_409_identifies_blocked_step(self):
        status, payload = self.request(
            "POST", f"/api/items/{self.item['id']}/transition",
            {"target": "containing", "expected_version": 2})
        self.assertEqual(status, 409)
        self.assertEqual(payload["details"]["blocked_step"], "escalation_confirmation")

        status, payload = self.request(
            "POST", f"/api/items/{self.item['id']}/escalation-confirmation")
        self.assertEqual(status, 201)
        self.assertEqual(payload["escalation_confirmation_status"], "active")

        status, payload = self.request(
            "PATCH", f"/api/items/{self.item['id']}/estimate",
            {"quantity": 11, "expected_version": 2}, role="observer")
        self.assertEqual(status, 200)
        self.assertEqual(payload["escalation_confirmation_status"], "invalidated")


if __name__ == "__main__":
    unittest.main()
