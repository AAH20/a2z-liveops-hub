import tempfile
import unittest
from pathlib import Path

from liveops_hub.core import LiveOpsHub
from liveops_hub.server import make_handler, require_loopback


class FakePort:
    def __init__(self):
        self.sent = 0
        self.fail_send = False
        self.state = "PENDING_REVIEW"

    def prepare(self, ticket_id, locale):
        return self.prepare_demo(locale)

    def prepare_demo(self, locale):
        return {"status": self.state, "draft_id": "draft-1", "answer": "Invented answer",
                "source": "https://example.invalid/kb/1", "article_id": "kb-1", "source_stamp": "2026-01-01"}

    def review(self, draft_id, reviewer, approve):
        self.state = "APPROVED_PENDING_SEND" if approve else "REJECTED"
        return {"draft_id": draft_id, "status": self.state}

    def send(self, draft_id, ticket_id):
        self.sent += 1
        if self.fail_send:
            raise RuntimeError("network uncertain")
        self.state = "SENT_UNVERIFIED"
        return {"draft_id": draft_id, "status": self.state}

    def outcome(self, draft_id, reviewer, status):
        return {"draft_id": draft_id, "outcome": status}

    def summary(self):
        return {}


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "pilot" / "hub.sqlite3"
        self.port = FakePort()
        self.hub = LiveOpsHub(self.db, "pilot-one", self.port, "reviewer-one", "sender-one")

    def test_full_local_flow_and_claim_boundary(self):
        self.assertEqual(self.hub.import_ticket(101,"en")["status"], "PENDING_REVIEW")
        self.assertEqual(self.hub.review("draft-1", True)["status"], "APPROVED_PENDING_SEND")
        self.assertEqual(self.hub.send("draft-1")["status"], "SENT_UNVERIFIED")
        self.assertEqual(self.hub.outcome("draft-1", "ACCEPTED")["evidence_class"], "OPERATOR_DECLARED_UNVERIFIED")
        self.assertEqual(self.hub.summary()["operator_declared_outcomes"]["ACCEPTED"], 1)
        self.assertIsNone(self.hub.summary()["verified_accepted_resolutions"])
        self.assertEqual(len(self.hub.events()), 7)
        with self.assertRaises(ValueError):
            self.hub.send("draft-1")
        self.assertEqual(self.port.sent, 1)

    def test_ambiguous_send_blocks_retry(self):
        self.hub.import_ticket(101,"en")
        self.hub.review("draft-1", True)
        self.port.fail_send = True
        with self.assertRaises(RuntimeError):
            self.hub.send("draft-1")
        self.assertEqual(self.hub.drafts()[0]["status"], "UNCERTAIN_RECONCILE")
        with self.assertRaises(ValueError):
            self.hub.send("draft-1")
        self.assertEqual(self.port.sent, 1)

    def test_customer_scope_is_bound_to_database(self):
        with self.assertRaises(ValueError):
            LiveOpsHub(self.db, "another-customer", self.port, "reviewer", "sender")

    def test_demo_never_sends(self):
        self.hub.import_demo("en")
        self.hub.review("draft-1", True)
        with self.assertRaises(ValueError):
            self.hub.send("draft-1")
        self.assertEqual(self.port.sent, 0)

    def test_reject_and_duplicate_import(self):
        self.hub.import_demo("en")
        self.hub.review("draft-1", False)
        self.assertEqual(self.hub.import_demo("en")["status"], "REJECTED")

    def test_loopback_and_role_keys(self):
        require_loopback("127.0.0.1")
        require_loopback("::1")
        for host in ("0.0.0.0", "localhost", "192.168.1.2"):
            with self.assertRaises(ValueError):
                require_loopback(host)
        with self.assertRaises(ValueError):
            make_handler(self.hub, {"operator": "x" * 32, "reviewer": "x" * 32, "sender": "y" * 32})


if __name__ == "__main__":
    unittest.main()
