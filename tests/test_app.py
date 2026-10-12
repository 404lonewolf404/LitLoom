import base64
import io
import json
import hashlib
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import app


class LiteratureDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app.DATA_DIR = root / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.RUNTIME_MODEL_KEY = ""
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def test_doi_and_arxiv_versions_deduplicate_and_preserve_notes(self):
        pid, created = app.upsert_paper({"title": "A Study", "doi": "https://doi.org/10.1234/ABC", "year": 2024, "notes": "my note"}, source="manual")
        self.assertTrue(created)
        same, created_again = app.upsert_paper({"title": "A Study Updated", "doi": "10.1234/abc", "abstract": "new metadata"}, source="crossref")
        self.assertEqual(same, pid)
        self.assertFalse(created_again)
        with app.db_session() as db:
            row = db.execute("SELECT * FROM papers WHERE id=?", (pid,)).fetchone()
            count = db.execute("SELECT COUNT(*) FROM source_records WHERE paper_id=?", (pid,)).fetchone()[0]
        self.assertEqual(row["notes"], "my note")
        self.assertEqual(row["title"], "A Study")
        self.assertEqual(count, 2)

        arxiv_id, first = app.upsert_paper({"title": "Vision Models", "arxiv_id": "https://arxiv.org/abs/2401.01234v1", "year": 2024}, source="arxiv")
        same_arxiv, second = app.upsert_paper({"title": "Vision Models revised", "arxiv_id": "2401.01234v3", "year": 2025}, source="arxiv")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(arxiv_id, same_arxiv)

    def test_exact_title_year_guard_only_applies_without_strong_ids(self):
        pid, created = app.upsert_paper({"title": "A careful title: with punctuation", "year": 2025}, source="manual")
        same, created_again = app.upsert_paper({"title": "A careful title with punctuation", "year": 2025}, source="import")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(pid, same)
        other, created_other = app.upsert_paper({"title": "A careful title with punctuation", "year": 2025, "doi": "10.7777/different"}, source="crossref")
        self.assertTrue(created_other)
        self.assertNotEqual(pid, other)

    def test_near_title_without_identifier_is_merged_across_providers(self):
        pid, created = app.upsert_paper({"title": "A robust method: for vision", "year": 2025}, source="openalex")
        same, created_again = app.upsert_paper({"title": "A Robust Method for Vision", "year": 2025, "abstract": "provider metadata"}, source="crossref")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(pid, same)
        self.assertGreaterEqual(app.title_similarity("多模态推理模型", "多模态推理模型"), 0.99)

    def test_query_memory_tracks_relevance_and_seen_count(self):
        query_id = "query-memory-test"
        with app.db_session() as db:
            now = app.now_iso()
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", (query_id, "多模态推理", "multimodal reasoning", '["arxiv"]', 168, now, now, now))
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit: [{
            "title": "Multimodal Reasoning with Visual Evidence",
            "abstract": "We study multimodal reasoning and visual evidence.",
            "year": 2026,
            "arxiv_id": "2601.00001v1",
            "source": "arxiv",
            "external_id": "2601.00001v1",
        }]
        try:
            first = app.run_query("multimodal reasoning", ["arxiv"], query_id=query_id)
            second = app.run_query("multimodal reasoning", ["arxiv"], query_id=query_id)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(first["papers"][0]["relevance_label"], "高相关")
        self.assertFalse(first["papers"][0]["seen_before"])
        self.assertTrue(second["papers"][0]["seen_before"])
        with app.db_session() as db:
            match = db.execute("SELECT * FROM query_matches WHERE query_id=?", (query_id,)).fetchone()
        self.assertEqual(match["seen_count"], 2)
        self.assertEqual(match["user_decision"], "new")

    def test_topic_feedback_is_remembered_and_repeated_results_are_suppressed(self):
        query_id = "query-feedback-test"
        with app.db_session() as db:
            now = app.now_iso()
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", (query_id, "推理", "multimodal reasoning", '["arxiv"]', 168, now, now, now))
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit: [{"title": "Multimodal Reasoning Feedback Fixture", "abstract": "A fixture about multimodal reasoning.", "year": 2026, "arxiv_id": "2602.00002v1", "external_id": "2602.00002v1"}]
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            first = app.run_query("multimodal reasoning", ["arxiv"], query_id=query_id)
            paper_id = first["papers"][0]["id"]
            req = Request(f"http://127.0.0.1:{server.server_port}/api/queries/{query_id}/matches/{paper_id}", data=json.dumps({"user_decision": "irrelevant", "viewed": True}).encode(), method="PATCH", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as response:
                self.assertEqual(response.status, 200)
            again = app.run_query("multimodal reasoning", ["arxiv"], query_id=query_id)
            self.assertEqual(again["suppressed_count"], 1)
            self.assertTrue(again["papers"][0]["suppressed"])
            self.assertTrue(any("忽略反馈" in reason for reason in again["papers"][0]["relevance_reasons"]))
            with app.db_session() as db:
                match = db.execute("SELECT user_decision,seen_count FROM query_matches WHERE query_id=? AND paper_id=?", (query_id, paper_id)).fetchone()
            self.assertEqual(match["user_decision"], "irrelevant")
            self.assertEqual(match["seen_count"], 2)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_search_results_are_persisted_for_reload(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit: [{
            "title": "Durable search result fixture",
            "abstract": "A result restored from the local run snapshot.",
            "year": 2026,
            "arxiv_id": "2609.00009v1",
            "external_id": "2609.00009v1",
        }]
        try:
            result = app.run_query("durable search", ["arxiv"], 10)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        restored = app.run_results_snapshot(result["id"])
        self.assertEqual(restored["status"], "success")
        self.assertTrue(restored["results_available"])
        self.assertEqual(restored["papers"][0]["title"], "Durable search result fixture")
        with app.db_session() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM run_snapshots WHERE run_id=?", (result["id"],)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM run_papers WHERE run_id=?", (result["id"],)).fetchone()[0], 1)

    def test_openalex_reference_sync_is_idempotent(self):
        paper_id, _ = app.upsert_paper({"title": "Reference fixture", "doi": "10.1000/fixture"}, source="manual")
        original = app.http_json
        app.http_json = lambda _url, timeout=25: {"referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"]}
        try:
            first = app.sync_openalex_references(paper_id)
            second = app.sync_openalex_references(paper_id)
        finally:
            app.http_json = original
        self.assertEqual(first["count"], 2)
        self.assertEqual(second["count"], 2)
        with app.db_session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM citation_edges WHERE source_paper_id=?", (paper_id,)).fetchone()[0], 2)

    def test_saved_query_fetches_multiple_source_pages(self):
        query_id = "query-pages-test"
        with app.db_session() as db:
            now = app.now_iso()
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", (query_id, "分页", "page fixture", '["arxiv"]', 168, now, now, now))
        original = app.SOURCE_SEARCH["arxiv"]
        def fake_search(_query, _limit, offset=0):
            if offset == 0:
                return [{"title": f"Page one fixture {i}", "year": 2026, "arxiv_id": f"2603.{i:05d}"} for i in range(50)]
            if offset == 50:
                return [{"title": "Page two fixture", "year": 2026, "arxiv_id": "2603.00100"}]
            return []
        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            result = app.run_query("page fixture", ["arxiv"], 50, query_id=query_id)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(result["sources"][0]["fetched"], 51)
        self.assertEqual(result["new_count"], 51)

    def test_model_settings_use_openai_shape_without_persisting_key(self):
        saved = app.save_model_settings({"provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-test", "temperature": 0.4, "enabled": True, "api_key": "sk-test-secret"})
        self.assertTrue(saved["api_key_set"])
        self.assertEqual(saved["api_key_source"], "当前进程")
        self.assertEqual(saved["model"], "gpt-test")
        with app.db_session() as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(model_config)").fetchall()]
            row = db.execute("SELECT * FROM model_config WHERE id=1").fetchone()
            audit = " ".join(r[0] for r in db.execute("SELECT payload_json FROM audit_events WHERE event_type='model.settings.updated'").fetchall())
        self.assertNotIn("api_key", columns)
        self.assertNotIn("sk-test-secret", " ".join(str(value) for value in row))
        self.assertNotIn("sk-test-secret", audit)
        with self.assertRaisesRegex(ValueError, "不要包含用户名、密码"):
            app.save_model_settings({"base_url": "https://secret:password@relay.example.test/v1", "model": "gpt-test"})

    def test_local_api_key_survives_process_restart_without_returning_secret(self):
        app.save_model_settings({"provider": "relay", "base_url": "https://relay.example.test/v1", "model": "relay-model", "enabled": True, "api_key": "local-secret"})
        secret_path = app.local_model_key_path()
        self.assertTrue(secret_path.exists())
        app.RUNTIME_MODEL_KEY = ""
        restored = app.model_settings()
        self.assertTrue(restored["api_key_set"])
        self.assertEqual(restored["api_key_source"], "本地配置文件")
        self.assertNotIn("local-secret", json.dumps(restored, ensure_ascii=False))
        self.assertEqual(app.model_api_key(), "local-secret")

    def test_clear_local_api_key_removes_persisted_secret(self):
        config = {"provider": "relay", "base_url": "https://relay.example.test/v1", "model": "relay-model", "enabled": True, "api_key": "local-secret"}
        app.save_model_settings(config)
        app.save_model_settings({**config, "api_key": "", "clear_api_key": True})
        app.RUNTIME_MODEL_KEY = ""
        self.assertFalse(app.local_model_key_path().exists())
        self.assertFalse(app.model_settings()["api_key_set"])

    def test_plain_text_local_api_key_file_is_supported(self):
        secret_path = app.local_model_key_path()
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text("plain-text-secret\n", encoding="utf-8")
        self.assertEqual(app.model_api_key(), "plain-text-secret")

    def test_session_only_key_clears_an_existing_local_secret(self):
        config = {"provider": "relay", "base_url": "https://relay.example.test/v1", "model": "relay-model", "enabled": True, "api_key": "saved-secret"}
        app.save_model_settings(config)
        app.save_model_settings({**config, "api_key": "session-secret", "persist_api_key": False})
        self.assertFalse(app.local_model_key_path().exists())
        self.assertEqual(app.model_api_key(), "session-secret")

    def test_model_settings_accept_api_url_without_base_url_and_derive_root(self):
        saved = app.save_model_settings({
            "provider": "Relay",
            "api_url": "https://relay.example.test/v1/chat/completions",
            "model": "relay-model",
            "enabled": True,
            "api_key": "relay-api-only-secret",
        })
        self.assertEqual(saved["api_url"], "https://relay.example.test/v1/chat/completions")
        self.assertEqual(saved["base_url"], "https://relay.example.test/v1")
        with self.assertRaisesRegex(ValueError, "至少填写 Base URL 或 API 请求地址"):
            app.save_model_settings({"base_url": "", "api_url": "", "model": "relay-model"})

    def test_model_request_uses_complete_custom_api_url_without_appending_path(self):
        original_key = app.RUNTIME_MODEL_KEY
        original_open = app.urlopen
        captured = {}
        app.RUNTIME_MODEL_KEY = "relay-secret"
        app.save_model_settings({
            "api_url": "https://relay.example.test/custom/generate",
            "model": "relay-model",
            "enabled": True,
            "api_key": "relay-secret",
        })

        class Response:
            headers = {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}]}).encode()

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            return Response()

        app.urlopen = fake_open
        try:
            app.model_json_request("system", {"query": "fixture"})
        finally:
            app.RUNTIME_MODEL_KEY = original_key
            app.urlopen = original_open
        # An explicit API URL is used verbatim, including non-standard paths.
        self.assertEqual(captured["url"], "https://relay.example.test/custom/generate")

    def test_deep_model_connection_uses_api_url_and_parses_sse(self):
        original_open = app.urlopen
        captured = {}
        app.save_model_settings({
            "base_url": "",
            "api_url": "https://relay.example.test/v1/responses",
            "protocol": "responses",
            "model": "relay-model",
            "enabled": True,
            "api_key": "relay-sse-secret",
        })

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                return iter([
                    b'data: {"delta":"O"}\n\n',
                    b'data: {"delta":"K"}\n\n',
                    b'data: [DONE]\n\n',
                ])

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return Response()

        app.urlopen = fake_open
        try:
            result = app.test_model_connection(deep=True)
        finally:
            app.urlopen = original_open
        self.assertTrue(result["ok"])
        self.assertEqual(result["tested"], "inference")
        self.assertEqual(captured["url"], "https://relay.example.test/v1/responses")
        self.assertEqual(captured["timeout"], 180)

    def test_openai_compatible_connection_uses_models_endpoint(self):
        app.save_model_settings({"provider": "openai", "base_url": "https://proxy.example.test/v1", "model": "gpt-test", "enabled": True, "api_key": "sk-protocol-secret"})
        original = app.urlopen
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        app.urlopen = fake_urlopen
        try:
            result = app.test_model_connection()
        finally:
            app.urlopen = original
        self.assertTrue(result["ok"])
        self.assertEqual(captured["url"], "https://proxy.example.test/v1/models")
        self.assertEqual(captured["authorization"], "Bearer sk-protocol-secret")
        self.assertEqual(captured["timeout"], 20)

    def test_openai_compatible_chat_request_uses_standard_endpoint_and_bearer_auth(self):
        original_settings = app.model_settings
        original_key = app.RUNTIME_MODEL_KEY
        original_open = app.urlopen
        captured = {}
        app.RUNTIME_MODEL_KEY = "relay-secret"
        app.model_settings = lambda: {"enabled": True, "model": "relay-model", "base_url": "https://relay.example.test/v1", "temperature": 0.2}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}]}).encode()

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            return Response()

        app.urlopen = fake_open
        try:
            self.assertEqual(app.model_json_request("system prompt", {"question": "fixture"}), {"ok": True})
        finally:
            app.model_settings = original_settings
            app.RUNTIME_MODEL_KEY = original_key
            app.urlopen = original_open
        self.assertEqual(captured["url"], "https://relay.example.test/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer relay-secret")
        self.assertEqual(captured["body"]["model"], "relay-model")
        self.assertEqual(captured["body"]["messages"][1]["role"], "user")
        self.assertIn("fixture", captured["body"]["messages"][1]["content"])

    def test_model_connection_error_exposes_status_and_models_path(self):
        from urllib.error import HTTPError

        app.save_model_settings({"provider": "openai", "base_url": "https://relay.example.test/v1", "model": "relay-model", "enabled": True, "api_key": "relay-secret"})
        original = app.urlopen

        def reject(_request, timeout=0):
            raise HTTPError("https://relay.example.test/v1/models", 401, "Unauthorized", {}, None)

        app.urlopen = reject
        try:
            with self.assertRaisesRegex(ValueError, "HTTP 401.*GET https://relay.example.test/v1/models"):
                app.test_model_connection()
        finally:
            app.urlopen = original

    def test_analytics_summary_is_data_backed(self):
        app.upsert_paper({"title": "Analytics fixture", "year": 2026, "status": "read", "topic": "multimodal"}, source="manual")
        app.upsert_paper({"title": "Analytics backlog", "year": 2026, "status": "to_read", "topic": "multimodal"}, source="manual")
        summary = app.analytics_summary()
        self.assertGreaterEqual(summary["metrics"]["total_papers"], 2)
        self.assertEqual(summary["status"]["read"], 1)
        self.assertEqual(summary["status"]["to_read"], 1)
        self.assertEqual(summary["topics"][0]["topic"], "multimodal")
        self.assertIn("feedback_coverage", summary["metrics"])
        self.assertIn("audit_events_30d", summary["metrics"])
        self.assertEqual(summary["metrics"]["schema_version"], "9")
        self.assertIn("retrieval_funnel", summary)
        self.assertIn("optimization_actions", summary)
        self.assertIn("development_cycle", summary)
        self.assertTrue(summary["optimization_actions"])

    def test_batch_triage_is_atomic_bounded_and_audited(self):
        first, _ = app.upsert_paper({"title": "Batch fixture one", "status": "inbox"}, source="manual")
        second, _ = app.upsert_paper({"title": "Batch fixture two", "status": "inbox"}, source="manual")
        result = app.batch_update_papers([first, "missing-paper", second], {"status": "to_read", "topic": "multimodal"})
        self.assertEqual(result, {"updated": 2, "skipped": ["missing-paper"]})
        with app.db_session() as db:
            rows = [dict(row) for row in db.execute("SELECT status,topic FROM papers WHERE id IN (?,?)", (first, second))]
            audit = db.execute("SELECT payload_json FROM audit_events WHERE event_type='papers.batch_updated' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(rows, [{"status": "to_read", "topic": "multimodal"}] * 2)
        self.assertEqual(json.loads(audit[0])["updated"], 2)
        with self.assertRaises(ValueError):
            app.batch_update_papers([first] * 501, {"status": "read"})

    def test_structured_reading_card_tracks_completion_and_analytics(self):
        paper_id, _ = app.upsert_paper({"title": "Reading card fixture", "status": "read"}, source="manual")
        with app.db_session() as db:
            card = app.save_reading_card(db, paper_id, {"research_question": "What is evaluated?", "results": "Improved on the benchmark."})
        self.assertEqual(card["completed_fields"], 2)
        self.assertEqual(card["field_count"], 11)
        self.assertEqual(card["completion_pct"], 18)
        summary = app.analytics_summary()["reading_evidence"]
        self.assertEqual(summary["card_count"], 1)
        self.assertEqual(summary["coverage_pct"], 100)
        self.assertEqual(summary["field_completion_pct"], 18.2)

    def test_search_index_covers_reading_cards_pdf_pages_and_rebuilds(self):
        paper_id, _ = app.upsert_paper({"title": "Search coverage fixture", "abstract": "metadata retrieval"}, source="manual")
        with app.db_session() as db:
            app.save_reading_card(db, paper_id, {"research_question": "证据校准如何工作 evidence calibration", "method": "counterfactual protocol"})
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("fixture-attachment", paper_id, "fixture.pdf", "fixture.pdf", "a" * 64, 10, app.now_iso()))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("fixture-attachment", paper_id, 1, "exactpagekeyword in the experimental protocol", 44, "good", "fixture", "b" * 64, app.now_iso()))
        card = app.search_library("counterfactual")
        pdf = app.search_library("exactpagekeyword")
        chinese = app.search_library("证据校准")
        self.assertEqual(card["results"][0]["id"], paper_id)
        self.assertIn("reading_card", card["results"][0]["match_fields"])
        self.assertEqual(pdf["results"][0]["id"], paper_id)
        self.assertIn("pdf_text", pdf["results"][0]["match_fields"])
        self.assertEqual(chinese["results"][0]["id"], paper_id)
        rebuilt = app.rebuild_search_index()
        self.assertEqual(rebuilt["documents"], 1)
        self.assertEqual(app.search_library("exactpagekeyword")["total"], 1)

    def test_library_pagination_and_filters_are_server_side(self):
        for index in range(7):
            app.upsert_paper({"title": f"Pagination fixture {index}", "year": 2020 + index, "topic": "long-term"}, source="manual")
        first = app.search_library("", limit=3, offset=0, topic="long-term")
        second = app.search_library("", limit=3, offset=3, topic="long-term")
        self.assertEqual(first["total"], 7)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["results"]), 3)
        self.assertEqual(len(second["results"]), 3)
        self.assertFalse(set(item["id"] for item in first["results"]) & set(item["id"] for item in second["results"]))
        self.assertEqual(app.search_library("", limit=10, filters={"year_from": 2025})["total"], 2)

    def test_note_and_reading_card_revisions_are_restorable_and_idempotent(self):
        paper_id, _ = app.upsert_paper({"title": "Revision fixture"}, source="manual")
        with app.db_session() as db:
            app.save_reading_card(db, paper_id, {"method": "method v1"})
            app.save_reading_card(db, paper_id, {"method": "method v1"})
            db.execute("UPDATE papers SET notes=? WHERE id=?", ("note v1", paper_id))
            app.record_paper_revision(db, paper_id, "notes", {"notes": "note v1"})
            db.execute("UPDATE papers SET notes=? WHERE id=?", ("note v2", paper_id))
            app.record_paper_revision(db, paper_id, "notes", {"notes": "note v2"})
        revisions = app.paper_history(paper_id)
        notes = [item for item in revisions if item["revision_type"] == "notes"]
        cards = [item for item in revisions if item["revision_type"] == "reading_card"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(cards), 1)
        self.assertEqual(notes[-1]["content"]["notes"], "note v1")

    def test_run_detail_decodes_only_safe_source_and_variant_fields(self):
        run_id = "run-detail-fixture"
        detail = [{"source": "arxiv", "status": "ok", "fetched": 8, "accepted": 5, "filtered": 3, "new": 2, "duplicates": 3, "raw_secret": "omit", "variants": [{"id": "primary", "query": "vision reasoning", "kind": "原始问题", "fetched": 8, "accepted": 5, "private_payload": "omit"}]}]
        with app.db_session() as db:
            db.execute("INSERT INTO runs(id,query_name,status,started_at,completed_at,new_count,duplicate_count,sources_json,source_details_json,error) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, "Vision", "success", app.now_iso(), app.now_iso(), 2, 3, '["arxiv"]', json.dumps(detail), ""))
        result = app.run_detail(run_id)
        self.assertEqual(result["source_details"][0]["fetched"], 8)
        self.assertEqual(result["source_details"][0]["variants"][0]["query"], "vision reasoning")
        self.assertNotIn("raw_secret", json.dumps(result))
        self.assertNotIn("private_payload", json.dumps(result))

    def test_analytics_summary_uses_persisted_run_source_details(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: ([
            {"title": "Analytics run paper", "abstract": "multimodal reasoning", "year": 2026,
             "arxiv_id": "2601.09999v1", "source": "arxiv", "external_id": "2601.09999v1"},
            {"title": "Filtered old paper", "abstract": "multimodal reasoning", "year": 2018,
             "arxiv_id": "1801.09999v1", "source": "arxiv", "external_id": "1801.09999v1"},
        ] if offset == 0 else [])
        try:
            result = app.run_query("multimodal reasoning", ["arxiv"], 10, options={"year_from": 2025, "expand_concepts": False})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(result["sources"][0]["fetched"], 2)
        summary = app.analytics_summary()
        self.assertEqual(summary["retrieval_funnel"]["fetched"], 2)
        self.assertEqual(summary["retrieval_funnel"]["filtered"], 1)
        self.assertEqual(summary["retrieval_funnel"]["accepted"], 1)
        self.assertEqual(summary["data_quality"]["status"], "complete")
        with app.db_session() as db:
            row = db.execute("SELECT source_details_json FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        details = json.loads(row[0])
        self.assertEqual(details[0]["source"], "arxiv")
        self.assertEqual(details[0]["accepted"], 1)

    def test_evaluation_summary_uses_explicit_feedback_and_precision_at_k(self):
        query_id = "evaluation-fixture"
        now = app.now_iso()
        paper_ids = []
        with app.db_session() as db:
            for index, decision in enumerate(("relevant", "irrelevant", "relevant", "irrelevant", "relevant")):
                paper_id = f"evaluation-paper-{index}"
                paper_ids.append(paper_id)
                db.execute("INSERT INTO papers(id,title,title_key,created_at,updated_at) VALUES(?,?,?,?,?)", (paper_id, f"Evaluation {index}", f"evaluation {index}", now, now))
                db.execute("""INSERT INTO query_matches(query_id,paper_id,first_seen_at,last_seen_at,seen_count,first_run_id,last_run_id,score,tier,explanation_json,score_version,user_decision,viewed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (query_id, paper_id, now, now, 1, "run", "run", 100 - index, "高相关", "[]", "test", decision, now))
        evaluation = app.evaluation_summary()
        self.assertEqual(evaluation["status"], "ready")
        self.assertEqual(evaluation["judged_matches"], 5)
        self.assertEqual(evaluation["precision_at_5"], 0.6)
        self.assertEqual(evaluation["precision_at_10"], 0.6)
        self.assertEqual(evaluation["judged_precision"], 0.6)

    def test_health_and_audit_are_operator_safe(self):
        paper_id, _ = app.upsert_paper({"title": "Health fixture", "year": 2026}, source="manual")
        health = app.health_summary()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["ready"])
        self.assertEqual(health["database"]["schema_version"], "9")
        self.assertGreaterEqual(health["database"]["audit_events"], 1)
        with app.db_session() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE entity_id=?", (paper_id,))]
        self.assertTrue(rows)
        self.assertNotIn("api_key", rows[0]["payload_json"])

    def test_automatic_backup_policy_creates_and_rotates_only_auto_archives(self):
        app.save_backup_policy({"enabled": True, "interval_hours": 24, "retention": 1})
        first = app.create_backup_archive(automatic=True)
        second = app.create_backup_archive(automatic=True)
        self.assertTrue(first["manifest"]["automatic"])
        self.assertTrue(second["manifest"]["automatic"])
        automatic_archives = list(app.BACKUP_DIR.glob("researchdesk-auto-*.zip"))
        self.assertEqual(len(automatic_archives), 1)
        self.assertIn(automatic_archives[0].name, {first["filename"], second["filename"]})
        manual = app.create_backup_archive()
        self.assertFalse(manual["manifest"]["automatic"])
        self.assertTrue((app.BACKUP_DIR / manual["filename"]).exists())
        self.assertEqual(app.get_backup_policy()["retention"], 1)
        policy = app.get_backup_policy()
        policy["next_run_at"] = "2000-01-01T00:00:00+00:00"
        with app.db_session() as db:
            db.execute("UPDATE app_meta SET value=? WHERE key='backup_policy'", (json.dumps(policy),))
        app.scheduled_backup_tick()
        refreshed = app.get_backup_policy()
        self.assertTrue(refreshed["last_success_at"])
        self.assertGreater(len(list(app.BACKUP_DIR.glob("researchdesk-auto-*.zip"))), 0)
        self.assertTrue((app.BACKUP_DIR / manual["filename"]).exists())

    def test_research_memory_ranks_related_unread_papers_and_honors_feedback(self):
        query_id = "memory-profile-test"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", (query_id, "多模态推理", "multimodal reasoning", '["openalex"]', 168, now, now, now))
        anchor, _ = app.upsert_paper({"title": "Multimodal Reasoning with Visual Evidence", "abstract": "multimodal reasoning visual evidence", "status": "read", "topic": "多模态推理"}, source="manual")
        candidate, _ = app.upsert_paper({"title": "Efficient Multimodal Reasoning", "abstract": "multimodal reasoning for agents", "status": "to_read", "topic": "多模态推理"}, source="openalex")
        unrelated, _ = app.upsert_paper({"title": "Soil Chemistry Field Survey", "abstract": "agriculture soil chemistry", "status": "to_read", "topic": "农业"}, source="manual")
        with app.db_session() as db:
            db.execute("INSERT INTO query_matches(query_id,paper_id,first_seen_at,last_seen_at,seen_count,first_run_id,last_run_id,score,tier,explanation_json,score_version,user_decision,viewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (query_id, candidate, now, now, 1, "run", "run", 80, "高相关", "[]", "test", "new", ""))
        memory = app.research_memory()
        self.assertEqual(memory["recommendations"][0]["id"], candidate)
        with app.db_session() as db:
            db.execute("INSERT INTO learning_feedback(paper_id,scope,decision,note,created_at) VALUES(?,?,?,?,?)", (candidate, "global", "irrelevant", "not my direction", now))
        after = app.research_memory()
        self.assertNotIn(candidate, [item["id"] for item in after["recommendations"]])
        self.assertIn(unrelated, [item["id"] for item in after["recommendations"]])

    def test_paper_connections_explain_topics_and_related_library(self):
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", ("connection-query", "视觉推理", "visual reasoning", '["arxiv"]', 168, now, now, now))
        paper_id, _ = app.upsert_paper({"title": "Visual Reasoning with Evidence", "abstract": "visual reasoning evidence", "status": "to_read"}, source="manual")
        related_id, _ = app.upsert_paper({"title": "Evidence for Visual Reasoning", "abstract": "visual reasoning benchmark", "status": "read"}, source="manual")
        result = app.paper_connections(paper_id)
        self.assertEqual(result["topics"][0]["tier"], "高相关")
        self.assertEqual(result["related"][0]["id"], related_id)

    def test_openalex_connector_normalizes_inverted_abstract(self):
        original = app.http_json
        app.http_json = lambda _url, timeout=20: {"results": [{"id": "https://openalex.org/W1", "title": "OpenAlex fixture", "publication_year": 2026, "doi": "https://doi.org/10.1000/fixture", "abstract_inverted_index": {"reasoning": [1], "multimodal": [0]}, "authorships": [{"author": {"display_name": "A Researcher"}}], "primary_location": {"source": {"display_name": "Test Venue"}}}]}
        try:
            rows = app.search_openalex("multimodal", 10)
        finally:
            app.http_json = original
        self.assertEqual(rows[0]["abstract"], "multimodal reasoning")
        self.assertEqual(rows[0]["doi"], "10.1000/fixture")

    def test_local_http_crud_and_backup_restore_round_trip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, method="GET", value=None):
            data = json.dumps(value).encode("utf-8") if value is not None else None
            req = Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=20) as response:
                return response.headers, response.read()

        try:
            headers, body = request("/static/app.js")
            self.assertIn("javascript", headers.get("Content-Type", ""))

            headers, body = request("/api/papers", "POST", {"title": "Integration Paper", "year": 2026, "topic": "multimodal"})
            self.assertTrue(headers.get("X-Request-ID"))
            paper = json.loads(body)
            pid = paper["id"]
            export_headers, export_body = request("/api/export/json", "GET")
            self.assertIn("application/json", export_headers.get("Content-Type", ""))
            export_payload = json.loads(export_body)
            self.assertEqual(export_payload["schema_version"], 9)
            self.assertIn("audit_events", export_payload)
            _, card_body = request(f"/api/papers/{pid}/reading-card", "PUT", {"research_question": "Which visual evidence supports the claim?", "method": "Evidence-conditioned reasoning"})
            self.assertEqual(json.loads(card_body)["completed_fields"], 2)
            _, batch_body = request("/api/papers/batch", "POST", {"ids": [pid], "updates": {"is_favorite": True}})
            self.assertEqual(json.loads(batch_body)["updated"], 1)
            _, card_read_body = request(f"/api/papers/{pid}/reading-card")
            self.assertEqual(json.loads(card_read_body)["method"], "Evidence-conditioned reasoning")
            with app.db_session() as db:
                db.execute("INSERT INTO runs(id,query_name,status,started_at,completed_at,new_count,duplicate_count,sources_json,source_details_json) VALUES(?,?,?,?,?,?,?,?,?)", ("http-run-fixture", "HTTP test", "success", app.now_iso(), app.now_iso(), 1, 0, '["arxiv"]', '[{"source":"arxiv","status":"ok","fetched":1,"accepted":1,"filtered":0,"new":1,"duplicates":0,"variants":[]}]'))
            _, run_body = request("/api/runs/http-run-fixture")
            self.assertEqual(json.loads(run_body)["source_details"][0]["accepted"], 1)
            with app.db_session() as db:
                db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("http-ai-artifact", pid, "reading_card_draft", "fixture", "fixture-model", "fulltext-reading-card-v1", "c" * 64, json.dumps({"analysis": {"reading_card": {"method": "fixture"}}}, ensure_ascii=False), app.now_iso()))
            export_payload = json.loads(request("/api/export/json", "GET")[1])
            self.assertEqual(export_payload["reading_cards"][0]["paper_id"], pid)
            _, import_body = request("/api/import", "POST", {"format": "json", "content": json.dumps(export_payload)})
            self.assertEqual(json.loads(import_body)["reading_cards"], 1)
            self.assertEqual(json.loads(import_body)["ai_artifacts"], 0)  # Re-import is idempotent for saved analyses.
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_artifacts WHERE paper_id=?", (pid,)).fetchone()[0], 1)
            _, evaluation_body = request("/api/evaluation", "GET")
            self.assertIn("precision_at_5", json.loads(evaluation_body))
            _, metrics_body = request("/api/metrics", "GET")
            metrics = json.loads(metrics_body)
            self.assertIn("p95_ms", metrics)
            self.assertIn("top_routes", metrics)
            request(f"/api/papers/{pid}", "PATCH", {"status": "reading", "notes": "check the baseline"})
            _, body = request("/api/papers?q=Integration", "GET")
            saved = json.loads(body)[0]
            self.assertEqual(saved["status"], "reading")
            self.assertEqual(saved["notes"], "check the baseline")
            _, library_body = request("/api/library?q=baseline&limit=1", "GET")
            library_payload = json.loads(library_body)
            self.assertEqual(library_payload["total"], 1)
            self.assertEqual(library_payload["items"][0]["id"], pid)
            _, search_body = request("/api/search?q=baseline&limit=1", "GET")
            self.assertEqual(json.loads(search_body)["results"][0]["id"], pid)
            _, history_body = request(f"/api/papers/{pid}/history", "GET")
            history = json.loads(history_body)["revisions"]
            self.assertTrue(history)
            latest_notes = next(item for item in history if item["revision_type"] == "notes")
            request(f"/api/papers/{pid}/history/{latest_notes['id']}/restore", "POST", {})
            _, maintenance_body = request("/api/maintenance/status", "GET")
            self.assertTrue(json.loads(maintenance_body)["index"]["documents_match_papers"])
            _, policy_body = request("/api/settings/backup-policy", "GET")
            self.assertTrue(json.loads(policy_body)["enabled"])
            _, policy_body = request("/api/settings/backup-policy", "PATCH", {"enabled": False, "interval_hours": 48, "retention": 3})
            self.assertFalse(json.loads(policy_body)["enabled"])
            self.assertEqual(json.loads(policy_body)["interval_hours"], 48)

            _, body = request("/api/backup", "POST", {})
            backup = json.loads(body)
            self.assertEqual(backup["manifest"]["schema_version"], 9)
            _, archive = request(backup["download_url"])
            request("/api/papers", "POST", {"title": "Temporary after backup", "year": 2026})
            request("/api/backup/restore", "POST", {"filename": backup["filename"], "data": base64.b64encode(archive).decode("ascii")})
            _, body = request("/api/papers?q=Temporary%20after%20backup", "GET")
            self.assertEqual(json.loads(body), [])
            _, body = request("/api/papers?q=Integration", "GET")
            self.assertEqual(json.loads(body)[0]["id"], pid)
            _, restored_card = request(f"/api/papers/{pid}/reading-card", "GET")
            self.assertEqual(json.loads(restored_card)["research_question"], "Which visual evidence supports the claim?")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_pdf_extraction_is_page_level_idempotent_and_searchable(self):
        reportlab = __import__("importlib").util.find_spec("reportlab")
        if reportlab is None:
            self.skipTest("reportlab is only used to build the PDF fixture")
        from io import BytesIO
        from reportlab.pdfgen import canvas

        paper_id, _ = app.upsert_paper({"title": "PDF evidence fixture", "year": 2026}, source="manual")
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        long_text = "Visual evidence and multimodal reasoning " * 12
        pdf.drawString(72, 740, long_text)
        pdf.showPage()
        pdf.drawString(72, 740, "short page with benchmark token")
        pdf.save()
        content = buffer.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        app.FILES_DIR.mkdir(parents=True, exist_ok=True)
        stored_name = digest + ".pdf"
        (app.FILES_DIR / stored_name).write_bytes(content)
        attachment_id = "pdf-fixture-attachment"
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", (attachment_id, paper_id, "fixture.pdf", stored_name, digest, len(content), app.now_iso()))

        first = app.extract_paper_documents(paper_id)
        second = app.extract_paper_documents(paper_id)
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["page_count"], second["page_count"])
        self.assertGreaterEqual(first["characters"], 160)
        summary = app.extraction_summary(paper_id)
        self.assertEqual(summary["extracted_attachment_count"], 1)
        self.assertEqual(summary["page_count"], 2)
        self.assertEqual(summary["pages"][0]["good_pages"], 1)
        self.assertEqual(summary["pages"][0]["low_pages"], 1)
        matches = app.search_document_pages(paper_id, "benchmark")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["page_number"], 2)
        self.assertIn("benchmark", matches[0]["snippet"])
        with app.db_session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_pages WHERE paper_id=?", (paper_id,)).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT text_sha256) FROM document_pages WHERE paper_id=?", (paper_id,)).fetchone()[0], 2)

    def test_fulltext_ai_helpers_parse_json_chunk_pages_and_preserve_human_card(self):
        self.assertEqual(app.parse_model_json("```json\n{\"ok\": true}\n```"), {"ok": True})
        with self.assertRaisesRegex(ValueError, "有效 JSON"):
            app.parse_model_json("not json")
        pages = [
            {"page_number": 2, "text": "Page two " * 1200},
            {"page_number": 3, "text": "Page three evidence"},
        ]
        chunks = app.build_page_chunks(pages, max_chars=4000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item["text"]) <= 4000 for item in chunks))
        self.assertIn(2, chunks[0]["pages"])
        self.assertIn("[第 2 页]", chunks[0]["text"])
        merged = app.merge_ai_reading_card({"method": "human method", "research_question": ""}, {"method": "AI method", "research_question": "AI question"})
        self.assertEqual(merged["method"], "human method")
        self.assertEqual(merged["research_question"], "AI question")
        self.assertEqual(app.merge_ai_reading_card({}, {"limitations": "原文未说明"})["limitations"], "")

    def test_multimodal_request_preserves_image_parts_and_page_anchor(self):
        original_settings = app.model_settings
        original_key = app.RUNTIME_MODEL_KEY
        original_open = app.urlopen
        captured = {}
        app.RUNTIME_MODEL_KEY = "fixture-key"
        app.model_settings = lambda: {"enabled": True, "model": "vision-model", "base_url": "https://model.example/v1", "temperature": 0.2}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()
        def fake_open(req, timeout=0):
            captured["body"] = json.loads(req.data)
            captured["timeout"] = timeout
            return Response()
        app.urlopen = fake_open
        try:
            result = app.model_multimodal_json_request("analyze", {"pages": "page text"}, [{"page": 3, "attachment_id": "att", "data_url": "data:image/jpeg;base64,ZmFrZQ=="}])
        finally:
            app.model_settings = original_settings
            app.RUNTIME_MODEL_KEY = original_key
            app.urlopen = original_open
        self.assertEqual(result, {"ok": True})
        parts = captured["body"]["messages"][1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertIn("第 3 页", parts[1]["text"])
        self.assertEqual(parts[2]["type"], "image_url")

    def test_pdf_visual_render_is_cached_and_page_specific(self):
        if __import__("importlib").util.find_spec("fitz") is None:
            self.skipTest("PyMuPDF is optional")
        if __import__("importlib").util.find_spec("reportlab") is None:
            self.skipTest("reportlab is only used to build the PDF fixture")
        from io import BytesIO
        from reportlab.pdfgen import canvas
        paper_id, _ = app.upsert_paper({"title": "Visual PDF fixture"}, source="manual")
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(70, 740, "Equation y = Wx + b")
        pdf.rect(70, 600, 200, 70)
        pdf.drawString(80, 630, "Figure 1")
        pdf.showPage()
        pdf.drawString(70, 740, "Page two with a different formula")
        pdf.save()
        content = buffer.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        app.FILES_DIR.mkdir(parents=True, exist_ok=True)
        stored_name = digest + ".pdf"
        (app.FILES_DIR / stored_name).write_bytes(content)
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("visual-fixture-attachment", paper_id, "visual.pdf", stored_name, digest, len(content), app.now_iso()))
        attachment = {"id": "visual-fixture-attachment", "stored_name": stored_name, "sha256": digest}
        first = app.render_pdf_page_visual(attachment, 1)
        second = app.render_pdf_page_visual(attachment, 1)
        third = app.render_pdf_page_visual(attachment, 2)
        self.assertTrue(first["data_url"].startswith("data:image/jpeg;base64,"))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertNotEqual(first["sha256"], third["sha256"])

    def test_visual_evidence_requires_a_rendered_page_and_text_quotes_still_verify(self):
        analysis = {"evidence_spans": [{"claim": "bad visual page", "page": 1, "quote": "valid text quote", "support": "direct"}], "visual_evidence": [
            {"claim": "not rendered", "type": "formula", "attachment_id": "not-rendered", "page": 1, "description": "invented"},
            {"claim": "rendered formula", "type": "formula", "attachment_id": "att", "page": 1, "description": "equation y=Wx", "transcription_latex": "y=Wx"},
        ], "uncertainties": []}
        app.validate_ai_evidence(analysis, [{"attachment_id": "att", "page_number": 1, "text": "valid text quote on this page"}], {("att", 1)})
        self.assertEqual(len(analysis["visual_evidence"]), 1)
        self.assertEqual(analysis["visual_evidence"][0]["claim"], "rendered formula")
        self.assertEqual(analysis["evidence_spans"], [{"claim": "bad visual page", "page": 1, "quote": "valid text quote", "support": "direct"}])

    def test_ai_artifact_evidence_verification_discards_fabricated_page_or_quote(self):
        analysis = {"evidence_spans": [
            {"claim": "valid", "page": 1, "quote": "The benchmark contains 10 classes."},
            {"claim": "bad page", "page": 99, "quote": "invented quote"},
        ], "uncertainties": []}
        app.validate_ai_evidence(analysis, [{"page_number": 1, "text": "Results: The benchmark contains 10 classes."}])
        self.assertEqual(len(analysis["evidence_spans"]), 1)
        self.assertIn("已移除 1 条", analysis["uncertainties"][0])

    def test_json_import_migrates_ai_analysis_history_with_paper_identity(self):
        class CaptureHandler:
            def send_json(self, value, status=200, headers=None):
                return value
        now = app.now_iso()
        portable = {
            "papers": [{"id": "exported-paper-id", "title": "Portable AI analysis paper", "year": 2026}],
            "ai_artifacts": [{
                "paper_id": "exported-paper-id", "artifact_type": "deep_read", "provider": "https://model.example/v1",
                "model": "fixture-model", "prompt_version": "deep-read-v1", "input_sha256": "d" * 64,
                "answer": json.dumps({"analysis": {"overview": "portable"}, "meta": {"page_count": 4}}, ensure_ascii=False), "created_at": now,
            }],
        }
        result = app.Handler.import_data(CaptureHandler(), {"format": "json", "content": json.dumps(portable, ensure_ascii=False)})
        self.assertEqual(result["ai_artifacts"], 1)
        with app.db_session() as db:
            row = db.execute("SELECT paper_id,artifact_type,prompt_version,answer FROM ai_artifacts").fetchone()
        self.assertEqual(row["artifact_type"], "deep_read")
        self.assertNotEqual(row["paper_id"], "exported-paper-id")
        self.assertEqual(json.loads(row["answer"])["analysis"]["overview"], "portable")

    def test_fulltext_ai_pipeline_persists_page_hash_and_structured_deep_read(self):
        paper_id, _ = app.upsert_paper({"title": "Deep read pipeline fixture", "topic": "multimodal reasoning"}, source="manual")
        original_pages = app.get_paper_pages_for_ai
        original_model_call = app.model_json_request
        rows = [{"page_number": 1, "text": "The method uses visual evidence. Results show 10% improvement.", "char_count": 62, "quality": "good", "text_sha256": "a" * 64}]
        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Deep read pipeline fixture", "authors": "A. Author", "year": 2026, "venue": "Fixture", "topic": "multimodal reasoning"}, rows)
        calls = []
        def fake_model_call(prompt, payload, timeout=120):
            calls.append(payload)
            if "证据抽取器" in prompt:
                return {"notes": [{"category": "method", "finding": "Uses visual evidence", "evidence_spans": [{"page": 1, "quote": "The method uses visual evidence.", "support": "direct"}]}], "evidence_spans": [], "uncertainties": []}
            return {
                "overview": "The paper tests visual evidence.", "research_question": "How does visual evidence help?", "motivation": "Multimodal reasoning",
                "contributions": ["Evidence-aware method"], "method": {"pipeline": "Use visual evidence", "components": [], "assumptions": [], "training_or_inference": "Inference"},
                "datasets": [], "experiments": [], "metrics": [], "main_findings": ["10% improvement"], "limitations": [],
                "reproducibility": {"code": "原文未说明", "data": "原文未说明", "environment": "原文未说明", "random_seed": "原文未说明", "missing_information": []},
                "critical_reading": {"strong_points": [], "weak_points": [], "possible_confounders": [], "questions_for_author": []},
                "evidence_spans": [{"claim": "The method uses visual evidence", "page": 1, "quote": "The method uses visual evidence.", "support": "direct"}],
                "uncertainties": [], "reading_card": {field: "原文未说明" for field in app.READING_CARD_FIELDS},
            }
        app.model_json_request = fake_model_call
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai = original_pages
            app.model_json_request = original_model_call
        self.assertEqual(result["analysis"]["evidence_spans"][0]["page"], 1)
        self.assertEqual(result["analysis"]["evidence_spans"][0]["quote"], "The method uses visual evidence.")
        self.assertGreaterEqual(len(calls), 2)
        with app.db_session() as db:
            artifact = db.execute("SELECT artifact_type,prompt_version,input_sha256,answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(artifact["artifact_type"], "deep_read")
        self.assertEqual(artifact["prompt_version"], "deep-read-v1")
        self.assertEqual(len(artifact["input_sha256"]), 64)
        self.assertIn('"page_count": 1', artifact["answer"])
        self.assertIn("10% improvement", artifact["answer"])

    def test_fulltext_ai_http_consent_model_gate_history_and_apply_keep_human_fields(self):
        paper_id, _ = app.upsert_paper({"title": "AI fulltext fixture"}, source="manual")
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, method="GET", value=None):
            data = json.dumps(value).encode("utf-8") if value is not None else None
            req = Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except Exception as exc:
                if hasattr(exc, "code"):
                    return exc.code, json.loads(exc.read())
                raise
        try:
            status, _ = request(f"/api/papers/{paper_id}/ai-reading-card", "POST", {"consent": False})
            self.assertEqual(status, 400)
            app.RUNTIME_MODEL_KEY = ""
            status, error = request(f"/api/papers/{paper_id}/ai-deep-read", "POST", {"consent": True})
            self.assertEqual(status, 503)
            self.assertIn("API Key", error["error"])

            with app.db_session() as db:
                draft = {"reading_card": {field: f"AI {field}" for field in app.READING_CARD_FIELDS}, "evidence": [], "uncertainties": []}
                artifact_id = "test-reading-card-artifact"
                db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "reading_card_draft", "fixture", "fixture-model", "fulltext-reading-card-v1", "f" * 64, json.dumps({"analysis": draft, "meta": {"page_count": 1}}, ensure_ascii=False), app.now_iso()))
            app.save_model_settings({"provider": "openai", "base_url": "https://example.test/v1", "model": "fixture-model", "temperature": 0.2, "enabled": True, "api_key": "test-key"})
            with app.db_session() as db:
                app.save_reading_card(db, paper_id, {"method": "human method", "contribution": "human contribution"})
            status, applied = request(f"/api/papers/{paper_id}/apply-ai-reading-card", "POST", {"artifact_id": artifact_id, "merge_empty": True})
            self.assertEqual(status, 200)
            self.assertEqual(applied["reading_card"]["method"], "human method")
            self.assertEqual(applied["reading_card"]["contribution"], "human contribution")
            self.assertEqual(applied["reading_card"]["research_question"], "AI research_question")
            status, history = request(f"/api/papers/{paper_id}/ai-analysis")
            self.assertEqual(status, 200)
            self.assertEqual(history["analyses"][0]["artifact_type"], "reading_card_draft")
            self.assertEqual(history["analyses"][0]["analysis"]["reading_card"]["method"], "AI method")
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_artifacts WHERE id=? AND answer LIKE '%test-key%'", (artifact_id,)).fetchone()[0], 0)

            generated_paper_id, _ = app.upsert_paper({"title": "Automatic reading-card apply fixture"}, source="manual")
            with app.db_session() as db:
                app.save_reading_card(db, generated_paper_id, {"method": "human method"})
            original_analysis = app.run_fulltext_ai_analysis
            ai_draft = {"reading_card": {field: f"generated {field}" for field in app.READING_CARD_FIELDS}, "evidence": []}
            app.run_fulltext_ai_analysis = lambda *_args, **_kwargs: {"draft": ai_draft, "analysis": ai_draft, "artifact_id": "generated-artifact", "model": "fixture-model", "created_at": app.now_iso()}
            try:
                status, generated = request(f"/api/papers/{generated_paper_id}/ai-reading-card", "POST", {"consent": True})
                self.assertEqual(status, 200)
                self.assertTrue(generated["auto_applied"])
                self.assertEqual(generated["applied"]["method"], "human method")
                self.assertEqual(generated["applied"]["research_question"], "generated research_question")
                status, draft_only = request(f"/api/papers/{generated_paper_id}/ai-reading-card", "POST", {"consent": True, "apply": False})
            finally:
                app.run_fulltext_ai_analysis = original_analysis
            self.assertEqual(status, 200)
            self.assertFalse(draft_only["auto_applied"])
            self.assertIsNone(draft_only["applied"])
            source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
            self.assertIn("consent:true,apply:true,merge_empty:true", source)
        finally:
            app.RUNTIME_MODEL_KEY = ""
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_schema_migrates_from_v5_to_v9_and_adds_search_and_history(self):
        with app.db_session() as db:
            db.execute("UPDATE app_meta SET value='5' WHERE key='schema_version'")
        app.init_db()
        with app.db_session() as db:
            version = db.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
            columns = [row[1] for row in db.execute("PRAGMA table_info(document_pages)").fetchall()]
            query_columns = [row[1] for row in db.execute("PRAGMA table_info(saved_queries)").fetchall()]
            run_columns = [row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()]
            card_columns = [row[1] for row in db.execute("PRAGMA table_info(reading_cards)").fetchall()]
            history_columns = [row[1] for row in db.execute("PRAGMA table_info(paper_revisions)").fetchall()]
            search_columns = [row[1] for row in db.execute("PRAGMA table_info(paper_search_content)").fetchall()]
        self.assertEqual(version, "9")
        self.assertIn("text_sha256", columns)
        self.assertIn("search_options_json", query_columns)
        self.assertIn("source_details_json", run_columns)
        self.assertIn("paper_id", card_columns)
        self.assertIn("content_json", history_columns)
        self.assertIn("pdf_text", search_columns)

    def test_federated_search_filters_and_ranks_across_new_source(self):
        original = app.SOURCE_SEARCH.get("europepmc")
        app.SOURCE_SEARCH["europepmc"] = lambda _query, _limit, offset=0: [
            {"title": "Multimodal reasoning in clinical imaging", "authors": "A Researcher", "year": 2026, "abstract": "multimodal reasoning for clinical imaging", "doi": "10.1000/clinical", "venue": "Clinical AI", "external_id": "MED:1", "source": "europepmc"},
            {"title": "Multimodal reasoning older study", "authors": "B Researcher", "year": 2018, "abstract": "multimodal reasoning", "doi": "10.1000/old", "venue": "Clinical AI", "external_id": "MED:2", "source": "europepmc"},
        ] if offset == 0 else []
        try:
            result = app.run_query("multimodal reasoning", ["europepmc"], 10, options={"year_from": 2025, "venue": "Clinical", "sort": "newest"})
        finally:
            if original is None:
                app.SOURCE_SEARCH.pop("europepmc", None)
            else:
                app.SOURCE_SEARCH["europepmc"] = original
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(result["total_filtered"], 1)
        self.assertEqual(result["papers"][0]["year"], 2026)
        self.assertEqual(result["papers"][0]["source_count"], 1)
        self.assertIn("concept_terms", result["plan"])
        self.assertEqual(result["plan"]["filters"]["year_from"], 2025)

    def test_search_query_plan_exposes_source_roles_and_concepts(self):
        plan = app.search_query_plan('"vision language" grounding', ["arxiv", "europepmc"])
        self.assertEqual(plan["exact_phrases"], ["vision language"])
        self.assertIn("vision", plan["concept_terms"])
        self.assertEqual(len(plan["sources"]), 2)
        self.assertIn("role", plan["sources"][1])

    def test_search_query_plan_can_disable_concept_expansion(self):
        plan = app.search_query_plan("multimodal reasoning", ["arxiv"], {"expand_concepts": False})
        self.assertEqual(len(plan["query_variants"]), 1)
        self.assertFalse(plan["filters"]["expand_concepts"])

    def test_chinese_query_plan_bridges_to_english_index_terms(self):
        plan = app.search_query_plan("多模态推理", ["arxiv"])
        queries = [item["query"] for item in plan["query_variants"]]
        self.assertIn("multimodal reasoning", queries)
        self.assertTrue(any("multimodal inference" in item for item in queries))

    def test_search_variants_expand_recall_and_keep_hit_paths(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []

        def fake_search(query, _limit, offset=0):
            calls.append(query)
            if offset:
                return []
            return [{
                "title": "Multimodal reasoning fixture",
                "abstract": f"Found through {query}",
                "year": 2026,
                "doi": "10.1000/multimodal-fixture",
                "source": "arxiv",
                "external_id": query,
            }]

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            result = app.run_query("multimodal reasoning", ["arxiv"], 10)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertGreater(len(calls), 1)
        self.assertEqual(result["accepted_count"], 1)
        self.assertGreaterEqual(len(result["papers"][0]["discovery_paths"]), 2)
        self.assertGreater(result["duplicate_count"], 0)
        self.assertGreater(result["plan"]["expansion_count"], 0)

    def test_deep_search_plan_exposes_facets_and_discovery_lanes(self):
        plan = app.search_query_plan("多模态推理 基准", ["arxiv", "openalex"], {"search_depth": "deep", "discovery_mode": "explore"})
        self.assertEqual(plan["filters"]["search_depth"], "deep")
        self.assertIn("评价", plan["facets"])
        queries = [item["query"] for item in plan["query_variants"]]
        self.assertTrue(any("survey" in item for item in queries))
        self.assertTrue(any("benchmark" in item for item in queries))
        self.assertIn("综述/基准/实证入口", plan["coverage_layers"])

    def test_foundations_sort_uses_citation_and_open_access_signals(self):
        original = app.SOURCE_SEARCH["openalex"]
        app.SOURCE_SEARCH["openalex"] = lambda _query, _limit, offset=0: [] if offset else [
            {"title": "New but lightly cited fixture", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/new", "citation_count": 2, "open_access": True, "source": "openalex", "external_id": "WNEW"},
            {"title": "Foundational reasoning fixture", "abstract": "multimodal reasoning", "year": 2020, "doi": "10.1000/classic", "citation_count": 400, "open_access": False, "source": "openalex", "external_id": "WCLASSIC"},
        ]
        try:
            result = app.run_query("multimodal reasoning", ["openalex"], 10, options={"expand_concepts": False, "sort": "foundations"})
        finally:
            app.SOURCE_SEARCH["openalex"] = original
        self.assertEqual(result["papers"][0]["doi"], "10.1000/classic")
        self.assertEqual(result["papers"][0]["citation_count"], 400)
        self.assertGreaterEqual(result["summary"]["with_citations"], 2)

    def test_suppress_seen_keeps_long_running_topics_incremental(self):
        query_id = "incremental-topic"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)", (query_id, "增量主题", "multimodal reasoning", '["arxiv"]', 168, now, now, now))
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{"title": "Incremental fixture", "abstract": "multimodal reasoning", "year": 2026, "arxiv_id": "2604.00001", "source": "arxiv", "external_id": "2604.00001"}]
        try:
            first = app.run_query("multimodal reasoning", ["arxiv"], 10, query_id=query_id, options={"expand_concepts": False})
            second = app.run_query("multimodal reasoning", ["arxiv"], 10, query_id=query_id, options={"expand_concepts": False, "suppress_seen": True})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(first["papers"]), 1)
        self.assertEqual(len(second["papers"]), 0)
        self.assertEqual(second["summary"]["new_to_topic"], 0)

    def test_openalex_relation_expansion_adds_cited_and_related_candidates(self):
        seed_id, _ = app.upsert_paper({"title": "Seed relation fixture", "doi": "10.1000/seed"}, source="manual")
        original = app.http_json

        def fake_json(url, timeout=20):
            if "10.1000%2Fseed" in url or url.endswith("/WSEED"):
                return {"id": "https://openalex.org/WSEED", "referenced_works": ["https://openalex.org/WREF"], "related_works": ["https://openalex.org/WREL"], "cited_by_api_url": "https://api.openalex.org/works?filter=cites:WSEED"}
            if "filter=cites:WSEED" in url:
                return {"results": [{"id": "https://openalex.org/WCITED"}]}
            key = url.rstrip("/").split("/")[-1]
            return {"id": "https://openalex.org/" + key, "title": "Expanded " + key, "publication_year": 2026, "doi": "10.1000/" + key.lower(), "authorships": [], "primary_location": {"source": {"display_name": "Fixture Venue"}}, "abstract_inverted_index": {"reasoning": [0]}}

        app.http_json = fake_json
        try:
            result = app.expand_openalex_relations(seed_id, limit=10)
        finally:
            app.http_json = original
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["created"], 3)
        self.assertEqual({item["relation"] for item in result["candidates"]}, {"引用", "相似", "被引用"})

    def test_multilingual_bridge_is_used_by_relevance_scoring(self):
        scored = app.score_relevance({
            "title": "Multimodal reasoning with visual evidence",
            "abstract": "We study multimodal inference for visual question answering.",
            "authors": "",
            "doi": "10.1000/bridge",
        }, "多模态推理", [], [])
        self.assertGreaterEqual(scored["score"], 35)
        self.assertIn("桥接", "；".join(scored["reasons"]))

    def test_dblp_connector_normalizes_authors_and_doi(self):
        original = app.http_json
        app.http_json = lambda _url, timeout=25: {"result": {"hits": {"hit": [{
            "info": {
                "title": "A DBLP Fixture.",
                "authors": {"author": [{"text": "Alice Researcher"}, {"text": "Bob Builder"}]},
                "year": "2026", "venue": "NeurIPS", "doi": "https://doi.org/10.1000/dblp",
                "url": "https://dblp.org/rec/conf/neurips/fixture", "type": "Conference and Workshop Papers",
            }
        }]}}}
        try:
            rows = app.search_dblp("multimodal reasoning", 10)
        finally:
            app.http_json = original
        self.assertEqual(rows[0]["title"], "A DBLP Fixture")
        self.assertEqual(rows[0]["authors"], "Alice Researcher, Bob Builder")
        self.assertEqual(rows[0]["doi"], "10.1000/dblp")
        self.assertEqual(rows[0]["source"], "dblp")

    def test_diagnostics_is_safe_and_data_backed(self):
        app.upsert_paper({"title": "Diagnostics fixture", "year": 2026, "abstract": ""}, source="manual")
        result = app.diagnostics_summary()
        self.assertEqual(result["release"]["schema_version"], "9")
        self.assertGreaterEqual(result["data_quality"]["papers"], 1)
        self.assertIn("openalex", result["connectors"])
        self.assertIn("p95_ms", result["slo"])
        self.assertNotIn("api_key", json.dumps(result, ensure_ascii=False).lower())

    def test_research_briefing_is_local_and_actionable(self):
        app.upsert_paper({"title": "Briefing fixture", "year": 2026, "abstract": "multimodal reasoning", "topic": "多模态"}, source="manual")
        result = app.research_briefing(days=7, limit=3)
        self.assertEqual(result["window_days"], 7)
        self.assertGreaterEqual(result["summary"]["new_papers"], 1)
        self.assertTrue(result["recent_papers"])
        self.assertIn("next_reads", result)
        self.assertNotIn("notes", json.dumps(result, ensure_ascii=False))

    def test_relation_expansion_uses_local_library_seed(self):
        app.upsert_paper({"title": "Multimodal reasoning with visual evidence", "abstract": "multimodal reasoning", "year": 2025}, source="manual")
        plan = app.search_query_plan("multimodal reasoning", ["openalex"], {"expand_relations": True, "expand_concepts": False})
        self.assertTrue(any(item["lane"] == "relations" for item in plan["query_variants"]))
        self.assertIn("馆藏论文邻域", plan["coverage_layers"])

    def test_exclude_terms_filter_candidates_before_dedup(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [
            {"title": "Useful multimodal reasoning", "abstract": "multimodal reasoning", "year": 2026, "arxiv_id": "2605.00001"},
            {"title": "Multimodal reasoning survey", "abstract": "survey of multimodal reasoning", "year": 2026, "arxiv_id": "2605.00002"},
        ]
        try:
            result = app.run_query("multimodal reasoning", ["arxiv"], 10, options={"expand_concepts": False, "exclude_terms": "survey"})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(result["accepted_count"], 1)
        self.assertNotIn("survey", result["papers"][0]["title"].lower())

    def test_openreview_connector_normalizes_note_shape(self):
        original = app.http_json
        app.http_json = lambda _url, timeout=25: {"notes": [{
            "id": "note-1", "forum": "forum-1", "cdate": 1790000000000,
            "content": {"title": {"value": "OpenReview Fixture"}, "abstract": {"value": "A review fixture."}, "authors": {"value": ["Alice", "Bob"]}, "venue": {"value": "NeurIPS"}},
        }]}
        try:
            rows = app.search_openreview("multimodal", 10)
        finally:
            app.http_json = original
        self.assertEqual(rows[0]["title"], "OpenReview Fixture")
        self.assertEqual(rows[0]["authors"], "Alice, Bob")
        self.assertEqual(rows[0]["source"], "openreview")
        self.assertTrue(rows[0]["open_access"])

    def test_rank_fusion_and_near_title_dedup_keep_best_metadata(self):
        records, duplicates = app.merge_search_records([
            {"title": "A Robust Method: for Vision", "year": 2025, "abstract": "short"},
            {"title": "A robust method for vision", "year": 2025, "abstract": "long abstract with evidence", "doi": "10.1000/fusion"},
        ])
        self.assertEqual(len(records), 1)
        self.assertGreaterEqual(duplicates, 1)
        self.assertEqual(records[0]["doi"], "10.1000/fusion")
        self.assertGreater(app.rank_fusion_score({"rrf": [{"rank": 1, "weight": 1.0}, {"rank": 2, "weight": 1.0}]}), app.rank_fusion_score({"rrf": [{"rank": 10, "weight": 1.0}]}))

    def test_agent_discovery_plan_has_safe_local_fallback_without_key(self):
        plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore"})
        self.assertFalse(plan["ai_expansion"]["enabled"])
        self.assertTrue(plan["query_variants"])
        self.assertNotIn("api_key", json.dumps(plan, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
