import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from liveops_hub.core import InstalledResolutionApp, LiveOpsHub


class InstalledAppIntegration(unittest.TestCase):
    @unittest.skipUnless(Path("/tmp/resolution-installed").exists(), "installed app unavailable")
    def test_pinned_demo_cannot_send(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path("/tmp/resolution-installed")
            command = ("a2z-app",) if shutil.which("a2z-app") else (sys.executable, "-m", "a2z_app_factory.cli")
            app = InstalledResolutionApp(target, target / "source/examples/knowledge.synthetic.json",
                                         Path(temp) / "engine.sqlite3", "",
                                         "5aa314fb1b2b24cb0a90d6b3a64d5af83b49c5c2",
                                         target / "source/examples/zendesk-ticket.synthetic.json", factory_command=command)
            hub = LiveOpsHub(Path(temp) / "hub.sqlite3", "synthetic-ci", app, "reviewer-ci", "sender-ci")
            first = hub.import_demo("en")
            self.assertEqual(first["status"], "PENDING_REVIEW")
            hub.review(first["draft_id"], True)
            with self.assertRaises(ValueError):
                hub.send(first["draft_id"])
            self.assertEqual(hub.summary()["verified_accepted_resolutions"], None)


if __name__ == "__main__":
    unittest.main()
