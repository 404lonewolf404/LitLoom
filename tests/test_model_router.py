import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LITLOOM_DATABASE_URL"] = ""
os.environ["LITLOOM_TESTING"] = "1"
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

    def test_team_members_get_isolated_model_settings(self):
        actor_a = {"workspace_id": "workspace-a", "user_id": "user-a"}
        actor_b = {"workspace_id": "workspace-a", "user_id": "user-b"}
        token_a = app.AUTH_ACTOR.set(actor_a)
        try:
            saved_a = app.save_model_settings({
                "provider": "provider-a",
                "base_url": "https://a.example/v1",
                "model": "model-a",
                "api_key": "key-a",
                "enabled": True,
            })
        finally:
            app.AUTH_ACTOR.reset(token_a)
        token_b = app.AUTH_ACTOR.set(actor_b)
        try:
            saved_b = app.save_model_settings({
                "provider": "provider-b",
                "base_url": "https://b.example/v1",
                "model": "model-b",
                "api_key": "key-b",
                "enabled": True,
            })
            current_b = app.model_settings()
        finally:
            app.AUTH_ACTOR.reset(token_b)
        token_a = app.AUTH_ACTOR.set(actor_a)
        try:
            current_a = app.model_settings()
            self.assertEqual(current_a["model"], "model-a")
            self.assertEqual(current_a["base_url"], "https://a.example/v1")
            self.assertEqual(current_b["model"], "model-b")
            self.assertEqual(saved_a["provider"], "provider-a")
            self.assertEqual(saved_b["provider"], "provider-b")
            self.assertNotEqual(app.model_api_key(), "key-b")
        finally:
            app.AUTH_ACTOR.reset(token_a)

    def test_team_member_without_saved_row_does_not_inherit_legacy_endpoint(self):
        app.save_model_settings({
            "provider": "legacy-provider",
            "base_url": "https://legacy.example/v1",
            "model": "legacy-model",
            "api_key": "legacy-key",
            "enabled": True,
        })
        actor = {"workspace_id": "workspace-a", "user_id": "new-user"}
        token = app.AUTH_ACTOR.set(actor)
        try:
            with patch.dict(os.environ, {
                "LIT_AGENT_BASE_URL": "https://deployment.example/v1",
                "LIT_AGENT_MODEL": "deployment-model",
            }, clear=False):
                settings = app.model_settings()
            self.assertEqual(settings["base_url"], "https://deployment.example/v1")
            self.assertEqual(settings["model"], "deployment-model")
            self.assertNotEqual(settings["base_url"], "https://legacy.example/v1")
        finally:
            app.AUTH_ACTOR.reset(token)


if __name__ == "__main__":
    unittest.main()
