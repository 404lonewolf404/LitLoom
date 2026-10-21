import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LITLOOM_DATABASE_URL"] = ""
import app
from literature_desk.model_router import resolve_model_route


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_paths = (app.DATA_DIR, app.FILES_DIR, app.VISUALS_DIR, app.BACKUP_DIR, app.DB_PATH)
        self.previous_key = app.RUNTIME_MODEL_KEY
        app.DATA_DIR = Path(self.temp.name) / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.VISUALS_DIR = app.DATA_DIR / "visual-pages"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.RUNTIME_MODEL_KEY = ""
        app.init_db()

    def tearDown(self):
        app.DATA_DIR, app.FILES_DIR, app.VISUALS_DIR, app.BACKUP_DIR, app.DB_PATH = self.previous_paths
        app.RUNTIME_MODEL_KEY = self.previous_key
        self.temp.cleanup()

    def test_empty_routes_keep_primary_model_and_explicit_routes_override_it(self):
        settings = {"model": "primary", "fast_model": " fast-id ", "analysis_model": "analysis-id"}
        self.assertEqual(resolve_model_route(settings, "vision")["model"], "primary")
        self.assertEqual(resolve_model_route(settings, "analysis")["model"], "analysis-id")
        self.assertEqual(resolve_model_route(settings, "fast")["model"], "fast-id")
        self.assertEqual(resolve_model_route(settings, "vision")["model_route"], "default")

    def test_text_request_uses_task_model_in_openai_payload(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}]}).encode())
        settings = {
            "enabled": True,
            "model": "primary",
            "fast_model": "fast-id",
            "analysis_model": "analysis-id",
            "vision_model": "vision-id",
            "base_url": "https://provider.example/v1",
            "api_url": "https://provider.example/v1/chat/completions",
            "protocol": "chat_completions",
            "temperature": 0.2,
        }
        with patch.object(app, "model_settings", return_value=settings), \
                patch.object(app, "model_api_key", return_value="fixture-key"), \
                patch.object(app, "open_compatible_model_request", return_value=response) as request, \
                patch.object(app, "MODEL_STREAMING", False):
            self.assertEqual(app.model_json_request("Return JSON", {"prompt": "fixture"}, task="fast"), {"ok": True})
        payload = request.call_args.args[1]
        self.assertEqual(payload["model"], "fast-id")
        self.assertEqual(request.call_args.args[0], "https://provider.example/v1/chat/completions")

    def test_task_model_settings_persist_and_export_without_secrets(self):
        saved = app.save_model_settings({
            "provider": "fixture",
            "base_url": "https://provider.example/v1",
            "model": "primary",
            "fast_model": "fast-id",
            "analysis_model": "analysis-id",
            "vision_model": "vision-id",
            "enabled": True,
            "api_key": "fixture-secret",
        })
        self.assertEqual(saved["fast_model"], "fast-id")
        self.assertEqual(saved["analysis_model"], "analysis-id")
        self.assertEqual(saved["vision_model"], "vision-id")
        with app.db_session() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(model_config)")}
        self.assertTrue({"fast_model", "analysis_model", "vision_model"}.issubset(columns))
        settings = resolve_model_route(saved, "vision")
        self.assertEqual(settings["model"], "vision-id")
        self.assertNotIn("fixture-secret", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
