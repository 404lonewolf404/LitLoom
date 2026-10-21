import os
import unittest
from unittest.mock import patch

from literature_desk.release import feature_assignment, release_snapshot


class ReleaseAssignmentTests(unittest.TestCase):
    def test_canary_assignment_is_stable_and_bounded(self):
        env = {
            "LITLOOM_CANARY_FEATURES": "new-ranker",
            "LITLOOM_CANARY_PERCENT": "50",
            "LITLOOM_CANARY_SALT": "fixture",
        }
        with patch.dict(os.environ, env, clear=False):
            first = feature_assignment("new-ranker", "workspace-a")
            second = feature_assignment("new-ranker", "workspace-a")
        self.assertEqual(first, second)
        self.assertGreaterEqual(first["bucket"], 0)
        self.assertLess(first["bucket"], 100)
        self.assertEqual(first["percent"], 50)

    def test_unlisted_feature_is_never_enabled(self):
        with patch.dict(os.environ, {"LITLOOM_CANARY_FEATURES": "", "LITLOOM_CANARY_PERCENT": "100"}, clear=False):
            result = feature_assignment("not-enabled", "workspace-a")
        self.assertFalse(result["enabled"])
        self.assertFalse(result["configured"])

    def test_release_snapshot_is_safe_and_reports_configured_features(self):
        env = {"LITLOOM_RELEASE_CHANNEL": "canary", "LITLOOM_CANARY_FEATURES": "search-v2", "LITLOOM_CANARY_PERCENT": "25"}
        with patch.dict(os.environ, env, clear=False):
            snapshot = release_snapshot("workspace-a")
        self.assertEqual(snapshot["channel"], "canary")
        self.assertEqual(snapshot["canary_percent"], 25)
        self.assertIn("search-v2", snapshot["assignments"])
        self.assertNotIn("workspace-a", str(snapshot))


if __name__ == "__main__":
    unittest.main()
