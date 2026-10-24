import unittest
from unittest.mock import patch

import app
from fastapi.testclient import TestClient

from literature_desk.web.fastapi_app import create_app


class FastAPIApplicationTests(unittest.TestCase):
    def test_fastapi_owns_asgi_boundary_and_forwards_legacy_root(self):
        application = create_app(app, manage_runtime=False)
        with TestClient(application) as client:
            health = client.get("/api/fastapi/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["framework"], "fastapi")
            self.assertEqual(health.json()["bridge"], "legacy-route-compatibility")

            root = client.get("/")
            self.assertEqual(root.status_code, 200)
            self.assertIn("text/html", root.headers.get("content-type", ""))
            self.assertGreater(len(root.content), 1000)

    def test_native_model_probe_uses_unsaved_form_values_without_saving(self):
        application = create_app(app, manage_runtime=False)
        override = {
            "base_url": "https://relay.example/v1",
            "api_url": "https://relay.example/v1/responses",
            "protocol": "responses",
            "model": "relay-model",
            "api_key": "temporary-test-key",
        }
        with (
            patch.object(app, "team_auth_enabled", return_value=False),
            patch.object(app, "test_model_connection", return_value={"ok": True, "tested": "inference"}) as probe,
            patch.object(app, "save_model_settings") as save,
            TestClient(application) as client,
        ):
            response = client.post(
                "/api/settings/model/test",
                json={"deep": True, "settings": override},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tested"], "inference")
        probe.assert_called_once_with(deep=True, settings_override=override)
        save.assert_not_called()

    def test_native_model_settings_require_a_valid_session(self):
        application = create_app(app, manage_runtime=False)
        with (
            patch.object(app, "team_auth_enabled", return_value=True),
            patch.object(app, "resolve_auth_session", return_value=None),
            TestClient(application) as client,
        ):
            response = client.get("/api/settings/model")
        self.assertEqual(response.status_code, 401)

    def test_native_routes_are_published_in_openapi(self):
        application = create_app(app, manage_runtime=False)
        with TestClient(application) as client:
            paths = client.get("/api/openapi.json").json()["paths"]
        for path in (
            "/api/settings/model",
            "/api/settings/model/test",
            "/api/agent/framework",
            "/api/agent/runs/{run_id}",
            "/api/agent/autonomous",
        ):
            self.assertIn(path, paths)


if __name__ == "__main__":
    unittest.main()
