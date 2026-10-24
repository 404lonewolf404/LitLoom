import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

os.environ["LITLOOM_DATABASE_URL"] = ""
os.environ["LITLOOM_TESTING"] = "1"
import app
from literature_desk.model_telemetry import endpoint_host, payload_text_chars, response_usage


class ModelTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_paths = (app.DATA_DIR, app.FILES_DIR, app.VISUALS_DIR, app.BACKUP_DIR, app.DB_PATH)
        app.DATA_DIR = Path(self.temp.name) / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.VISUALS_DIR = app.DATA_DIR / "visual-pages"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.init_db()

    def tearDown(self):
        app.DATA_DIR, app.FILES_DIR, app.VISUALS_DIR, app.BACKUP_DIR, app.DB_PATH = self.previous_paths
        self.temp.cleanup()

    def test_usage_normalizer_accepts_chat_responses_and_provider_cost(self):
        self.assertEqual(response_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": "0.0004"}}), {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cost_usd": 0.0004,
        })
        self.assertEqual(response_usage({"usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}})["total_tokens"], 11)

    def test_endpoint_and_payload_helpers_omit_credentials_and_image_data(self):
        self.assertEqual(endpoint_host("https://user:secret@relay.example:8443/v1/tenant-key?token=private"), "relay.example:8443")
        payload = {"messages": [{"content": [{"type": "text", "text": "paper words"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,secret"}}]}]}
        self.assertEqual(payload_text_chars(payload), len("paper words"))

    def test_http_model_call_persists_usage_without_prompt_or_key(self):
        body = json.dumps({
            "id": "provider-call-fixture",
            "choices": [{"message": {"content": "{\"ok\":true}"}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 9, "total_tokens": 40, "cost": 0.00012},
        }).encode()

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return body

        payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "PRIVATE PAPER PROMPT"}]}
        with patch.object(app, "urlopen", return_value=Response()), app.model_usage_context("fast", "text"):
            with app.open_compatible_model_request("https://relay.example.test/v1/private-tenant", payload, "fixture-secret", 3) as response:
                self.assertEqual(app.read_model_response(response)["choices"][0]["message"]["content"], "{\"ok\":true}")

        summary = app.model_usage_summary()
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["input_tokens"], 31)
        self.assertEqual(summary["output_tokens"], 9)
        self.assertAlmostEqual(summary["reported_cost_usd"], 0.00012)
        self.assertEqual(summary["by_task"][0]["task"], "fast")
        with app.db_session() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM model_usage_events")]
        self.assertEqual(rows[0]["endpoint_host"], "relay.example.test")
        self.assertNotIn("PRIVATE PAPER PROMPT", json.dumps(rows))
        self.assertNotIn("fixture-secret", json.dumps(rows))

    def test_streaming_usage_is_recorded_and_connection_errors_are_counted(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n'
            b'data: [DONE]\n'
        )

        class StreamResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self): return iter(body.splitlines())

        with patch.object(app, "urlopen", return_value=StreamResponse()), app.model_usage_context("analysis", "text"):
            with app.open_compatible_model_request("https://relay.example.test/v1/chat/completions", {"model": "stream-fixture"}, "", 3) as response:
                self.assertEqual(app.read_model_response(response)["output_text"], "hello")

        def reject(_request, timeout=0):
            raise HTTPError("https://relay.example.test/v1/chat/completions", 401, "Unauthorized", {}, None)

        with patch.object(app, "urlopen", side_effect=reject):
            with self.assertRaises(HTTPError):
                app.open_compatible_model_request("https://relay.example.test/v1/chat/completions", {"model": "stream-fixture"}, "", 3)
        summary = app.runtime_metrics_summary()["model_usage"]
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["input_tokens"], 4)
        self.assertEqual(summary["output_tokens"], 2)
        self.assertIsNone(summary["reported_cost_usd"])
        self.assertEqual(summary["cost_coverage_pct"], 0)

    def test_schema_16_database_migrates_to_usage_telemetry_schema(self):
        with app.db_session() as db:
            db.execute("UPDATE app_meta SET value='16' WHERE key='schema_version'")
        app.init_db()
        with app.db_session() as db:
            version = db.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(version, str(app.DB_SCHEMA_VERSION))
        self.assertTrue({"model_usage_daily", "model_usage_events"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
