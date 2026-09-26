import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import (INVALID_REASON_QUANTITY_CORRECTED, SEVERITIES, STATES,
                       TRANSITION_ROLES)

COMMANDER = TRANSITION_ROLES['containing'][0]


class EscalationConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create(self, quantity=12, threshold=10, severity='minor', ref='ESC-1'):
        return self.service.create_item({
            "title": "spill", "description": "over the line",
            "severity": severity, "quantity": quantity, "threshold": threshold,
            "external_ref": ref,
        }, "creator", 'observer')

    def test_confirm_registers_estimate_criterion_and_version(self):
        item = self._create()
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        confirmed = self.service.confirm_escalation(
            item["id"], {"note": "依据监测估算确认升级"}, "cmdr", COMMANDER)
        conf = confirmed["escalation_confirmation"]
        self.assertEqual(conf["status"], "valid")
        self.assertEqual(conf["quantity"], 12.0)
        self.assertEqual(conf["item_version"], 2)
        self.assertEqual(conf["criterion"], "quantity_at_threshold")
        contained = self.service.transition(item["id"], "containing",
                                            confirmed["version"], "cmdr", COMMANDER)
        self.assertEqual(contained["status"], "containing")

    def test_transition_without_confirmation_returns_409_with_step(self):
        item = self._create()
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"], "containing", item["version"],
                                    "cmdr", COMMANDER)
        self.assertEqual(ctx.exception.details["step"], "escalation_confirmation")
        self.assertEqual(ctx.exception.details["reason"], "missing_confirmation")
        self.assertEqual(ctx.exception.details["required_confirmation"]["item_version"],
                         item["version"])

    def test_quantity_correction_invalidates_confirmation_and_blocks(self):
        item = self._create()
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        corrected = self.service.correct_estimate(
            item["id"], {"quantity": 20, "expected_version": item["version"]},
            "cmdr", COMMANDER)
        self.assertEqual(corrected["version"], 3)
        conf = corrected["escalation_confirmation"]
        self.assertEqual(conf["status"], "invalidated")
        self.assertEqual(conf["invalid_reason"], INVALID_REASON_QUANTITY_CORRECTED)
        self.assertTrue(conf["invalidated_at"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"], "containing", corrected["version"],
                                    "cmdr", COMMANDER)
        self.assertEqual(ctx.exception.details["reason"],
                         INVALID_REASON_QUANTITY_CORRECTED)
        # 重新确认后放行
        self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        contained = self.service.transition(item["id"], "containing",
                                            corrected["version"], "cmdr", COMMANDER)
        self.assertEqual(contained["status"], "containing")

    def test_confirm_only_commander_and_only_when_required(self):
        item = self._create(quantity=2, threshold=10)
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        with self.assertRaises(PermissionDenied):
            self.service.confirm_escalation(item["id"], {}, "obs", 'observer')
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        self.assertEqual(ctx.exception.details["reason"], "not_required")
        # 未越线无需确认，直接围控
        contained = self.service.transition(item["id"], "containing",
                                            item["version"], "cmdr", COMMANDER)
        self.assertEqual(contained["status"], "containing")

    def test_confirmation_idempotent_and_version_guarded(self):
        item = self._create()
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        first = self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        again = self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        self.assertEqual(first["escalation_confirmation"]["id"],
                         again["escalation_confirmation"]["id"])
        with self.assertRaises(ConflictError):
            self.service.confirm_escalation(
                item["id"], {"expected_version": 99}, "cmdr", COMMANDER)

    def test_list_carries_confirmation_status_and_invalid_reason(self):
        item = self._create(ref='ESC-2')
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        self.service.correct_estimate(item["id"], {"quantity": 30}, "cmdr", COMMANDER)
        listing = self.service.list_items("viewer")
        row = next(x for x in listing if x["id"] == item["id"])
        self.assertEqual(row["escalation_confirmation"]["status"], "invalidated")
        self.assertTrue(row["escalation_confirmation"]["invalid_reason_text"])

    def test_catastrophic_severity_also_requires_confirmation(self):
        item = self._create(quantity=0.1, threshold=10,
                            severity=SEVERITIES[-1], ref='ESC-3')
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(item["id"], "containing", item["version"],
                                    "cmdr", COMMANDER)
        self.assertEqual(ctx.exception.details["reason"], "missing_confirmation")
        confirmed = self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        self.assertEqual(confirmed["escalation_confirmation"]["criterion"],
                         "severity_catastrophic")

    def test_confirmation_and_correction_enter_audit(self):
        item = self._create()
        item = self.service.transition(item["id"], "assessing", item["version"],
                                       "cmdr", COMMANDER)
        self.service.confirm_escalation(item["id"], {}, "cmdr", COMMANDER)
        self.service.correct_estimate(item["id"], {"quantity": 18}, "cmdr", COMMANDER)
        actions = [e["action"] for e in self.service.audit("viewer", item["id"])]
        self.assertIn("escalation_confirmed", actions)
        self.assertIn("estimate_corrected", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
