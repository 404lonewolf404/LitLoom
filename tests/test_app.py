import base64
import io
import json
import hashlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ["LITLOOM_DATABASE_URL"] = ""
import app


class LiteratureDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self._env_api_key = os.environ.pop("LIT_AGENT_API_KEY", None)
        self._env_api_key_file = os.environ.pop("LIT_AGENT_API_KEY_FILE", None)
        app.DATA_DIR = root / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.RUNTIME_MODEL_KEY = ""
        app.init_db()

    def tearDown(self):
        if self._env_api_key is not None:
            os.environ["LIT_AGENT_API_KEY"] = self._env_api_key
        if self._env_api_key_file is not None:
            os.environ["LIT_AGENT_API_KEY_FILE"] = self._env_api_key_file
        self.temp.cleanup()

    def _start_api_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_port}"

    def _api_json(self, base, path, method="GET", value=None, expected_status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8") if value is not None else None
        request = Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            allowed = expected_status if isinstance(expected_status, (tuple, list, set)) else (expected_status,)
            self.assertIn(response.status, allowed)
            return json.loads(response.read().decode("utf-8"))

    def _wait_for_triage(self, base, run_id, timeout=4):
        deadline = time.monotonic() + timeout
        latest = {}
        while time.monotonic() < deadline:
            payload = self._api_json(base, f"/api/runs/{quote(run_id, safe='')}/triage")
            latest = payload.get("triage", payload)
            if latest.get("status") not in {"queued", "running"}:
                return latest
            time.sleep(0.025)
        self.fail(f"AI candidate triage did not settle: {latest}")

    def _wait_for_run_results(self, base, run_id, timeout=4):
        deadline = time.monotonic() + timeout
        latest = {}
        while time.monotonic() < deadline:
            latest = self._api_json(base, f"/api/runs/{quote(run_id, safe='')}/results", expected_status=(200, 202))
            if latest.get("status") not in {"queued", "running"}:
                return latest
            time.sleep(0.025)
        self.fail(f"Discovery rerun did not settle: {latest}")

    def test_candidate_raw_is_compact_and_non_recursive(self):
        nested = {
            "title": "Food volume estimation",
            "abstract": "A" * 100,
            "raw": {"title": "Food volume estimation", "raw": {"raw": {"secret": "should not leak"}}},
            "sources": [{"source": "arxiv", "raw": {"large": "x" * 1000}}],
        }
        compact = app.compact_candidate_record(nested)
        self.assertEqual(compact["raw"]["title"], "Food volume estimation")
        self.assertNotIn("raw", compact["raw"])
        self.assertNotIn("sources", compact["raw"])
        self.assertLess(len(json.dumps(compact["raw"], ensure_ascii=False)), 1000)

    def test_user_search_options_fill_repeat_slots_for_legacy_clients(self):
        """Normal browser searches must not shrink when an old client omitted the fill flag."""
        prepared = app.prepare_user_search_options({"suppress_seen": True, "fill_repeats": False})
        self.assertTrue(prepared["fill_repeats"])
        strict = app.prepare_user_search_options({"suppress_seen": True, "strict_fresh_only": True})
        self.assertNotIn("fill_repeats", strict)

    def test_abstract_brief_formatter_keeps_complete_sections(self):
        result = app._normalize_abstract_brief({
            "research_question": "研究问题完整保留。",
            "method_clues": "方法线索完整保留。",
            "possible_contributions": "可能贡献完整保留。",
            "needs_verification": ["核验项" + "很长" * 180 for _ in range(6)],
            "recommended_sections": ["方法与实验"],
            "uncertainties": ["摘要无法确认"],
        }, "测试论文")
        self.assertIn("适合继续阅读：方法与实验", result)
        self.assertIn("摘要无法确认：摘要无法确认", result)
        self.assertIn("研究问题：", result)
        self.assertIn("方法线索：", result)
        self.assertIn("可能贡献：", result)
        self.assertIn("需要核验：", result)
        self.assertLessEqual(len(result), 600)
        self.assertFalse(result.endswith("支"))

    def test_abstract_brief_persistent_job_saves_and_reuses_model_result_without_pdf(self):
        paper_id, _ = app.upsert_paper({"title": "Async abstract brief fixture", "abstract": "This study estimates food portion size from smartphone images without a fiducial marker."}, source="manual")
        original_settings, original_key, original_request = app.model_settings, app.model_api_key, app.model_json_request
        original_runtime_key = app.RUNTIME_MODEL_KEY
        calls = []
        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "base_url": "https://model.example/v1", "model": "fixture-model"}
        app.model_api_key = lambda: "fixture-key"
        app.RUNTIME_MODEL_KEY = "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: calls.append(1) or {
            "research_question": "Can smartphone images estimate portion size without a fiducial marker?",
            "method_clues": "Estimate portions from smartphone images.",
            "possible_contributions": "A marker-free estimation approach.",
            "needs_verification": ["Metric details"],
            "recommended_sections": ["Methods", "Evaluation"],
            "uncertainties": ["摘要无法确认"],
        }
        try:
            now = app.now_iso()
            job_id = "abstract-brief-persistent-fixture"
            with app.db_session() as db:
                db.execute("INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, paper_id, "abstract_brief", "queued", "queued", "queued", "{}", now, now))
            app.run_ai_job(job_id)
            job = app.get_ai_job(job_id)
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["message"], "摘要助读已生成并保存")
            self.assertEqual(job["progress_current"], job["progress_total"])
            self.assertGreater(job["duration_ms"], 0)
            self.assertTrue(job["artifact_id"])
            self.assertEqual(len(calls), 1)
            reused = app.generate_abstract_brief_analysis(paper_id)
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["artifact_id"], job["artifact_id"])
            self.assertEqual(len(calls), 1)
            with app.db_session() as db:
                artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (job["artifact_id"],)).fetchone()
            self.assertIn("研究问题：", artifact["answer"])
        finally:
            app.model_settings, app.model_api_key, app.model_json_request = original_settings, original_key, original_request
            app.RUNTIME_MODEL_KEY = original_runtime_key

    def test_abstract_brief_keeps_evidence_only_partial_result_when_provider_is_unavailable(self):
        paper_id, _ = app.upsert_paper({
            "title": "Provider outage abstract fixture",
            "abstract": "This study estimates food portion volume from meal images. We evaluate a convolutional model on a held-out dataset.",
        }, source="manual")
        original_settings, original_key, original_request = app.model_settings, app.model_api_key, app.model_json_request
        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "base_url": "https://model.example/v1", "model": "fixture-model", "temperature": 0.2}
        app.model_api_key = lambda: "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(app.HTTPError("https://model.example/v1/chat/completions", 402, "Payment Required", {}, None))
        try:
            result = app.generate_abstract_brief_analysis(paper_id)
        finally:
            app.model_settings, app.model_api_key, app.model_json_request = original_settings, original_key, original_request
        self.assertEqual(result["analysis_status"], "partial")
        self.assertTrue(result["fallback"])
        self.assertIn("本地降级", result["answer"])
        self.assertIn("摘要原文摘录", result["answer"])
        self.assertLessEqual(len(result["answer"]), 600)

    def test_abstract_brief_route_queues_with_consent_without_requiring_pdf(self):
        paper_id, _ = app.upsert_paper({"title": "Async abstract route fixture", "abstract": "An abstract."}, source="manual")
        original_settings, original_key, original_submit = app.model_settings, app.model_api_key, app.submit_ai_job
        original_runtime_key = app.RUNTIME_MODEL_KEY
        submitted = []
        app.model_settings = lambda: {"enabled": True, "model": "fixture", "base_url": "https://model.example/v1"}
        app.model_api_key = lambda: "fixture-key"
        app.RUNTIME_MODEL_KEY = "fixture-key"
        app.submit_ai_job = lambda target, kind, data: submitted.append((target, kind, data)) or {"job": {"id": "brief-job", "paper_id": target, "job_type": kind, "status": "queued"}}
        server, thread, base = self._start_api_server()
        try:
            try:
                self._api_json(base, f"/api/papers/{paper_id}/agent-brief", "POST", {"background": True, "consent": False})
                self.fail("abstract data must require consent")
            except HTTPError as exc:
                self.assertEqual(exc.code, 400)
            queued = self._api_json(base, f"/api/papers/{paper_id}/agent-brief", "POST", {"background": True, "consent": True}, expected_status=202)
            self.assertEqual(queued["job"]["job_type"], "abstract_brief")
            self.assertEqual(submitted[0][1], "abstract_brief")
            self.assertTrue(submitted[0][2]["consent"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.model_settings, app.model_api_key, app.submit_ai_job = original_settings, original_key, original_submit
            app.RUNTIME_MODEL_KEY = original_runtime_key

    def test_background_ai_job_survives_client_disconnect_and_page_reload(self):
        """A closed detail page must not cancel work owned by the server."""
        paper_id, _ = app.upsert_paper({
            "title": "Background survives navigation fixture",
            "abstract": "This study estimates food volume from meal images.",
        }, source="manual")
        original_settings, original_key, original_request = app.model_settings, app.model_api_key, app.model_json_request
        original_runtime_key = app.RUNTIME_MODEL_KEY
        request_started = threading.Event()
        release_request = threading.Event()

        def delayed_model(_prompt, _payload, timeout=120, **_kwargs):
            request_started.set()
            if not release_request.wait(4):
                raise TimeoutError("fixture model request was not released")
            return {
                "research_question": "Can meal images estimate food volume?",
                "method_clues": "Estimate volume from meal images.",
                "possible_contributions": "A food-volume estimation approach.",
                "needs_verification": ["Dataset and metric details"],
                "recommended_sections": ["Methods", "Evaluation"],
                "uncertainties": ["摘要无法确认"],
            }

        app.model_settings = lambda: {
            "enabled": True, "api_key_set": True, "provider": "fixture",
            "base_url": "https://model.example/v1", "model": "fixture-model",
        }
        app.model_api_key = lambda: "fixture-key"
        app.RUNTIME_MODEL_KEY = "fixture-key"
        app.model_json_request = delayed_model
        server, thread, base = self._start_api_server()
        job_id = ""
        try:
            queued = self._api_json(
                base, f"/api/papers/{paper_id}/agent-brief", "POST",
                {"background": True, "consent": True}, expected_status=202,
            )
            job_id = queued["job"]["id"]
            self.assertTrue(request_started.wait(3), "background model request did not start")

            # The POST connection is already closed. Fetching a new page and
            # its state simulates leaving the paper view and reloading LitLoom.
            for path in ("/", "/api/state", "/api/papers?limit=5"):
                with urlopen(base + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
            running = self._api_json(base, f"/api/ai-jobs/{job_id}")
            self.assertEqual(running["status"], "running")

            release_request.set()
            deadline = time.monotonic() + 5
            completed = running
            while time.monotonic() < deadline:
                completed = self._api_json(base, f"/api/ai-jobs/{job_id}")
                if completed.get("status") not in {"queued", "running"}:
                    break
                time.sleep(0.025)
            self.assertEqual(completed["status"], "success", completed.get("error") or completed)
            self.assertTrue(completed.get("artifact_id"))
            history = self._api_json(base, f"/api/papers/{paper_id}/ai-analysis")
            self.assertTrue(any(item.get("artifact_type") == "abstract_brief" for item in history.get("analyses", [])))
        finally:
            release_request.set()
            if job_id:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        latest = app.get_ai_job(job_id)
                    except Exception:
                        latest = None
                    if not latest or latest.get("status") not in {"queued", "running"}:
                        break
                    time.sleep(0.025)
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.model_settings, app.model_api_key, app.model_json_request = original_settings, original_key, original_request
            app.RUNTIME_MODEL_KEY = original_runtime_key

    def test_run_and_sync_job_endpoints_honor_requested_limits(self):
        now = app.now_iso()
        with app.db_session() as db:
            for index in range(3):
                run_id = f"history-limit-run-{index}"
                job_id = f"history-limit-sync-{index}"
                db.execute(
                    "INSERT INTO runs(id,query_id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?,?)",
                    (run_id, "fixture-query", f"Limit fixture {index}", "success", now, now),
                )
                db.execute(
                    "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (job_id, "fixture-query", run_id, "success", "completed", now, now),
                )
        server, thread, base = self._start_api_server()
        try:
            self.assertEqual(len(self._api_json(base, "/api/runs?limit=1")), 1)
            self.assertEqual(len(self._api_json(base, "/api/sync-jobs?limit=2")), 2)
            self.assertEqual(len(self._api_json(base, "/api/sync-jobs")), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reading_card_background_defaults_to_fast_card_template(self):
        paper_id, _ = app.upsert_paper({"title": "Reading card template fixture", "abstract": "A fixture abstract."}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
                ("reading-card-template-attachment", paper_id, "fixture.pdf", "fixture.pdf", "fixture-sha", 100, now),
            )

        class Executor:
            def submit(self, *_args, **_kwargs):
                return None

        original_executor = app.AI_JOB_EXECUTOR
        original_fulltext = app.paper_has_fulltext_source
        app.AI_JOB_EXECUTOR = Executor()
        app.paper_has_fulltext_source = lambda _paper_id: True
        try:
            created = app.submit_ai_job(paper_id, "reading_card", {"consent": True})
            self.assertFalse(created["deduplicated"])
            self.assertEqual(created["job"]["options"]["template"], "fulltext_reading_card")
        finally:
            app.AI_JOB_EXECUTOR = original_executor
            app.paper_has_fulltext_source = original_fulltext

    def test_ai_job_submit_race_marks_queued_row_for_startup_recovery(self):
        """Executor shutdown must not leave an invisible forever-queued job."""
        paper_id, _ = app.upsert_paper({"title": "Executor race fixture", "year": 2026}, source="manual")

        class ClosingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original_executor = app.AI_JOB_EXECUTOR
        original_fulltext = app.paper_has_fulltext_source
        app.AI_JOB_EXECUTOR = ClosingExecutor()
        app.paper_has_fulltext_source = lambda _paper_id: True
        app.STOP_EVENT.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
                app.submit_ai_job(paper_id, "reading_card", {"consent": True, "apply": False})
            with app.db_session() as db:
                row = db.execute("SELECT status,stage,error,message FROM ai_jobs WHERE paper_id=? ORDER BY created_at DESC LIMIT 1", (paper_id,)).fetchone()
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["stage"], "interrupted")
            self.assertEqual(row["error"], "服务重启时任务未完成")
            self.assertIn("下次启动", row["message"])
        finally:
            app.AI_JOB_EXECUTOR = original_executor
            app.paper_has_fulltext_source = original_fulltext
            app.STOP_EVENT.clear()

    def test_running_ai_job_marks_interpreter_shutdown_as_resumable(self):
        paper_id, _ = app.upsert_paper({"title": "Shutdown recovery fixture"}, source="manual")
        now = app.now_iso()
        job_id = "interpreter-shutdown-running-fixture"
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, paper_id, "abstract_brief", "queued", "queued", "queued", "{}", now, now),
            )
        original_generate, original_key = app.generate_abstract_brief_analysis, app.model_api_key
        app.generate_abstract_brief_analysis = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("cannot schedule new futures after interpreter shutdown")
        )
        app.model_api_key = lambda: ""
        try:
            app.run_ai_job(job_id)
            job = app.get_ai_job(job_id)
        finally:
            app.generate_abstract_brief_analysis, app.model_api_key = original_generate, original_key
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["stage"], "interrupted")
        self.assertEqual(job["error"], "服务重启时任务未完成")
        self.assertIn("下次启动", job["message"])

    def test_startup_recovers_legacy_executor_shutdown_failure(self):
        paper_id, _ = app.upsert_paper({"title": "Legacy shutdown recovery fixture"}, source="manual")
        now = app.now_iso()
        job_id = "legacy-interpreter-shutdown-fixture"
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,error,message,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, paper_id, "abstract_brief", "failed", "failed", "cannot schedule new futures after interpreter shutdown", "analysis failed", "{}", now, now, now),
            )
        app.init_db()
        recovered = app.get_ai_job(job_id)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["error"], "服务重启时任务未完成")

        original_submit = app.submit_ai_job
        calls = []
        app.submit_ai_job = lambda target, kind, data: calls.append((target, kind, data)) or {
            "job": {"id": "resumed-shutdown-fixture", "paper_id": target, "job_type": kind, "status": "queued"}
        }
        try:
            resumed = app.resume_interrupted_ai_jobs()
        finally:
            app.submit_ai_job = original_submit
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["interrupted_job_id"], job_id)
        self.assertEqual(calls[0][0:2], (paper_id, "abstract_brief"))
        self.assertEqual(app.get_ai_job(job_id)["error"], "")

    def test_metadata_draft_submit_race_marks_interrupted(self):
        class ClosingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original_executor = app.METADATA_DRAFT_EXECUTOR
        app.METADATA_DRAFT_EXECUTOR = ClosingExecutor()
        app.STOP_EVENT.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
                app.submit_metadata_draft_job({"title": "Metadata executor race"})
            with app.db_session() as db:
                row = db.execute("SELECT status,stage,error,message FROM metadata_draft_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["stage"], "interrupted")
            self.assertEqual(row["error"], "服务重启时任务未完成")
            self.assertIn("下次启动", row["message"])
        finally:
            app.METADATA_DRAFT_EXECUTOR = original_executor
            app.STOP_EVENT.clear()

    def test_pdf_ingest_submit_race_marks_batch_interrupted(self):
        now = app.now_iso()
        job_id = "pdf-executor-race"
        with app.db_session() as db:
            db.execute("INSERT INTO pdf_ingest_jobs(id,status,stage,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?)", (job_id, "queued", "queued", "{}", now, now))

        class ClosingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original_executor = app.PDF_INGEST_EXECUTOR
        app.PDF_INGEST_EXECUTOR = ClosingExecutor()
        app.STOP_EVENT.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
                app.submit_pdf_ingest_job(job_id)
            with app.db_session() as db:
                row = db.execute("SELECT status,stage,error FROM pdf_ingest_jobs WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(tuple(row), ("interrupted", "interrupted", "服务重启时任务未完成"))
        finally:
            app.PDF_INGEST_EXECUTOR = original_executor
            app.STOP_EVENT.clear()

    def test_agent_submit_race_marks_run_interrupted(self):
        goal = app.create_research_goal({"objective": "Agent executor race", "workflow": "research_map", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])

        class ClosingExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        original_executor = app.AGENT_RUN_EXECUTOR
        app.AGENT_RUN_EXECUTOR = ClosingExecutor()
        app.STOP_EVENT.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "服务正在停止"):
                app.approve_agent_run(run["id"], consent_external=False)
            current = app.agent_run_snapshot(run["id"], include_steps=False)
            self.assertEqual(current["status"], "interrupted")
            self.assertEqual(current["error"], "服务重启时任务未完成")
        finally:
            app.AGENT_RUN_EXECUTOR = original_executor
            app.STOP_EVENT.clear()

    def test_research_goal_schedule_patch_resets_next_run_and_can_pause(self):
        goal = app.create_research_goal({
            "objective": "追踪最新的多模态论文",
            "workflow": "auto",
            "sources": ["arxiv"],
        })
        server, thread, base = self._start_api_server()
        try:
            enabled = self._api_json(
                base,
                f"/api/research-goals/{goal['id']}",
                method="PATCH",
                value={"auto_run": True, "cadence_hours": 24},
            )
            self.assertEqual(enabled["auto_run"], 1)
            self.assertEqual(enabled["cadence_hours"], 24)
            self.assertGreater(enabled["next_run_at"], app.now_iso())

            paused = self._api_json(
                base,
                f"/api/research-goals/{goal['id']}",
                method="PATCH",
                value={"auto_run": False},
            )
            self.assertEqual(paused["auto_run"], 0)
            self.assertEqual(paused["next_run_at"], "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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

    def test_discovery_keeps_retrieval_rank_and_ai_screen_labels_separate_after_reload(self):
        """A high retrieval score must not overwrite an abstract-only AI verdict."""
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [{
            "title": "Retrieval and triage separation fixture",
            "abstract": "A paper retrieved for the target research question.",
            "year": 2026,
            "doi": "10.4242/separate-verdicts",
            "external_id": "separate-verdicts",
        }]
        try:
            result = app.run_query(
                "target research question",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        paper = result["papers"][0]
        self.assertEqual(paper["retrieval_relevance_label"], paper["relevance_label"])
        self.assertEqual(paper["retrieval_relevance_score"], paper["relevance_score"])
        self.assertEqual(paper["retrieval_relevance_source"], "rank_fusion_local")
        self.assertNotIn("ai_triage", paper)

        with app.db_session() as db:
            row = db.execute("SELECT record_json FROM discovery_candidates WHERE run_id=? AND id=?", (result["id"], paper["id"])).fetchone()
            record = json.loads(row["record_json"])
            record["ai_triage"] = {"id": paper["id"], "label": "uncertain", "reason": "摘要不足，待核验"}
            record["ai_screening"] = dict(record["ai_triage"])
            db.execute("UPDATE discovery_candidates SET record_json=? WHERE run_id=? AND id=?", (json.dumps(record, ensure_ascii=False), result["id"], paper["id"]))

        restored = app.run_results_snapshot(result["id"])
        restored_paper = restored["papers"][0]
        self.assertEqual(restored_paper["retrieval_relevance_label"], paper["relevance_label"])
        self.assertEqual(restored_paper["ai_triage"]["label"], "uncertain")
        self.assertNotEqual(restored_paper["retrieval_relevance_label"], restored_paper["ai_triage"]["label"])

    def test_discovery_history_restores_candidate_only_run_without_importing(self):
        """A one-off discovery remains a review queue after a page reload."""
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [{
            "title": "History-only candidate fixture",
            "abstract": "A candidate retained in the discovery review queue.",
            "year": 2026,
            "doi": "10.4242/history-only",
            "external_id": "history-only",
        }]
        try:
            result = app.run_query(
                "history candidate screening",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertTrue(result["candidate_mode"])
        self.assertEqual(result["accepted_count"], 1)
        with app.db_session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)

        server, thread, base = self._start_api_server()
        try:
            history_payload = self._api_json(base, "/api/discover/history?limit=20")
            self.assertEqual(set(history_payload), {"history", "limit", "offset"})
            self.assertEqual(history_payload["limit"], 20)
            item = next(entry for entry in history_payload["history"] if entry["id"] == result["id"])
            self.assertTrue(item["candidate_mode"])
            self.assertEqual(item["query"], "history candidate screening")
            self.assertEqual(item["result_count"], 1)
            filtered = self._api_json(base, "/api/discover/history?limit=1&offset=0&q=history%20candidate")
            self.assertEqual([entry["id"] for entry in filtered["history"]], [result["id"]])

            restored = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/results")
            self.assertTrue(restored["candidate_mode"])
            self.assertEqual(restored["papers"][0]["title"], "History-only candidate fixture")
            self.assertTrue(restored["papers"][0]["id"].startswith("candidate:"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_legacy_terminal_candidate_rows_reload_with_retrieval_metadata(self):
        """Pre-split candidate snapshots remain visible after a terminal reload."""
        run_id = "legacy-terminal-candidate-fixture"
        candidate_id = "candidate:legacy-terminal-candidate"
        now = app.now_iso()
        record = {
            "id": candidate_id,
            "title": "Legacy retrieval metadata fixture",
            "abstract": "A saved candidate from before the retrieval and AI screening split.",
            "year": 2025,
            "relevance_score": 72,
            "relevance_label": "高相关",
            "relevance_reasons": ["标题与研究问题有词项重合"],
        }
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at,sources_json) VALUES(?,?,?,?,?,?)",
                (run_id, "legacy retrieval metadata", "success", now, now, '["arxiv"]'),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "legacy retrieval metadata", "{}", json.dumps({"candidate_mode": True}), now),
            )
            db.execute(
                "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                (candidate_id, run_id, "legacy-terminal-key", json.dumps(record, ensure_ascii=False), now),
            )

        restored = app.run_results_snapshot(run_id)
        self.assertEqual(restored["status"], "success")
        self.assertEqual(len(restored["papers"]), 1)
        paper = restored["papers"][0]
        self.assertEqual(paper["retrieval_relevance_label"], "高相关")
        self.assertEqual(paper["retrieval_relevance_score"], 72)
        self.assertEqual(paper["retrieval_relevance_source"], "legacy_snapshot")

    def test_missing_discovery_run_endpoints_return_not_found(self):
        server, thread, base = self._start_api_server()
        try:
            for path in (
                "/api/runs/expired-run-fixture",
                "/api/runs/expired-run-fixture/results",
            ):
                with self.subTest(path=path):
                    try:
                        self._api_json(base, path)
                        self.fail("missing discovery runs must return 404")
                    except HTTPError as exc:
                        self.assertEqual(exc.code, 404)
                        payload = json.loads(exc.read().decode("utf-8"))
                        self.assertTrue(payload.get("error"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_search_limit_caps_final_review_queue_after_expansion(self):
        original = app.SOURCE_SEARCH["arxiv"]
        records = [{
            "title": f"Expanded review candidate {index}",
            "abstract": "A candidate about the target research question.",
            "year": 2026,
            "doi": f"10.4242/expanded-limit-{index}",
            "external_id": f"expanded-limit-{index}",
        } for index in range(9)]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(item) for item in records]
        try:
            result = app.run_query(
                "expanded review queue limit fixture",
                ["arxiv"],
                3,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(len(result["papers"]), 3)
        self.assertEqual(result["summary"]["result_limit"], 3)
        self.assertEqual(result["summary"]["truncated_count"], 6)
        restored = app.run_results_snapshot(result["id"])
        self.assertEqual(len(restored["papers"]), 3)

    def test_triage_progress_counts_visible_candidates_not_filtered_queue_rows(self):
        records = [{
            "title": f"Visible screening candidate {index}",
            "abstract": f"Candidate {index} studies the target research question.",
            "year": 2026,
            "doi": f"10.4242/visible-screening-{index}",
            "external_id": f"visible-screening-{index}",
        } for index in range(8)]
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(item) for item in records]
        try:
            result = app.run_query(
                "visible screening progress fixture",
                ["arxiv"],
                20,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(len(result["papers"]), 8)
        with app.db_session() as db:
            for index in range(58):
                candidate_id = f"candidate:hidden-screening-{index}"
                record = {"id": candidate_id, "title": f"Filtered candidate {index}", "abstract": "Not visible in this run's review list."}
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (candidate_id, result["id"], f"hidden-screening-{index}", json.dumps(record), app.now_iso()),
                )

        running = app._save_discovery_screening_state(result["id"], {"status": "running", "total": 66, "screened": 4})
        self.assertEqual((running["screened"], running["total"]), (4, 8))
        completed = app._save_discovery_screening_state(result["id"], {"status": "completed", "total": 66, "screened": 8})
        self.assertEqual((completed["screened"], completed["total"]), (8, 8))
        state = app.discovery_triage_state(result["id"])
        self.assertEqual((state["processed_count"], state["candidate_count"]), (8, 8))

    def test_suppress_seen_keeps_an_explicitly_empty_candidate_snapshot(self):
        record = {
            "title": "Repeated candidate should stay hidden fixture",
            "abstract": "Used to confirm that an empty filtered result stays empty after reload.",
            "year": 2026,
            "doi": "10.4242/empty-suppressed-candidate",
            "external_id": "empty-suppressed-candidate",
        }
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record)]
        try:
            first = app.run_query("empty suppressed candidate question", ["arxiv"], 10, options={"expand_concepts": False, "ai_screen": False})
            second = app.run_query("empty suppressed candidate question", ["arxiv"], 10, options={"expand_concepts": False, "suppress_seen": True, "ai_screen": False})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(len(first["papers"]), 1)
        self.assertEqual(second["papers"], [])
        restored = app.run_results_snapshot(second["id"])
        self.assertEqual(restored["papers"], [])
        self.assertEqual(restored["accepted_count"], 0)
        with app.db_session() as db:
            row = db.execute("SELECT summary_json FROM run_snapshots WHERE run_id=?", (second["id"],)).fetchone()
        self.assertEqual(json.loads(row["summary_json"])["candidate_result_ids"], [])

    def test_fill_repeats_preserves_requested_page_after_strict_filter(self):
        original = app.SOURCE_SEARCH["arxiv"]
        records = [{
            "title": f"Repeat fill candidate {index}",
            "abstract": "multimodal reasoning verification",
            "year": 2026,
            "doi": f"10.4242/repeat-fill-{index}",
            "url": f"https://example.test/repeat-fill-{index}",
            "source": "arxiv",
        } for index in range(3)]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args, **_kwargs: [dict(item) for item in records]
        try:
            first = app.run_query(
                "repeat fill page size fixture", ["arxiv"], 3,
                options={"expand_concepts": False, "ai_expand": False, "ai_screen": False},
            )
            second = app.run_query(
                "repeat fill page size fixture", ["arxiv"], 3,
                options={"expand_concepts": False, "suppress_seen": True, "fill_repeats": True, "ai_expand": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(first["papers"]), 3)
        self.assertEqual(len(second["papers"]), 3)
        self.assertEqual(second["shortfall_count"], 0)
        self.assertTrue(second["repeat_fallback"])
        self.assertEqual(second["repeat_fill_count"], 3)
        self.assertTrue(all(paper["seen_before"] for paper in second["papers"]))
        self.assertTrue(any("保持请求数量" in reason for reason in second["papers"][0]["discovery_reasons"]))

    def test_strict_fresh_only_overrides_stale_repeat_fill_flag(self):
        options = app.normalize_search_options({
            "suppress_seen": True,
            "fill_repeats": True,
            "strict_fresh_only": True,
        })
        self.assertTrue(options["suppress_seen"])
        self.assertFalse(options["fill_repeats"])
        self.assertEqual(options["strict_fresh_only"], True)

    def test_repeat_search_uses_history_when_connector_is_rate_limited(self):
        """A transient provider 429 must not erase a repeat topic's review page."""
        records = [{
            "title": f"Rate limited history candidate {index}",
            "abstract": "Food volume estimation from images with deep learning.",
            "year": 2026,
            "doi": f"10.4242/rate-limited-history-{index}",
            "external_id": f"rate-limited-history-{index}",
            "url": f"https://example.test/rate-limited-history-{index}",
        } for index in range(3)]
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args, **_kwargs: [dict(item) for item in records]
        try:
            first = app.run_query(
                "rate limited repeat history question", ["arxiv"], 3,
                options={"expand_concepts": False, "ai_screen": False},
            )
            def rate_limited(*_args, **_kwargs):
                raise HTTPError("https://export.arxiv.org", 429, "Too Many Requests", {}, io.BytesIO(b""))
            app.SOURCE_SEARCH["arxiv"] = rate_limited
            second = app.run_query(
                "rate limited repeat history question", ["arxiv"], 3,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(len(first["papers"]), 3)
        self.assertEqual(len(second["papers"]), 3)
        self.assertEqual(second["shortfall_count"], 0)
        self.assertEqual(second["status"], "partial")
        self.assertTrue(second["repeat_fallback"])
        self.assertIn("429", second["error"])
        self.assertTrue(all(paper.get("selection_backfilled") for paper in second["papers"]))

    def test_ai_screen_keeps_relevant_repeat_fallback_when_strict_fresh_filter_has_no_new_results(self):
        record = {
            "title": "Repeat fallback for AI screening fixture",
            "abstract": "A relevant candidate retained for model-assisted review.",
            "year": 2026,
            "doi": "10.4242/ai-screen-repeat-fallback",
            "external_id": "ai-screen-repeat-fallback",
        }
        original_source = app.SOURCE_SEARCH["arxiv"]
        original_start = app.start_discovery_ai_screen
        started = []
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record)]
        app.start_discovery_ai_screen = lambda run_id: started.append(run_id) or {"status": "queued", "run_id": run_id}
        try:
            first = app.run_query(
                "ai screening repeat fallback question",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            second = app.run_query(
                "ai screening repeat fallback question",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "suppress_seen": True, "ai_screen": True},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original_source
            app.start_discovery_ai_screen = original_start

        self.assertEqual(len(first["papers"]), 1)
        self.assertEqual(len(second["papers"]), 1)
        self.assertTrue(second["repeat_fallback"])
        self.assertTrue(second["summary"]["repeat_fallback_for_ai_screen"])
        self.assertIn("保留历史相关候选", second["papers"][0]["discovery_reasons"][-1])
        self.assertEqual(second["triage"]["status"], "queued")
        self.assertEqual(started, [second["id"]])

    def test_manual_saved_topic_run_is_review_only_until_explicit_import(self):
        record = {
            "title": "Saved-topic review candidate fixture",
            "abstract": "The manually launched topic must remain a candidate until selected.",
            "year": 2026,
            "doi": "10.4242/saved-topic-review",
            "external_id": "saved-topic-review",
        }
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record)]
        query_id = "saved-topic-review-fixture"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                """INSERT INTO saved_queries(
                       id,name,query,sources_json,search_options_json,cadence_hours,
                       active,last_run_at,next_run_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,1,'',?,?,?)""",
                (query_id, "Review topic", "targeted saved topic question", '["arxiv"]',
                 json.dumps({"expand_concepts": False, "ai_screen": False}), 168, now, now, now),
            )

        server, thread, base = self._start_api_server()
        try:
            queued = self._api_json(base, f"/api/queries/{query_id}/run", "POST", {}, expected_status=202)
            self.assertTrue(queued["candidate_mode"])
            self.assertTrue(queued["plan"]["search_options"]["review_only"])

            result = self._wait_for_run_results(base, queued["id"])
            self.assertTrue(result["candidate_mode"])
            self.assertEqual(len(result["papers"]), 1)
            self.assertTrue(result["papers"][0]["id"].startswith("candidate:"))
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)

            imported = self._api_json(
                base,
                f"/api/runs/{quote(queued['id'], safe='')}/import-candidates",
                "POST",
                {"candidate_ids": [result["papers"][0]["id"]]},
            )
            self.assertEqual(imported["imported"], 1)
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_tavily_only_search_is_accepted_as_review_only_candidate_run(self):
        record = {
            "title": "Deep Learning Food Volume Estimation with Convolutional Attention",
            "abstract": "We estimate food volume from images using convolutional features and attention.",
            "year": 2026,
            "url": "https://example.test/food-volume-estimation",
            "source": "tavily",
            "external_id": "https://example.test/food-volume-estimation",
            "verification_status": "unverified",
            "verification_sources": [],
            "discovery_only": True,
            "tavily_score": 0.88,
        }
        original = app.SOURCE_SEARCH["tavily"]
        app.SOURCE_SEARCH["tavily"] = lambda _query, _limit, offset=0: [dict(record)] if offset == 0 else []
        server, thread, base = self._start_api_server()
        try:
            queued = self._api_json(
                base,
                "/api/discover",
                "POST",
                {
                    "query": "food volume estimation with convolutional attention",
                    "sources": ["tavily"],
                    "limit": 10,
                    "options": {"search_backend": "connectors", "ai_expand": False, "ai_screen": False},
                },
                expected_status=202,
            )
            self.assertEqual(queued["source_names"], ["tavily"])
            result = self._wait_for_run_results(base, queued["id"])
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["candidate_mode"])
            self.assertEqual(len(result["papers"]), 1)
            self.assertTrue(result["papers"][0]["id"].startswith("candidate:"))
            self.assertTrue(result["papers"][0]["discovery_only"])
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        finally:
            app.SOURCE_SEARCH["tavily"] = original
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_saved_query_can_opt_in_to_tavily_discovery(self):
        server, thread, base = self._start_api_server()
        try:
            saved = self._api_json(
                base,
                "/api/queries",
                "POST",
                {
                    "name": "Tavily scheduled discovery fixture",
                    "query": "food volume estimation",
                    "sources": ["tavily"],
                    "cadence_hours": 168,
                },
                expected_status=201,
            )
            self.assertEqual(saved["sources"], ["tavily"])
            with app.db_session() as db:
                row = db.execute("SELECT sources_json FROM saved_queries WHERE id=?", (saved["id"],)).fetchone()
            self.assertEqual(json.loads(row["sources_json"]), ["tavily"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_scheduled_search_is_review_only_unless_auto_add_is_explicit(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda query, *_args: [{
            "title": "Scheduled candidate " + ("opt-in" if "explicit" in query else "review"),
            "abstract": "A scheduled search result retained for review.",
            "year": 2026,
            "doi": "10.4242/scheduled-" + ("opt-in" if "explicit" in query else "review"),
            "external_id": "scheduled-" + ("opt-in" if "explicit" in query else "review"),
        }]
        now = app.now_iso()
        with app.db_session() as db:
            for query_id, name, query in (
                ("scheduled-review", "Scheduled review", "scheduled review topic"),
                ("scheduled-auto-add", "Scheduled opt-in", "explicit scheduled auto add"),
            ):
                db.execute(
                    "INSERT INTO saved_queries(id,name,query,sources_json,search_options_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,168,1,'',?,?,?)",
                    (query_id, name, query, '[\"arxiv\"]', json.dumps({"expand_concepts": False, "ai_screen": False}), now, now, now),
                )
        try:
            review = app.run_query(
                "scheduled review topic", ["arxiv"], 10,
                query_id="scheduled-review", scheduled=True,
                options={"expand_concepts": False, "ai_screen": False},
            )
            self.assertTrue(review["candidate_mode"])
            self.assertTrue(review["papers"][0]["id"].startswith("candidate:"))
            run_detail = app.run_detail(review["id"])
            self.assertTrue(run_detail["candidate_mode"])
            self.assertEqual((run_detail["candidate_count"], run_detail["awaiting_review_count"]), (1, 1))
            server, thread, base = self._start_api_server()
            try:
                state = self._api_json(base, "/api/state")
                listed = next(item for item in state["runs"] if item["id"] == review["id"])
                self.assertTrue(listed["candidate_mode"])
                self.assertEqual(listed["candidate_count"], 1)
                task = next(item for item in self._api_json(base, "/api/sync-jobs") if item["run_id"] == review["id"])
                self.assertEqual((task["candidate_count"], task["awaiting_review_count"]), (1, 1))
                app.import_discovery_candidates(review["id"], [review["papers"][0]["id"]])
                updated_task = next(item for item in self._api_json(base, "/api/sync-jobs") if item["run_id"] == review["id"])
                self.assertEqual((updated_task["imported_count"], updated_task["awaiting_review_count"]), (1, 0))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)

            opted_in = app.run_query(
                "explicit scheduled auto add", ["arxiv"], 10,
                query_id="scheduled-auto-add", scheduled=True,
                options={"expand_concepts": False, "ai_screen": False, "auto_add": True},
            )
            self.assertFalse(opted_in["candidate_mode"])
            self.assertEqual(opted_in["papers"][0]["title"], "Scheduled candidate opt-in")
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 2)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

    def test_json_migration_preserves_candidate_history_triage_and_feedback(self):
        original_search = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [{
            "title": "Portable discovery-history fixture",
            "abstract": "Candidate metadata retained during JSON migration.",
            "year": 2026,
            "doi": "10.4242/portable-discovery-history",
            "external_id": "portable-discovery-history",
        }]
        try:
            result = app.run_query(
                "portable candidate history question",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original_search
        candidate = result["papers"][0]
        app.update_discovery_candidate_feedback(result["id"], candidate["id"], "irrelevant", "not the target population")
        app._save_discovery_screening_state(result["id"], {"status": "unavailable", "error": "fixture: model not configured"})

        server, thread, base = self._start_api_server()
        old_paths = (app.DATA_DIR, app.FILES_DIR, app.BACKUP_DIR, app.DB_PATH)
        try:
            export_req = Request(base + "/api/export/json", method="GET")
            with urlopen(export_req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("discovery_candidates", payload)
            self.assertIn("discovery_triage_jobs", payload)
            self.assertIn("discovery_candidate_feedback", payload)

            destination = Path(self.temp.name) / "migration-target"
            app.DATA_DIR = destination
            app.FILES_DIR = destination / "files"
            app.BACKUP_DIR = destination / "backups"
            app.DB_PATH = destination / "literature.sqlite3"
            app.init_db()
            imported = self._api_json(base, "/api/import", "POST", {"format": "json", "content": json.dumps(payload, ensure_ascii=False)})
            self.assertEqual(imported["discovery"]["candidates"], 1)
            self.assertEqual(imported["discovery"]["triage_jobs"], 1)
            self.assertGreaterEqual(imported["discovery"]["feedback"], 1)

            history = self._api_json(base, "/api/discover/history?limit=20")["history"]
            self.assertTrue(any(item["id"] == result["id"] for item in history))
            restored = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/results")
            self.assertEqual(restored["papers"][0]["review_decision"], "irrelevant")
            self.assertEqual(restored["triage"]["status"], "unavailable")
        finally:
            app.DATA_DIR, app.FILES_DIR, app.BACKUP_DIR, app.DB_PATH = old_paths
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_candidate_feedback_is_intent_scoped_and_hydrates_repeat_search(self):
        """Rejecting a candidate must survive UUID changes but not leak to another intent."""
        record = {
            "title": "Stable feedback candidate fixture",
            "abstract": "A paper used to verify durable discovery feedback.",
            "year": 2026,
            "doi": "10.4242/stable-feedback",
            "external_id": "stable-feedback",
        }
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record)]
        try:
            first = app.run_query(
                "targeted multimodal evidence synthesis",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            first_candidate = first["papers"][0]
            server, thread, base = self._start_api_server()
            try:
                feedback = self._api_json(
                    base,
                    f"/api/runs/{quote(first['id'], safe='')}/candidates/{quote(first_candidate['id'], safe='')}/feedback",
                    "POST",
                    {"decision": "irrelevant", "note": "研究对象不匹配，后续不要再优先推荐"},
                )
                changed = feedback.get("candidate", feedback)
                self.assertEqual(changed["review_decision"], "irrelevant")
                self.assertEqual(changed["prior_feedback"]["note"], "研究对象不匹配，后续不要再优先推荐")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            repeated = app.run_query(
                "targeted multimodal evidence synthesis",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            repeated_candidate = repeated["papers"][0]
            self.assertNotEqual(first_candidate["id"], repeated_candidate["id"])
            self.assertEqual(first_candidate["candidate_key"], repeated_candidate["candidate_key"])
            self.assertEqual(repeated_candidate["review_decision"], "irrelevant")
            self.assertEqual(repeated_candidate["user_decision"], "irrelevant")
            self.assertEqual(repeated_candidate["prior_feedback"]["note"], "研究对象不匹配，后续不要再优先推荐")
            self.assertTrue(repeated_candidate["seen_before"])

            server, thread, base = self._start_api_server()
            try:
                revoked = self._api_json(
                    base,
                    f"/api/runs/{quote(repeated['id'], safe='')}/candidates/{quote(repeated_candidate['id'], safe='')}/feedback",
                    "POST",
                    {"decision": "new"},
                )
                self.assertEqual(revoked["decision"], "new")
                self.assertIsNone(revoked["candidate"]["prior_feedback"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            after_revoke = app.run_query(
                "targeted multimodal evidence synthesis",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )["papers"][0]
            self.assertEqual(after_revoke["review_decision"], "new")
            self.assertIsNone(after_revoke["prior_feedback"])

            isolated = app.run_query(
                "a genuinely separate research question",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            isolated_candidate = isolated["papers"][0]
            self.assertNotEqual(repeated["intent_key"], isolated["intent_key"])
            self.assertEqual(isolated_candidate["review_decision"], "new")
            self.assertIsNone(isolated_candidate["prior_feedback"])
            self.assertFalse(isolated_candidate["seen_before"])
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

    def test_discovery_triage_without_model_is_explicit_and_keeps_candidates(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [{
            "title": "No-model triage fixture",
            "abstract": "The candidate must remain manually reviewable without a configured model.",
            "year": 2026,
            "doi": "10.4242/no-model-triage",
            "external_id": "no-model-triage",
        }]
        try:
            result = app.run_query(
                "triage unavailable fixture",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        server, thread, base = self._start_api_server()
        try:
            initial_payload = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/triage")
            initial = initial_payload.get("triage", initial_payload)
            self.assertEqual(initial["status"], "not_requested")
            self.assertFalse(initial["configured"])
            started_payload = self._api_json(
                base,
                f"/api/runs/{quote(result['id'], safe='')}/triage",
                "POST",
                {"force": False},
                expected_status=202,
            )
            self.assertIn((started_payload.get("triage", started_payload))["status"], {"queued", "running", "unavailable"})
            final = self._wait_for_triage(base, result["id"])
            self.assertEqual(final["status"], "unavailable")
            self.assertFalse(final["configured"])
            restored = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/results")
            self.assertEqual(len(restored["papers"]), 1)
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_discovery_triage_annotates_bounded_candidate_payload_without_importing(self):
        records = [
            {
                "title": "Direct triage fixture",
                "abstract": "This directly studies the target multimodal reasoning task.",
                "year": 2026,
                "doi": "10.4242/direct-triage",
                "external_id": "direct-triage",
            },
            {
                "title": "Unrelated triage fixture",
                "abstract": "This is about an unrelated agricultural crop experiment.",
                "year": 2025,
                "doi": "10.4242/unrelated-triage",
                "external_id": "unrelated-triage",
            },
        ]
        source_original = app.SOURCE_SEARCH["arxiv"]
        settings_original = app.model_settings
        key_original = app.model_api_key
        request_original = app.model_json_request
        calls = []
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record) for record in records]
        app.model_settings = lambda: {"enabled": True, "model": "triage-fixture-model"}
        app.model_api_key = lambda: "triage-fixture-key"

        def fake_model_request(_prompt, payload, timeout=0, max_output_tokens=None):
            calls.append({"payload": payload, "timeout": timeout, "max_output_tokens": max_output_tokens})
            return {"results": [
                {
                    "id": candidate["id"],
                    "label": "direct" if index == 0 else "unrelated",
                    "score": 95 if index == 0 else 4,
                    "confidence": "high",
                    "reason": "fixture classification",
                }
                for index, candidate in enumerate(payload["candidates"])
            ]}

        app.model_json_request = fake_model_request
        try:
            result = app.run_query(
                "direct multimodal reasoning target",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            server, thread, base = self._start_api_server()
            try:
                self._api_json(
                    base,
                    f"/api/runs/{quote(result['id'], safe='')}/triage",
                    "POST",
                    {"force": False},
                    expected_status=202,
                )
                final = self._wait_for_triage(base, result["id"])
                self.assertEqual(final["status"], "completed")
                self.assertEqual(final["processed_count"], 2)
                restored = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/results")
                self.assertEqual(len(restored["papers"]), 2)
                annotations = [candidate["ai_triage"] for candidate in restored["papers"]]
                self.assertEqual({item["label"] for item in annotations}, {"direct", "unrelated"})
                self.assertTrue(all({"label", "score", "confidence", "reason"}.issubset(item) for item in annotations))
                self.assertTrue(all(candidate["ai_screening"] == candidate["ai_triage"] for candidate in restored["papers"]))
                self.assertEqual(len(calls), 1)
                model_candidates = calls[0]["payload"]["candidates"]
                self.assertEqual(len(model_candidates), 2)
                self.assertTrue(all(set(candidate) <= {"id", "title", "abstract", "year", "venue", "authors"} for candidate in model_candidates))
                self.assertTrue(all("raw" not in candidate for candidate in model_candidates))
                with app.db_session() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.SOURCE_SEARCH["arxiv"] = source_original
            app.model_settings = settings_original
            app.model_api_key = key_original
            app.model_json_request = request_original

    def test_discovery_triage_keeps_unverified_web_leads_uncertain(self):
        original_settings = app.model_settings
        original_key = app.model_api_key
        original_request = app.model_json_request
        app.model_settings = lambda: {"enabled": True, "model": "triage-fixture-model"}
        app.model_api_key = lambda: "triage-fixture-key"
        calls = []

        def should_not_screen(_prompt, payload, **_kwargs):
            calls.append(payload)
            return {"results": []}

        app.model_json_request = should_not_screen
        try:
            with app.DB_LOCK, app.db_session() as db:
                run_id = "unverified-web-triage-run"
                now = app.now_iso()
                db.execute("INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)", (run_id, "food volume", "success", now, '["model_web"]'))
                db.execute("INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)", (run_id, "food volume", '{}', json.dumps({"candidate_mode": True, "candidate_result_ids": ["candidate:web-lead"], "intent_key": "intent:web-lead"}), now))
                record = {"id": "candidate:web-lead", "title": "Food volume project", "abstract": "Food volume estimation from images.", "url": "https://github.com/example/food-volume", "verification_status": "unverified", "discovery_only": True}
                db.execute("INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)", (record["id"], run_id, "web-lead", json.dumps(record), now))
            state = app.start_discovery_ai_screen(run_id)
            deadline = time.time() + 3
            while time.time() < deadline:
                state = app.discovery_triage_state(run_id)
                if state.get("status") not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(state["status"], "completed")
            restored = app.run_results_snapshot(run_id)
            self.assertEqual(restored["papers"][0]["ai_triage"]["label"], "uncertain")
            self.assertEqual(restored["papers"][0]["ai_triage"]["origin"], "verification")
            self.assertEqual(calls, [])
        finally:
            app.model_settings = original_settings
            app.model_api_key = original_key
            app.model_json_request = original_request

    def test_screen_candidate_group_accepts_alternate_shape_and_title_mapping(self):
        original = app.model_json_request
        candidates = [
            {"id": "candidate:one", "title": "Food volume estimation", "abstract": "A"},
            {"id": "candidate:two", "title": "Food segmentation", "abstract": "B"},
        ]
        app.model_json_request = lambda *_args, **_kwargs: {"judgments": [
            {"title": "Food volume estimation", "label": "direct", "score": 92, "confidence": "high", "reason": "task match"},
            {"title": "Food segmentation", "label": "adjacent", "score": 60, "confidence": "medium", "reason": "nearby task"},
        ]}
        try:
            result = app._screen_candidate_group("food volume", candidates, 1, 1)
        finally:
            app.model_json_request = original
        self.assertEqual([item["id"] for item in result], ["candidate:one", "candidate:two"])
        self.assertEqual([item["label"] for item in result], ["direct", "adjacent"])

    def test_screen_candidate_group_keeps_candidates_when_model_shape_is_invalid(self):
        original = app.model_json_request
        candidates = [{"id": "candidate:one", "title": "Food volume estimation", "abstract": "A"}]
        app.model_json_request = lambda *_args, **_kwargs: {"answer": "not a screening list"}
        try:
            result = app._screen_candidate_group("food volume", candidates, 1, 1)
        finally:
            app.model_json_request = original
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "uncertain")
        self.assertTrue(result[0]["_format_fallback"])

    def test_discovery_triage_retries_only_omitted_candidates(self):
        records = [
            {
                "title": "Partial triage returned fixture",
                "abstract": "A candidate with an intentionally returned model judgment.",
                "year": 2026,
                "doi": "10.4242/partial-returned",
                "external_id": "partial-returned",
            },
            {
                "title": "Partial triage omitted fixture",
                "abstract": "A candidate intentionally omitted by the mock model.",
                "year": 2026,
                "doi": "10.4242/partial-omitted",
                "external_id": "partial-omitted",
            },
        ]
        source_original = app.SOURCE_SEARCH["arxiv"]
        settings_original = app.model_settings
        key_original = app.model_api_key
        request_original = app.model_json_request
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record) for record in records]
        app.model_settings = lambda: {"enabled": True, "model": "partial-triage-fixture"}
        app.model_api_key = lambda: "partial-triage-key"
        calls = []
        app.model_json_request = lambda _prompt, payload, **_kwargs: (calls.append(payload) or {
            "results": [{
                "id": payload["candidates"][0]["id"],
                "label": "direct",
                "score": 88,
                "confidence": "high",
                "reason": "one candidate was returned",
            }]
        })
        try:
            result = app.run_query(
                "partial triage omission fixture",
                ["arxiv"],
                10,
                options={"expand_concepts": False, "ai_screen": False},
            )
            server, thread, base = self._start_api_server()
            try:
                self._api_json(
                    base,
                    f"/api/runs/{quote(result['id'], safe='')}/triage",
                    "POST",
                    {"force": False},
                    expected_status=202,
                )
                final = self._wait_for_triage(base, result["id"])
                self.assertEqual(final["status"], "completed")
                self.assertEqual(final["candidate_count"], 2)
                self.assertEqual(final["processed_count"], 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(calls[0]["candidates"]), 2)
                self.assertEqual(len(calls[1]["candidates"]), 1)
                self.assertTrue(final["retry_count"] >= 1)
                restored = self._api_json(base, f"/api/runs/{quote(result['id'], safe='')}/results")
                self.assertEqual(len(restored["papers"]), 2)
                self.assertEqual({item["ai_triage"]["label"] for item in restored["papers"]}, {"direct"})
                self.assertTrue(all(item["ai_triage"]["origin"] == "model" for item in restored["papers"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.SOURCE_SEARCH["arxiv"] = source_original
            app.model_settings = settings_original
            app.model_api_key = key_original
            app.model_json_request = request_original

    def test_discovery_rerun_preserves_review_context_and_selected_import_is_explicit(self):
        records = [
            {
                "title": "Wanted rerun candidate",
                "abstract": "A relevant item that remains in the candidate queue.",
                "year": 2026,
                "doi": "10.4242/wanted-rerun",
                "external_id": "wanted-rerun",
            },
            {
                "title": "Exclude rerun candidate",
                "abstract": "This fixture must be removed by the original exclusion filter.",
                "year": 2026,
                "doi": "10.4242/excluded-rerun",
                "external_id": "excluded-rerun",
            },
        ]
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda *_args: [dict(record) for record in records]
        try:
            first = app.run_query(
                "rerun context fixture",
                ["arxiv"],
                10,
                options={
                    "expand_concepts": False,
                    "ai_screen": False,
                    "search_depth": "focused",
                    "exclude_terms": "Exclude",
                },
            )
            self.assertEqual(len(first["papers"]), 1)
            self.assertTrue(first["candidate_mode"])
            server, thread, base = self._start_api_server()
            try:
                rerun_payload = self._api_json(
                    base,
                    f"/api/runs/{quote(first['id'], safe='')}/rerun",
                    "POST",
                    {"ai_screen": False},
                    expected_status=202,
                )
                rerun = rerun_payload.get("run", rerun_payload)
                self.assertNotEqual(rerun["id"], first["id"])
                restored = self._wait_for_run_results(base, rerun["id"])
                self.assertTrue(restored["candidate_mode"])
                self.assertEqual(restored["query"], "rerun context fixture")
                self.assertIn("arxiv", restored["source_names"])
                self.assertEqual(restored["plan"]["search_options"]["search_depth"], "focused")
                self.assertEqual(restored["plan"]["search_options"]["exclude_terms"], "Exclude")
                self.assertEqual(len(restored["papers"]), 1)
                self.assertEqual(restored["papers"][0]["title"], "Wanted rerun candidate")

                selected = restored["papers"][0]
                imported = self._api_json(
                    base,
                    f"/api/runs/{quote(rerun['id'], safe='')}/import-candidates",
                    "POST",
                    {"candidate_ids": [selected["id"]]},
                )
                self.assertEqual(imported["imported"], 1)
                after_import = self._api_json(base, f"/api/runs/{quote(rerun['id'], safe='')}/results")
                self.assertEqual(after_import["papers"][0]["imported_paper_id"], imported["paper_ids"][0])
                with app.db_session() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

    def test_ai_search_plan_is_passed_to_worker_and_saved_before_execution(self):
        plan = {
            "original_query": "multimodal reasoning",
            "query_variants": [
                {"id": "primary", "query": "multimodal reasoning", "lane": "primary"},
                {"id": "ai-1", "query": "vision language reasoning", "lane": "ai"},
            ],
            "sources": [{"id": "arxiv", "role": "最新预印本"}],
            "ai_expansion": {"enabled": True, "added": 1},
        }
        self.assertEqual(app._prioritize_ai_search_variants(plan)[1]["lane"], "ai")
        called = threading.Event()
        captured = {}
        original = app.run_query

        def fake_run_query(query, sources, limit=20, query_id="", scheduled=False, name="", options=None, request_id="", plan_override=None):
            captured.update({"query": query, "sources": sources, "limit": limit, "request_id": request_id, "plan": plan_override})
            called.set()
            return {"id": request_id, "status": "success", "papers": []}

        app.run_query = fake_run_query
        try:
            result = app.launch_query_job("multimodal reasoning", ["arxiv"], 25, request_id="12345678-1234-4234-8234-123456789012", plan_override=plan)
            self.assertEqual(result["plan"], plan)
            self.assertTrue(called.wait(2), "后台检索 worker 未收到 AI 计划")
            self.assertEqual(captured["plan"], plan)
            self.assertEqual(captured["request_id"], result["id"])
            with app.db_session() as db:
                row = db.execute("SELECT plan_json FROM run_snapshots WHERE run_id=?", (result["id"],)).fetchone()
            self.assertEqual(json.loads(row["plan_json"])["query_variants"][1]["query"], "vision language reasoning")
        finally:
            app.run_query = original

    def test_ai_search_variants_survive_connector_request_budget(self):
        plan = {
            "original_query": "multimodal reasoning",
            "query_variants": [
                {"id": "primary", "query": "multimodal reasoning", "lane": "primary"},
                {"id": "local-1", "query": "multi-modal reasoning", "lane": "synonym"},
                {"id": "local-2", "query": "multimodal inference", "lane": "synonym"},
                {"id": "ai-1", "query": "vision-language reasoning", "lane": "ai"},
                {"id": "ai-2", "query": "cross-modal reasoning", "lane": "ai-adjacent"},
            ],
            "ai_expansion": {"enabled": True, "added": 2},
        }
        calls = []
        call_lock = threading.Lock()
        original = app.SOURCE_SEARCH["arxiv"]

        def fake_search(query, _limit, _offset=0):
            with call_lock:
                calls.append(query)
            return []

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            result = app._prefetch_source_variants("arxiv", plan, 5, False, "focused")
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual([item["id"] for item in result["variants"]], ["primary", "ai-1", "ai-2"])
        self.assertCountEqual(calls, ["multimodal reasoning", "vision-language reasoning", "cross-modal reasoning"])

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

    def test_one_off_search_uses_bounded_network_budget(self):
        calls = []
        original = app.SOURCE_SEARCH["arxiv"]

        def fake_search(query, limit, offset=0):
            calls.append((query, limit, offset))
            return [{
                "title": "Bounded one-off search fixture",
                "abstract": "A fixture for responsive candidate search.",
                "year": 2026,
                "arxiv_id": "2604.00001v1",
                "external_id": "2604.00001v1",
            }]

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            result = app.run_query("responsive candidate search", ["arxiv"], 25, options={"search_depth": "balanced", "expand_concepts": True})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(result["status"], "success")
        # Balanced one-off searches use one page and at most three lanes;
        # saved topics/deep searches retain their larger recall budget.
        self.assertLessEqual(len(calls), 3)
        self.assertTrue(calls)
        self.assertTrue(all(limit <= 25 and offset == 0 for _, limit, offset in calls))
        self.assertEqual(result["accepted_count"], 1)

    def test_repeat_search_refills_a_third_page_when_first_pages_overlap_history(self):
        """Repeat preference must not turn a requested page into a short page."""
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []

        def fake_search(_query, _limit, offset=0):
            calls.append(offset)
            if offset == 0:
                return [{
                    "title": "Historical overlap A",
                    "abstract": "repeat refill fixture",
                    "year": 2026,
                    "arxiv_id": "2605.00001",
                }]
            if offset == 3:
                return [{
                    "title": "Historical overlap B",
                    "abstract": "repeat refill fixture",
                    "year": 2026,
                    "arxiv_id": "2605.00002",
                }]
            if offset == 6:
                return [
                    {
                        "title": "Fresh refill C",
                        "abstract": "repeat refill fixture",
                        "year": 2026,
                        "arxiv_id": "2605.00003",
                    },
                    {
                        "title": "Fresh refill D",
                        "abstract": "repeat refill fixture",
                        "year": 2026,
                        "arxiv_id": "2605.00004",
                    },
                ]
            return []

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            first = app.run_query("repeat refill fixture", ["arxiv"], 3)
            second = app.run_query("repeat refill fixture", ["arxiv"], 3)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(first["accepted_count"], 1)
        self.assertGreaterEqual(second["accepted_count"], 3)
        self.assertIn(6, calls)
        self.assertGreaterEqual(second["fresh_candidate_count"], 3)

    def test_repeat_search_does_not_stop_after_a_full_historical_page(self):
        """A full first page of seen works must not prevent fetching fresh rows."""
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []
        historical = [{
            "title": f"Full historical page {index}",
            "abstract": "repeat page fixture",
            "year": 2026,
            "arxiv_id": f"2606.0000{index}",
        } for index in range(1, 4)]
        fresh = [{
            "title": f"Fresh continuation page {index}",
            "abstract": "repeat page fixture",
            "year": 2026,
            "arxiv_id": f"2606.0000{index + 3}",
        } for index in range(1, 4)]
        search_round = {"value": 0}

        def fake_search(_query, _limit, offset=0):
            calls.append(offset)
            if offset == 0:
                search_round["value"] += 1
                return [dict(item) for item in historical]
            if offset == 3 and search_round["value"] > 1:
                return [dict(item) for item in fresh]
            return []

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            first = app.run_query("full historical page fixture", ["arxiv"], 3, options={"expand_concepts": False, "ai_screen": False})
            second = app.run_query("full historical page fixture", ["arxiv"], 3, options={"expand_concepts": False, "ai_screen": False})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(first["accepted_count"], 3)
        self.assertEqual(second["accepted_count"], 3)
        self.assertEqual(second["shortfall_count"], 0)
        self.assertGreaterEqual(second["fresh_candidate_count"], 3)
        self.assertIn(3, calls)
        self.assertTrue(all(not paper["seen_before"] for paper in second["papers"]))

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
        self.assertTrue(app.model_settings()["api_key_file_configured"])

    def test_environment_key_remains_supported_as_fallback(self):
        os.environ["LIT_AGENT_API_KEY"] = "environment-secret"
        settings = app.model_settings()
        self.assertTrue(settings["api_key_set"])
        self.assertEqual(settings["api_key_source"], "环境变量")
        self.assertNotIn("environment-secret", json.dumps(settings, ensure_ascii=False))

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

    def test_model_settings_warn_on_endpoint_protocol_mismatch(self):
        saved = app.save_model_settings({
            "provider": "Relay",
            "api_url": "https://relay.example.test/v1/responses",
            "protocol": "chat_completions",
            "model": "relay-model",
            "enabled": True,
            "api_key": "relay-mismatch-secret",
        })
        self.assertIn("/responses", saved["endpoint_warning"])
        self.assertIn("Chat Completions", saved["endpoint_warning"])

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

    def test_deep_model_connection_stops_after_first_streamed_text(self):
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
                yield b'event: response.created\n'
                yield b'data: {"type":"response.created"}\n'
                yield b'\n'
                yield b'data: {"type":"response.output_text.delta","delta":"O"}\n'
                yield b'\n'
                raise AssertionError("probe should close the stream after the first text delta")

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data)
            return Response()

        app.urlopen = fake_open
        try:
            result = app.test_model_connection(deep=True)
        finally:
            app.urlopen = original_open
        self.assertTrue(result["ok"])
        self.assertEqual(result["tested"], "inference")
        self.assertEqual(captured["url"], "https://relay.example.test/v1/responses")
        self.assertEqual(captured["timeout"], app.MODEL_PROBE_TIMEOUT)
        self.assertTrue(captured["payload"]["stream"])
        self.assertEqual(captured["payload"]["max_output_tokens"], 1)
        self.assertFalse(captured["payload"]["store"])
        self.assertEqual(captured["payload"]["reasoning"]["effort"], app.MODEL_PROBE_REASONING)
        self.assertTrue(result["stream"])
        self.assertFalse(result["response_complete"])
        self.assertEqual(result["probe_mode"], "first_token")
        self.assertTrue(result["probe_completed"])
        self.assertTrue(result["stream_closed_after_first_token"])
        self.assertIsNotNone(result["time_to_first_token_ms"])
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_deepseek_reasoning_content_counts_as_streamed_probe_output(self):
        """DeepSeek may send reasoning_content before message content."""
        original_open = app.urlopen
        app.save_model_settings({
            "base_url": "https://api.deepseek.com",
            "api_url": "https://api.deepseek.com/chat/completions",
            "protocol": "chat_completions",
            "model": "deepseek-reasoner",
            "enabled": True,
            "api_key": "deepseek-fixture-secret",
        })

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n'
                raise AssertionError("probe should close after DeepSeek reasoning_content")

        def fake_open(_request, timeout=0):
            return Response()

        app.urlopen = fake_open
        try:
            result = app.test_model_connection(deep=True)
        finally:
            app.urlopen = original_open
        self.assertTrue(result["ok"])
        self.assertTrue(result["stream"])

    def test_model_response_content_accepts_deepseek_reasoning_fallback(self):
        result = {"choices": [{"message": {"reasoning_content": "thinking"}}]}
        self.assertEqual(app.model_response_content(result), "thinking")

    def test_streamed_chat_response_collects_deepseek_reasoning_content(self):
        class Response:
            headers = {"Content-Type": "text/event-stream"}
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n'
                yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n'

        result = app.read_model_response(Response())
        self.assertEqual(result["output_text"], "answer")

        class ReasoningOnlyResponse:
            headers = {"Content-Type": "text/event-stream"}
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n'

        reasoning_only = app.read_model_response(ReasoningOnlyResponse())
        self.assertEqual(reasoning_only["output_text"], "thinking")

    def test_compatible_model_request_drops_unsupported_optional_fields(self):
        from io import BytesIO
        from urllib.error import HTTPError

        payload = {"model": "relay-model", "reasoning_effort": "low", "stream": True, "max_tokens": 16}
        attempts = []

        class Response:
            status = 200

            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def fake_open(request, timeout=0):
            attempts.append(json.loads(request.data))
            if len(attempts) == 1:
                raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(b'{"error":"unsupported reasoning_effort"}'))
            if len(attempts) == 2:
                raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(b'{"error":"stream is unsupported"}'))
            return Response()

        original = app.urlopen
        app.urlopen = fake_open
        try:
            with app.open_compatible_model_request("https://relay.example.test/v1/responses", payload, "fixture-key", 10) as response:
                self.assertEqual(response.status, 200)
        finally:
            app.urlopen = original
        self.assertEqual(len(attempts), 3)
        self.assertIn("reasoning_effort", attempts[0])
        self.assertNotIn("reasoning_effort", attempts[1])
        self.assertFalse(attempts[2]["stream"])

    def test_compatible_model_request_retries_transient_gateway_errors_but_not_auth_errors(self):
        from io import BytesIO
        from urllib.error import HTTPError, URLError

        attempts = []

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def fake_open(request, timeout=0):
            attempts.append(request.full_url)
            if len(attempts) <= 2:
                raise HTTPError(request.full_url, 502, "Bad Gateway", {}, BytesIO(b"openresty"))
            return Response()

        original_open, original_sleep = app.urlopen, app.time.sleep
        app.urlopen, app.time.sleep = fake_open, lambda _delay: None
        try:
            with app.open_compatible_model_request("https://relay.example.test/v1/chat/completions", {"model": "m"}, "", 5) as response:
                self.assertEqual(response.status, 200)
        finally:
            app.urlopen, app.time.sleep = original_open, original_sleep
        self.assertEqual(len(attempts), 3)

        attempts.clear()
        def unauthorized(request, timeout=0):
            attempts.append(request.full_url)
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(b"invalid key"))

        app.urlopen = unauthorized
        try:
            with self.assertRaises(HTTPError):
                app.open_compatible_model_request("https://relay.example.test/v1/chat/completions", {"model": "m"}, "", 5)
        finally:
            app.urlopen = original_open
        self.assertEqual(len(attempts), 1)

        attempts.clear()
        def dropped_connection(request, timeout=0):
            attempts.append(request.full_url)
            if len(attempts) <= 2:
                raise URLError("connection reset by peer")
            return Response()

        app.urlopen, app.time.sleep = dropped_connection, lambda _delay: None
        try:
            with app.open_compatible_model_request("https://relay.example.test/v1/chat/completions", {"model": "m"}, "", 5) as response:
                self.assertEqual(response.status, 200)
        finally:
            app.urlopen, app.time.sleep = original_open, original_sleep
        self.assertEqual(len(attempts), 3)

    def test_public_json_request_retries_transient_gateway_errors(self):
        from io import BytesIO
        from urllib.error import HTTPError

        attempts = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"ok": true}'

        def fake_open(request, timeout=0):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise HTTPError(request.full_url, 503, "Service Unavailable", {}, BytesIO(b"busy"))
            return Response()

        original_open, original_sleep = app.urlopen, app.time.sleep
        app.urlopen, app.time.sleep = fake_open, lambda _delay: None
        try:
            self.assertEqual(app.http_json("https://index.example.test/works", timeout=5), {"ok": True})
        finally:
            app.urlopen, app.time.sleep = original_open, original_sleep
        self.assertEqual(len(attempts), 2)

    def test_public_json_uses_provider_native_keys_only_for_matching_hosts(self):
        from io import BytesIO
        from urllib.parse import parse_qs, urlparse

        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"ok": true}'

        def capture(request, timeout=0):
            requests.append(request)
            return Response()

        original_open = app.urlopen
        hosts = {"api.openalex.org", "api.semanticscholar.org"}
        with app.PUBLIC_SOURCE_STATE_LOCK:
            previous_cooldowns = {host: app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None) for host in hosts}
        try:
            with patch.dict(os.environ, {
                "LIT_OPENALEX_API_KEY": "openalex-test-key",
                "LIT_SEMANTIC_SCHOLAR_API_KEY": "s2-test-key",
            }):
                app.urlopen = capture
                app.http_json("https://api.openalex.org/works?search=vision", timeout=5)
                app.http_json("https://api.semanticscholar.org/graph/v1/paper/search?query=vision", timeout=5)
        finally:
            app.urlopen = original_open
            with app.PUBLIC_SOURCE_STATE_LOCK:
                for host, expires in previous_cooldowns.items():
                    app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
                    if expires:
                        app.PUBLIC_SOURCE_COOLDOWNS[host] = expires

        self.assertEqual(len(requests), 2)
        openalex_query = parse_qs(urlparse(requests[0].full_url).query)
        self.assertEqual(openalex_query.get("api_key"), ["openalex-test-key"])
        scholar_headers = {key.casefold(): value for key, value in requests[1].headers.items()}
        self.assertEqual(scholar_headers.get("x-api-key"), "s2-test-key")
        self.assertNotIn("s2-test-key", requests[1].full_url)

    def test_public_json_rate_limit_opens_host_cooldown(self):
        from io import BytesIO
        from urllib.error import HTTPError

        url = "https://rate-limited.example.test/works"
        host = "rate-limited.example.test"
        attempts = []

        def reject(request, timeout=0):
            attempts.append(request.full_url)
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(b"busy"))

        original_open, original_sleep = app.urlopen, app.time.sleep
        with app.PUBLIC_SOURCE_STATE_LOCK:
            app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        app.urlopen, app.time.sleep = reject, lambda _delay: None
        try:
            with self.assertRaises(HTTPError):
                app.http_json(url, timeout=5)
            with self.assertRaises(app.PublicSourceCooldownError):
                app.http_json(url, timeout=5)
        finally:
            app.urlopen, app.time.sleep = original_open, original_sleep
            with app.PUBLIC_SOURCE_STATE_LOCK:
                app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        # A rate-limited source is never hammered once per query variant.
        self.assertEqual(len(attempts), 1)

    def test_public_source_cooldown_survives_process_restart(self):
        """A restart must keep a recently rate-limited host out of the next burst."""
        url = "https://restart-rate-limited.example.test/works"
        host = "restart-rate-limited.example.test"
        with app.PUBLIC_SOURCE_STATE_LOCK:
            app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        app._cooldown_public_source(url, 60)
        with app.PUBLIC_SOURCE_STATE_LOCK:
            app.PUBLIC_SOURCE_COOLDOWNS.clear()
        with app.db_session() as db:
            app._load_persisted_public_cooldowns(db)
        try:
            self.assertGreater(app._public_source_cooldown_remaining(url), 0)
            with app.db_session() as db:
                row = db.execute(
                    "SELECT value FROM app_meta WHERE key=?",
                    (app.PUBLIC_SOURCE_COOLDOWN_META_PREFIX + host,),
                ).fetchone()
            self.assertIsNotNone(row)
        finally:
            with app.PUBLIC_SOURCE_STATE_LOCK:
                app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)

    def test_parallel_public_json_requests_share_host_cooldown(self):
        """Concurrent query variants must not stampede a rate-limited host."""
        from io import BytesIO

        url = "https://parallel-rate-limited.example.test/works"
        host = "parallel-rate-limited.example.test"
        attempts = []

        def reject(request, timeout=0):
            attempts.append(request.full_url)
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(b"busy"))

        original_open = app.urlopen
        with app.PUBLIC_SOURCE_STATE_LOCK:
            app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        app.urlopen = reject
        outcomes = []

        def request_once():
            try:
                app.http_json(url, timeout=5)
            except Exception as exc:  # both callers should receive a useful source error
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=request_once) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        finally:
            app.urlopen = original_open
            with app.PUBLIC_SOURCE_STATE_LOCK:
                app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(outcomes), 2)
        self.assertIn("HTTPError", outcomes)
        self.assertIn("PublicSourceCooldownError", outcomes)

    def test_arxiv_uses_bounded_public_bytes_helper(self):
        original = app.http_bytes
        captured = {}

        def fake_bytes(url, timeout, accept):
            captured.update({"url": url, "timeout": timeout, "accept": accept})
            return b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'''

        app.http_bytes = fake_bytes
        try:
            self.assertEqual(app.search_arxiv("multimodal", 10), [])
        finally:
            app.http_bytes = original
        self.assertLessEqual(captured["timeout"], 30)
        self.assertIn("xml", captured["accept"])

    def test_deepseek_provider_preset_is_editable_and_contains_no_secret(self):
        presets = app.model_provider_presets()
        deepseek = next(item for item in presets if item["id"] == "deepseek")
        self.assertEqual(deepseek["protocol"], "chat_completions")
        self.assertEqual(deepseek["api_url"], "https://api.deepseek.com/chat/completions")
        self.assertIn("deepseek-flash", deepseek["models"])
        self.assertIn("deepseek-chat", deepseek["models"])
        self.assertNotIn("api_key", json.dumps(deepseek, ensure_ascii=False).lower())

    def test_deepseek_search_toggle_persists_explicitly_off(self):
        config = {
            "provider": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_url": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-flash",
            "enabled": True,
            "api_key": "fixture-key",
            "deepseek_search_enabled": True,
        }
        with patch.dict(os.environ, {"LIT_DEEPSEEK_SEARCH_ENABLED": ""}, clear=False):
            app.save_model_settings(config)
            self.assertTrue(app.model_settings()["deepseek_search_enabled"])
            app.save_model_settings({**config, "api_key": "", "deepseek_search_enabled": False})
            self.assertFalse(app.model_settings()["deepseek_search_enabled"])

    def test_switching_provider_requires_a_new_key(self):
        app.save_model_settings({"provider": "旧服务", "base_url": "https://old.example.test/v1", "model": "old-model", "enabled": True, "api_key": "old-secret"})
        with self.assertRaisesRegex(ValueError, "新 API Key"):
            app.save_model_settings({"provider": "DeepSeek", "base_url": "https://api.deepseek.com", "api_url": "https://api.deepseek.com/chat/completions", "model": "deepseek-chat", "enabled": True})
        self.assertEqual(app.model_settings()["provider"], "旧服务")
        self.assertEqual(app.model_api_key(), "old-secret")
        saved = app.save_model_settings({"provider": "DeepSeek", "base_url": "https://api.deepseek.com", "api_url": "https://api.deepseek.com/chat/completions", "model": "deepseek-chat", "enabled": True, "api_key": "new-secret"})
        self.assertEqual(saved["provider"], "DeepSeek")
        self.assertNotIn("new-secret", json.dumps(saved, ensure_ascii=False))

    def test_responses_payload_disables_storage_by_default(self):
        payload = app._model_request_payload({"protocol": "responses", "model": "relay-model", "temperature": 0.2}, "system", "input")
        self.assertFalse(payload["store"])

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
        self.assertEqual(captured["timeout"], 8)

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
        self.assertEqual(captured["body"]["reasoning_effort"], app.MODEL_REASONING_EFFORT)

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
        self.assertEqual(summary["metrics"]["schema_version"], str(app.DB_SCHEMA_VERSION))
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

    def test_paper_detail_distinguishes_pdf_file_from_cached_fulltext(self):
        paper_id, _ = app.upsert_paper({"title": "Cached full-text fixture"}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("cached-text-attachment", paper_id, "paper.pdf", "missing-on-disk.pdf", "c" * 64, 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("cached-text-attachment", paper_id, 1, "Previously extracted page text.", 30, "good", "fixture", "d" * 64, now))
        detail = app.paper_detail(paper_id)
        self.assertFalse(detail["has_local_pdf"])
        self.assertTrue(detail["has_fulltext_source"])

    def test_paper_detail_summary_omits_large_ai_answers(self):
        """The browser detail request must stay small while history stays complete."""
        paper_id, _ = app.upsert_paper({"title": "Lightweight detail fixture"}, source="manual")
        now = app.now_iso()
        answer = json.dumps({"analysis": {"overview": "x" * 5000}}, ensure_ascii=False)
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("large-detail-artifact", paper_id, "deep_read", "fixture", "fixture-model", "v1", "a" * 64, answer, now),
            )
        full = app.paper_detail(paper_id)
        summary = app.paper_detail(paper_id, include_ai_artifacts=False)
        self.assertEqual(full["ai_artifacts"][0]["answer"], answer)
        self.assertNotIn("answer", summary["ai_artifacts"][0])
        self.assertEqual(summary["ai_artifacts"][0]["artifact_type"], "deep_read")

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

    def test_run_progress_survives_refresh_at_top_level(self):
        """A refreshed discovery response keeps the scheduler stage visible."""
        run_id = "run-progress-refresh-fixture"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json,source_details_json,error) VALUES(?,?,?,?,?,?,?)",
                (run_id, "progress refresh", "running", now, '["arxiv","crossref"]', "[]", ""),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,attempts,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "", run_id, "running", "source:crossref", 2, "", now, now),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "progress refresh", "{}", json.dumps({"candidate_mode": True}), now),
            )
        detail = app.run_detail(run_id)
        self.assertEqual(detail["stage"], "source:crossref")
        self.assertEqual((detail["source_index"], detail["source_total"]), (2, 2))
        self.assertEqual(detail["progress_current"], 2)
        self.assertEqual(detail["search_status"], "running")
        self.assertEqual(detail["screening_status"], "not_requested")
        self.assertEqual(detail["workflow_status"], "running")
        self.assertEqual(detail["screening_progress"], {"processed": 0, "total": 0, "error": ""})
        snapshot = app.run_results_snapshot(run_id)
        self.assertEqual(snapshot["stage"], "source:crossref")
        self.assertEqual((snapshot["progress_current"], snapshot["progress_total"]), (2, 2))
        self.assertEqual(snapshot["search_status"], "running")
        self.assertEqual(snapshot["workflow_status"], "running")

    def test_model_web_progress_recovers_logical_source_from_details(self):
        """Agent/MCP model searches must not become 0/0 after a refresh."""
        run_id = "model-web-progress-refresh-fixture"
        now = app.now_iso()
        details = [{
            "source": "model_web", "status": "partial", "fetched": 12,
            "accepted": 8, "filtered": 4, "new": 5, "duplicates": 3,
            "variants": [], "error": "Semantic Scholar 暂时限流",
        }]
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at,sources_json,source_details_json,error) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "model web progress", "partial", now, now, "[]", json.dumps(details), "partial source"),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,attempts,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "", run_id, "retryable", "completed", 1, "partial source", now, now),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "model web progress", "{}", json.dumps({"candidate_mode": True}), now),
            )
        detail = app.run_detail(run_id)
        self.assertEqual(detail["sources"], ["model_web"])
        self.assertEqual((detail["source_index"], detail["source_total"]), (1, 1))
        snapshot = app.run_results_snapshot(run_id)
        self.assertEqual((snapshot["progress_current"], snapshot["progress_total"]), (1, 1))

    def test_active_model_web_progress_has_one_logical_source_without_details(self):
        """A running model search exposes progress before source rows exist."""
        run_id = "model-web-active-progress-fixture"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json,source_details_json,error) VALUES(?,?,?,?,?,?,?)",
                (run_id, "active model web", "running", now, "[]", "[]", ""),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,attempts,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "", run_id, "running", "model_web_search:调用检索工具", 1, "", now, now),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "active model web", "{}", json.dumps({"candidate_mode": True}), now),
            )
        detail = app.run_detail(run_id)
        self.assertEqual(detail["sources"], ["model_web"])
        self.assertEqual((detail["source_index"], detail["source_total"]), (1, 1))

    def test_search_and_screening_status_are_consistent_after_refresh(self):
        """The second AI phase remains visible after the connector run ends."""
        run_id = "run-screening-progress-fixture"
        now = app.now_iso()
        summary = {
            "candidate_mode": True,
            "candidate_result_ids": ["candidate:1", "candidate:2", "candidate:3"],
            "ai_screening": {"status": "running", "processed_count": 1, "candidate_count": 3, "error": ""},
        }
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at,sources_json,source_details_json,error) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, "screening progress", "partial", now, now, '["openalex"]', "[]", "OpenAlex timeout"),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "screening progress", "{}", json.dumps(summary), now),
            )
            db.execute(
                "INSERT INTO discovery_triage_jobs(run_id,status,model,candidate_count,processed_count,error,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "running", "deepseek-flash", 3, 1, "", now, now, ""),
            )

        detail = app.run_detail(run_id)
        self.assertEqual(detail["search_status"], "partial")
        self.assertEqual(detail["screening_status"], "running")
        self.assertEqual(detail["screening_progress"], {"processed": 1, "total": 3, "error": ""})
        self.assertEqual(detail["workflow_status"], "screening_running")

        snapshot = app.run_results_snapshot(run_id)
        self.assertEqual(snapshot["search_status"], "partial")
        self.assertEqual(snapshot["screening_status"], "running")
        self.assertEqual(snapshot["workflow_status"], "screening_running")

        history = app.discovery_history(limit=10)
        item = next(row for row in history if row["id"] == run_id)
        self.assertEqual(item["workflow_status"], "screening_running")
        self.assertEqual(item["screening_progress"]["processed"], 1)

        with app.db_session() as db:
            db.execute(
                "UPDATE discovery_triage_jobs SET status=?,processed_count=?,completed_at=?,updated_at=? WHERE run_id=?",
                ("completed", 3, now, now, run_id),
            )
        completed = app.run_results_snapshot(run_id)
        self.assertEqual(completed["screening_status"], "completed")
        self.assertEqual(completed["workflow_status"], "completed")
        self.assertEqual(completed["screening_progress"]["processed"], 3)

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
        self.assertEqual(health["database"]["schema_version"], str(app.DB_SCHEMA_VERSION))
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

    def test_recommendations_do_not_let_unrelated_reading_anchors_override_topic(self):
        query_id = "food-volume-memory-test"
        now = app.now_iso()
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        with app.db_session() as db:
            db.execute(
                "INSERT INTO saved_queries(id,name,query,sources_json,cadence_hours,active,last_run_at,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,1,'',?,?,?)",
                (query_id, "食物体积估计", query, '["openalex"]', 168, now, now, now),
            )
        app.upsert_paper({
            "title": "WildObserve: Camera-trap image review software",
            "abstract": "Camera traps collect wildlife images. A computer vision model helps reviewers label animals.",
            "status": "read",
        }, source="manual")
        unrelated, _ = app.upsert_paper({
            "title": "Camera-trap image classification with deep learning",
            "abstract": "A deep learning model classifies animal images captured by camera traps.",
            "status": "to_read",
        }, source="manual")
        relevant, _ = app.upsert_paper({
            "title": "Food Volume Estimation from Meal Images with RGB-D Point Clouds",
            "abstract": "We estimate food volume and portion size from meal images using depth and computer vision.",
            "status": "to_read",
        }, source="manual")

        recommendations = app._research_memory(10)["recommendations"]
        ranked_ids = [item["id"] for item in recommendations]
        self.assertLess(ranked_ids.index(relevant), ranked_ids.index(unrelated))
        relevant_item = next(item for item in recommendations if item["id"] == relevant)
        self.assertFalse(any("接近已读论文" in reason for reason in relevant_item["recommendation_reasons"]))

    def test_openalex_related_works_are_fetched_in_bounded_provider_batches(self):
        original = app.http_json
        requests = []

        def fake_http_json(url, timeout=20):
            from urllib.parse import parse_qs, urlparse
            params = parse_qs(urlparse(url).query)
            requests.append((params.get("filter", [""])[0], params.get("select", [""])[0]))
            if params.get("select") == ["id,related_works"]:
                return {"results": [
                    {"id": "https://openalex.org/W1", "related_works": ["https://openalex.org/W100", "https://openalex.org/W101"]},
                    {"id": "https://openalex.org/W2", "related_works": ["https://openalex.org/W100", "https://openalex.org/W102"]},
                ]}
            return {"results": [
                {"id": "https://openalex.org/W100", "title": "Shared recommendation", "publication_year": 2025, "doi": "https://doi.org/10.1000/shared", "authorships": [], "abstract_inverted_index": {"related": [0], "work": [1]}, "primary_location": {}},
                {"id": "https://openalex.org/W101", "title": "First recommendation", "publication_year": 2024, "doi": "https://doi.org/10.1000/first", "authorships": [], "abstract_inverted_index": {}, "primary_location": {}},
                {"id": "https://openalex.org/W102", "title": "Second recommendation", "publication_year": 2023, "doi": "https://doi.org/10.1000/second", "authorships": [], "abstract_inverted_index": {}, "primary_location": {}},
            ]}

        app.http_json = fake_http_json
        try:
            result = app._load_openalex_recommendations(["W1", "W2"])
        finally:
            app.http_json = original
        self.assertEqual(len(requests), 2)
        self.assertIn("W100", result["by_openalex"])
        self.assertEqual(result["by_openalex"]["W100"]["seed_count"], 2)
        self.assertIn("10.1000/shared", result["by_doi"])
        self.assertEqual(result["external_candidates"][0]["title"], "Shared recommendation")

    def test_openalex_recommendation_signal_personalizes_existing_library_candidates(self):
        anchor_id, _ = app.upsert_paper({"title": "Visual food volume estimation", "abstract": "Estimate food volume from meal images.", "status": "read"}, source="manual")
        recommended_id, _ = app.upsert_paper({"title": "Food portion volume from meal images", "abstract": "A method estimates volume of food portions from RGB images.", "doi": "10.1000/food-volume", "status": "to_read"}, source="manual")
        with app.db_session() as db:
            app.attach_source(db, anchor_id, "openalex", "https://openalex.org/W987")
        baseline = app._research_memory(10)
        ranked = app._research_memory(10, provider_rankings={
            "by_openalex": {},
            "by_doi": {"10.1000/food-volume": {"score": 100, "rank": 1, "seed_count": 1}},
        })
        self.assertIn(recommended_id, [item["id"] for item in baseline["recommendations"]])
        recommended = next(item for item in ranked["recommendations"] if item["id"] == recommended_id)
        self.assertGreater(recommended["recommendation_score"], next(item for item in baseline["recommendations"] if item["id"] == recommended_id)["recommendation_score"])
        self.assertTrue(any("OpenAlex 相关工作推荐" in reason for reason in recommended["recommendation_reasons"]))

    def test_semantic_scholar_recommendations_use_hosted_provider_and_normalize(self):
        original = app.http_json
        requests = []

        def fake_http_json(url, timeout=20):
            requests.append((url, timeout))
            return {"recommendedPapers": [
                {
                    "paperId": "S2-1",
                    "title": "Hosted recommendation",
                    "abstract": "A provider-side recommendation.",
                    "year": 2026,
                    "authors": [{"name": "A Researcher"}],
                    "venue": "A Venue",
                    "externalIds": {"DOI": "10.1000/hosted"},
                    "url": "https://www.semanticscholar.org/paper/S2-1",
                    "citationCount": 12,
                },
            ]}

        app.http_json = fake_http_json
        try:
            result = app._load_semantic_scholar_recommendations(["10.1000/seed"])
        finally:
            app.http_json = original
        self.assertEqual(len(requests), 1)
        self.assertIn("recommendations/v1/papers/for/10.1000%2Fseed", requests[0][0])
        self.assertEqual(result["external_candidates"][0]["title"], "Hosted recommendation")
        self.assertEqual(result["external_candidates"][0]["doi"], "10.1000/hosted")
        self.assertEqual(result["by_doi"]["10.1000/hosted"]["provider"], "Semantic Scholar")

    def test_recommendation_provider_merge_keeps_both_mature_sources(self):
        merged = app._merge_recommendation_provider_results(
            {"by_openalex": {"W1": {"score": 90}}, "by_doi": {"10.1000/shared": {"score": 70, "provider": "OpenAlex"}}, "external_candidates": [{"title": "OpenAlex work", "doi": "10.1000/openalex", "recommendation_score": 80, "recommendation_rank": 1}]},
            {"by_semantic_id": {"S1": {"score": 95}}, "by_doi": {"10.1000/shared": {"score": 92, "provider": "Semantic Scholar"}}, "external_candidates": [{"title": "Semantic work", "doi": "10.1000/semantic", "recommendation_score": 95, "recommendation_rank": 1}]},
        )
        self.assertEqual(merged["by_doi"]["10.1000/shared"]["provider"], "Semantic Scholar")
        self.assertEqual({item["title"] for item in merged["external_candidates"]}, {"OpenAlex work"})
        self.assertEqual({item["title"] for item in merged["external_recommendations"]}, {"OpenAlex work", "Semantic work"})
        self.assertEqual(
            {item["recommendation_provider"] for item in merged["external_recommendations"]},
            {"OpenAlex", "Semantic Scholar"},
        )

    def test_semantic_scholar_external_recommendation_can_be_confirmed_and_imported(self):
        anchor_id, _ = app.upsert_paper({"title": "Semantic seed", "doi": "10.1000/semantic-seed", "status": "read"}, source="manual")
        with app.db_session() as db:
            app.attach_source(db, anchor_id, "semanticscholar", "S-SEED")
        candidate = {
            "external_id": "S2-1",
            "title": "Semantic hosted candidate",
            "abstract": "A provider-side recommendation.",
            "doi": "10.1000/semantic-candidate",
            "source": "semanticscholar",
            "recommendation_provider": "Semantic Scholar",
            "recommendation_score": 95,
            "recommendation_rank": 1,
        }
        cache_key = "oa:|s2:S-SEED"
        with app.OPENALEX_RECOMMENDATION_CACHE_LOCK:
            app.OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
                "status": "ready",
                "rankings": {"external_recommendations": [candidate], "external_candidates": [], "related_count": 1},
                "updated_at": app.now_iso(),
                "at": time.monotonic(),
            }
        try:
            visible = app._available_openalex_recommendations({"external_recommendations": [candidate]})
            self.assertEqual(visible[0]["recommendation_provider"], "Semantic Scholar")
            imported = app.save_openalex_recommendation("S2-1")
            self.assertTrue(imported["created"])
            paper = app.paper_detail(imported["id"])
            self.assertEqual(paper["sources"][0]["source"], "semanticscholar")
        finally:
            with app.OPENALEX_RECOMMENDATION_CACHE_LOCK:
                app.OPENALEX_RECOMMENDATION_CACHE.pop(cache_key, None)

    def test_openalex_recommendation_provider_failure_keeps_local_recommendations(self):
        anchor_id, _ = app.upsert_paper({"title": "Visual food volume estimation", "abstract": "Estimate food volume from meal images.", "status": "read"}, source="manual")
        candidate_id, _ = app.upsert_paper({"title": "Food volume from meal images", "abstract": "Estimate food portion volume using computer vision.", "status": "to_read"}, source="manual")
        with app.db_session() as db:
            app.attach_source(db, anchor_id, "openalex", "https://openalex.org/W7654321")
        cache_key = "W7654321"
        original_loader = app._load_openalex_recommendations
        app._load_openalex_recommendations = lambda _seeds: (_ for _ in ()).throw(RuntimeError("provider unavailable"))
        try:
            app._refresh_openalex_recommendations(cache_key, ["W7654321"])
        finally:
            app._load_openalex_recommendations = original_loader
        result = app.research_memory(10)
        self.assertEqual(result["recommendation_provider"]["status"], "unavailable")
        self.assertIn(candidate_id, [item["id"] for item in result["recommendations"]])
        with app.OPENALEX_RECOMMENDATION_CACHE_LOCK:
            app.OPENALEX_RECOMMENDATION_CACHE.pop(cache_key, None)

    def test_openalex_external_recommendations_require_explicit_import_and_deduplicate(self):
        anchor_id, _ = app.upsert_paper({"title": "Seed paper for OpenAlex suggestions", "status": "read"}, source="manual")
        with app.db_session() as db:
            app.attach_source(db, anchor_id, "openalex", "https://openalex.org/W97531")
        candidate = {
            "openalex_id": "https://openalex.org/W2468",
            "external_id": "https://openalex.org/W2468",
            "doi": "10.1000/openalex-recommendation",
            "title": "Provider recommended paper",
            "authors": "A Researcher",
            "year": 2025,
            "abstract": "A provider-side related-work recommendation.",
            "url": "https://doi.org/10.1000/openalex-recommendation",
            "source": "openalex",
            "recommendation_rank": 1,
            "recommendation_score": 100,
        }
        cache_key = "W97531"
        with app.OPENALEX_RECOMMENDATION_CACHE_LOCK:
            app.OPENALEX_RECOMMENDATION_CACHE[cache_key] = {
                "status": "ready",
                "rankings": {"external_candidates": [candidate], "by_openalex": {}, "by_doi": {}, "related_count": 1},
                "updated_at": app.now_iso(),
                "at": time.monotonic(),
            }
        try:
            visible = app._available_openalex_recommendations({"external_candidates": [candidate]})
            self.assertEqual(len(visible), 1)
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
            server, _thread, base = self._start_api_server()
            try:
                imported = self._api_json(base, "/api/recommendations/import", "POST", {"external_id": "https://openalex.org/W2468"}, expected_status=201)
                repeated = self._api_json(base, "/api/recommendations/import", "POST", {"external_id": "https://openalex.org/W2468"}, expected_status=200)
            finally:
                server.shutdown()
                server.server_close()
            self.assertTrue(imported["created"])
            self.assertFalse(repeated["created"])
            paper = app.paper_detail(imported["id"])
            self.assertEqual(paper["status"], "to_read")
            self.assertEqual(paper["sources"][0]["source"], "openalex")
            self.assertEqual(app._available_openalex_recommendations({"external_candidates": [candidate]}), [])
        finally:
            with app.OPENALEX_RECOMMENDATION_CACHE_LOCK:
                app.OPENALEX_RECOMMENDATION_CACHE.pop(cache_key, None)

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
            app.save_model_settings({"provider": "export-fixture", "base_url": "https://provider.example/v1", "api_url": "https://provider.example/v1/chat/completions", "model": "fixture-chat", "embedding_model": "fixture-embedding", "embedding_api_url": "https://provider.example/v1/embeddings", "api_key": "export-secret-fixture", "enabled": True})
            export_headers, export_body = request("/api/export/json", "GET")
            self.assertIn("application/json", export_headers.get("Content-Type", ""))
            export_payload = json.loads(export_body)
            self.assertEqual(export_payload["schema_version"], app.DB_SCHEMA_VERSION)
            self.assertIn("discovery_search_memory", export_payload)
            self.assertIn("audit_events", export_payload)
            self.assertEqual(export_payload["model_config"]["embedding_model"], "fixture-embedding")
            self.assertNotIn("api_key", export_payload["model_config"])
            self.assertNotIn("export-secret-fixture", export_body.decode("utf-8"))
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
            _, legacy_runs_body = request("/api/search/runs?limit=1")
            self.assertIsInstance(json.loads(legacy_runs_body), list)
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
            self.assertEqual(backup["manifest"]["schema_version"], app.DB_SCHEMA_VERSION)
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

    def test_download_pdf_from_paper_url_and_deduplicate(self):
        pdf_bytes = b"%PDF-1.4\nfixture pdf\n%%EOF\n"

        class PdfHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(pdf_bytes)))
                self.end_headers()
                self.wfile.write(pdf_bytes)

            def log_message(self, *_args):
                return

        remote = ThreadingHTTPServer(("127.0.0.1", 0), PdfHandler)
        remote_thread = threading.Thread(target=remote.serve_forever, daemon=True)
        remote_thread.start()
        api_server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        try:
            paper_id, _ = app.upsert_paper({"title": "Download fixture", "pdf_url": f"http://127.0.0.1:{remote.server_port}/paper.pdf"}, source="manual")
            req = Request(f"http://127.0.0.1:{api_server.server_port}/api/papers/{paper_id}/download-pdf", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=20) as response:
                first = json.loads(response.read())
            self.assertFalse(first["deduplicated"])
            self.assertEqual(first["filename"], "paper.pdf")
            with urlopen(req, timeout=20) as response:
                second = json.loads(response.read())
            self.assertTrue(second["deduplicated"])
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments WHERE paper_id=?", (paper_id,)).fetchone()[0], 1)
        finally:
            api_server.shutdown()
            api_server.server_close()
            remote.shutdown()
            remote.server_close()
            api_thread.join(timeout=2)
            remote_thread.join(timeout=2)

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

    def test_document_search_ranks_bm25_evidence_and_bridges_chinese_terms(self):
        paper_id, _ = app.upsert_paper({"title": "Document retrieval fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        attachment_id = "document-search-fixture"
        with app.db_session() as db:
            db.execute(
                "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
                (attachment_id, paper_id, "fixture.pdf", "fixture.pdf", "document-search-hash", 10, now),
            )
            page_texts = [
                "Straße precedes this study of food volume estimation with deep learning and benchmark data.",
                "Food volume estimation appears in this brief background paragraph.",
                "Deep learning supports vision models, but this page does not discuss portion measurement.",
                "We study a large language model with long-context reasoning.",
            ]
            for page, text in enumerate(page_texts, 1):
                db.execute(
                    "INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (attachment_id, paper_id, page, text, len(text), "good", "fixture", hashlib.sha256(text.encode()).hexdigest(), now),
                )

        ranked = app.search_document_pages(paper_id, "food volume estimation deep learning", limit=3)
        self.assertEqual(ranked[0]["page_number"], 1)
        self.assertEqual(ranked[0]["retrieval_method"], "bm25_all_terms")
        self.assertEqual(ranked[0]["lexical_rank"], 1)
        self.assertEqual(ranked[0]["evidence_anchor"]["page_number"], 1)
        start = ranked[0]["evidence_anchor"]["start_char"]
        self.assertEqual(start, page_texts[0].find("food"))
        self.assertEqual(page_texts[0][start:start + 4].casefold(), "food")

        bridged = app.search_document_pages(paper_id, "大语言模型", limit=3)
        self.assertEqual([item["page_number"] for item in bridged], [4])
        self.assertEqual(bridged[0]["retrieval_method"], "concept_bridge")
        self.assertIn("large language model", bridged[0]["matched_terms"])

    def test_semantic_index_is_idempotent_updates_changed_text_and_fuses_search(self):
        app.save_model_settings({
            "provider": "embedding-fixture", "base_url": "https://provider.example/v1",
            "api_url": "https://provider.example/v1/chat/completions", "model": "fixture-chat",
            "embedding_model": "fixture-embedding", "embedding_api_url": "https://provider.example/v1/embeddings",
            "api_key": "fixture-key", "enabled": True,
        })
        saved_settings = app.model_settings()
        self.assertEqual(saved_settings["effective_embedding_api_url"], "https://provider.example/v1/embeddings")
        self.assertTrue(saved_settings["embedding_configured"])
        self.assertNotIn("fixture-key", json.dumps(saved_settings))
        paper_id, _ = app.upsert_paper({"title": "Semantic retrieval fixture", "year": 2026}, source="manual")
        texts = [
            "Food volume is inferred from visual cues and meal portion geometry.",
            "Reinforcement learning improves robot navigation in changing environments.",
        ]
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("semantic-fixture", paper_id, "fixture.pdf", "semantic-fixture.pdf", "semantic-fixture-hash", 10, now))
            for page_number, text in enumerate(texts, 1):
                db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("semantic-fixture", paper_id, page_number, text, len(text), "good", "fixture", hashlib.sha256(text.encode()).hexdigest(), now))

        def fake_embeddings(values, settings=None, api_key=None):
            vectors = [[0.0, 1.0] if "Reinforcement learning" in value else [1.0, 0.0] for value in values]
            return vectors, 2

        def create_job():
            job_id = "semantic-job-" + str(time.time_ns())
            current = app.model_settings()
            with app.db_session() as db:
                db.execute("INSERT INTO semantic_index_jobs(id,paper_id,model,status,stage,progress_total,message,created_at,updated_at) VALUES(?,?,?,'queued','queued',2,'fixture',?,?)", (job_id, paper_id, app._embedding_index_model(current), now, now))
            return job_id

        app.STOP_EVENT.clear()
        with patch.object(app, "request_embeddings", side_effect=fake_embeddings) as mocked:
            first_job = create_job()
            app.run_semantic_index_job(first_job)
            self.assertEqual(app.get_semantic_index_status(paper_id)["indexed_pages"], 2)
            self.assertEqual(app.get_semantic_index_job(first_job)["status"], "success")

            details = app.search_document_pages_detailed(paper_id, "serving size measurement camera photographs", semantic=True)
            self.assertEqual(details["retrieval_mode"], "hybrid")
            self.assertEqual(details["pages"][0]["page_number"], 1)
            self.assertEqual(details["pages"][0]["retrieval_method"], "semantic")
            anchor = details["pages"][0]["evidence_anchor"]
            self.assertTrue(texts[0][anchor["start_char"]:anchor["end_char"]])

            calls_before = mocked.call_count
            second_job = create_job()
            app.run_semantic_index_job(second_job)
            self.assertEqual(mocked.call_count, calls_before)

            updated_text = "Food intake is estimated by matching meal portion shapes across photographs."
            with app.db_session() as db:
                db.execute("UPDATE document_pages SET text=?,char_count=?,text_sha256=?,extracted_at=? WHERE paper_id=? AND page_number=1", (updated_text, len(updated_text), hashlib.sha256(updated_text.encode()).hexdigest(), app.now_iso(), paper_id))
            self.assertEqual(app.get_semantic_index_status(paper_id)["stale_pages"], 1)
            third_job = create_job()
            app.run_semantic_index_job(third_job)
            self.assertEqual(mocked.call_count, calls_before + 1)
            self.assertEqual(app.get_semantic_index_status(paper_id)["stale_pages"], 0)

    def test_semantic_request_falls_back_to_bm25_when_embeddings_are_unconfigured(self):
        paper_id, _ = app.upsert_paper({"title": "BM25 semantic fallback fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("semantic-fallback", paper_id, "fixture.pdf", "semantic-fallback.pdf", "semantic-fallback-hash", 10, now))
            text = "Food volume estimation uses a visual benchmark and meal images."
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("semantic-fallback", paper_id, 1, text, len(text), "good", "fixture", hashlib.sha256(text.encode()).hexdigest(), now))
        details = app.search_document_pages_detailed(paper_id, "food volume", semantic=True)
        self.assertEqual(details["retrieval_mode"], "bm25_fallback")
        self.assertIn("BM25", details["retrieval_warning"])
        self.assertEqual(details["pages"][0]["retrieval_method"], "bm25_all_terms")

    def test_openai_compatible_embedding_request_uses_configured_endpoint_and_key(self):
        settings = {"embedding_model": "fixture-embed", "embedding_api_url": "https://relay.example/v9/vectorize"}
        response = io.BytesIO(json.dumps({"data": [{"index": 0, "embedding": [0.25, 0.75]}]}).encode())
        with patch.object(app, "open_compatible_model_request", return_value=response) as request:
            vectors, dimensions = app.request_embeddings(["multilingual paper passage"], settings, "fixture-api-key")
        self.assertEqual(vectors, [[0.25, 0.75]])
        self.assertEqual(dimensions, 2)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://relay.example/v9/vectorize")
        self.assertEqual(args[1], {"model": "fixture-embed", "input": ["multilingual paper passage"]})
        self.assertEqual(args[2], "fixture-api-key")

    def test_interrupted_semantic_index_is_requeued_at_startup(self):
        paper_id, _ = app.upsert_paper({"title": "Restartable semantic index fixture"}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO semantic_index_jobs(id,paper_id,model,status,stage,message,error,created_at,updated_at,completed_at) VALUES(?,?,?,'interrupted','interrupted','stopped','服务重启时任务未完成',?,?,?)", ("semantic-restart-fixture", paper_id, "embedding-model-id", now, now, now))
        with patch.object(app, "_enqueue_semantic_index_job") as enqueue:
            app.resume_semantic_index_jobs()
        enqueue.assert_called_once_with("semantic-restart-fixture")
        self.assertEqual(app.get_semantic_index_job("semantic-restart-fixture")["status"], "queued")

    def test_partial_semantic_index_retries_only_pages_not_already_indexed(self):
        app.save_model_settings({
            "provider": "embedding-retry-fixture", "base_url": "https://provider.example/v1",
            "api_url": "https://provider.example/v1/chat/completions", "model": "fixture-chat",
            "embedding_model": "fixture-embedding", "api_key": "fixture-key", "enabled": True,
        })
        paper_id, _ = app.upsert_paper({"title": "Semantic retry fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("semantic-retry", paper_id, "fixture.pdf", "semantic-retry.pdf", "semantic-retry-hash", 10, now))
            for page, text in enumerate(("Page one has sufficiently useful extracted evidence.", "Page two contains independent evidence for retry."), 1):
                db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("semantic-retry", paper_id, page, text, len(text), "good", "fixture", hashlib.sha256(text.encode()).hexdigest(), now))

        def create_job():
            job_id = "semantic-retry-job-" + str(time.time_ns())
            index_model = app._embedding_index_model(app.model_settings())
            with app.db_session() as db:
                db.execute("INSERT INTO semantic_index_jobs(id,paper_id,model,status,stage,progress_total,message,created_at,updated_at) VALUES(?,?,?,'queued','queued',2,'fixture',?,?)", (job_id, paper_id, index_model, now, now))
            return job_id

        attempts = {"count": 0}
        def flaky_embeddings(values, settings=None, api_key=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary provider failure")
            return [[1.0, 0.0] for _ in values], 2

        app.STOP_EVENT.clear()
        with patch.object(app, "request_embeddings", side_effect=flaky_embeddings):
            failed_id = create_job()
            app.run_semantic_index_job(failed_id)
            self.assertEqual(app.get_semantic_index_job(failed_id)["status"], "partial")
            self.assertEqual(app.get_semantic_index_status(paper_id)["stale_pages"], 2)
            retry_id = create_job()
            app.run_semantic_index_job(retry_id)
        self.assertEqual(app.get_semantic_index_job(retry_id)["status"], "success")
        self.assertEqual(app.get_semantic_index_status(paper_id)["indexed_pages"], 2)
        self.assertEqual(attempts["count"], 2)

    def test_semantic_index_http_workflow_is_async_and_reports_progress(self):
        app.save_model_settings({
            "provider": "embedding-api-fixture", "base_url": "https://provider.example/v1",
            "api_url": "https://provider.example/v1/chat/completions", "model": "fixture-chat",
            "embedding_model": "fixture-embedding", "embedding_api_url": "https://provider.example/v1/embeddings",
            "api_key": "fixture-key", "enabled": True,
        })
        paper_id, _ = app.upsert_paper({"title": "Semantic API workflow fixture", "year": 2026}, source="manual")
        text = "A visual portion estimator uses plate geometry and food segmentation."
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("semantic-api-fixture", paper_id, "fixture.pdf", "semantic-api-fixture.pdf", "semantic-api-fixture-hash", 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("semantic-api-fixture", paper_id, 1, text, len(text), "good", "fixture", hashlib.sha256(text.encode()).hexdigest(), now))

        def fake_embeddings(values, settings=None, api_key=None):
            return [[1.0, 0.0] for _ in values], 2

        app.STOP_EVENT.clear()
        with patch.object(app, "request_embeddings", side_effect=fake_embeddings):
            server, thread, base = self._start_api_server()
            try:
                semantic_url = base + f"/api/papers/{quote(paper_id, safe='')}/semantic-index"
                denied = Request(semantic_url, data=json.dumps({"consent": False}).encode(), method="POST", headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(denied, timeout=10)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                submitted = self._api_json(base, f"/api/papers/{quote(paper_id, safe='')}/semantic-index", "POST", {"consent": True}, expected_status=202)
                job_id = submitted["job"]["id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = self._api_json(base, f"/api/semantic-index-jobs/{quote(job_id, safe='')}")
                    if status["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.02)
                self.assertEqual(status["status"], "success")
                self.assertEqual(status["progress_current"], 1)
                self.assertEqual(status["progress_total"], 1)
                result = self._api_json(base, f"/api/papers/{quote(paper_id, safe='')}/pages?q=dietary%20intake%20quantification%20smartphone%20photograph&semantic=1")
                self.assertEqual(result["retrieval_mode"], "hybrid")
                self.assertEqual(result["pages"][0]["page_number"], 1)
                self.assertEqual(result["pages"][0]["retrieval_method"], "semantic")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_pdf_batch_ingest_creates_library_record_and_deduplicates(self):
        if __import__("importlib").util.find_spec("reportlab") is None:
            self.skipTest("reportlab is only used to build the PDF fixture")
        from io import BytesIO
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 750, "A Practical Multimodal Retrieval Study")
        pdf.drawString(72, 730, "2024")
        pdf.drawString(72, 710, "We evaluate a method using visual evidence and retrieval benchmarks. " * 6)
        pdf.save()
        content = buffer.getvalue()
        original = (app.model_settings, app.model_api_key)
        app.model_settings = lambda: {"enabled": False}
        app.model_api_key = lambda: ""
        server, thread, base = self._start_api_server()
        try:
            boundary = "LitLoomPdfTestBoundary"
            body = b""
            for filename in ("study.pdf", "study-copy.pdf"):
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n").encode() + content + b"\r\n"
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"auto_ai\"\r\n\r\nfalse\r\n--{boundary}--\r\n").encode()
            request = Request(base + "/api/pdf-ingest", data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 202)
                job = json.loads(response.read().decode("utf-8"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self._api_json(base, f"/api/pdf-ingest/{job['id']}")
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["completed"], 2)
            self.assertEqual(job["duplicates"], 1)
            self.assertEqual(job["items"][0]["paper_id"], job["items"][1]["paper_id"])
            self.assertEqual(job["items"][0]["ai_status"], "skipped")
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 1)
                self.assertGreater(db.execute("SELECT COUNT(*) FROM document_pages").fetchone()[0], 0)
                paper = db.execute("SELECT title,year FROM papers").fetchone()
            self.assertIn("Multimodal Retrieval", paper["title"])
            self.assertEqual(paper["year"], 2024)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            app.model_settings, app.model_api_key = original

    def test_watched_pdf_folder_scans_new_files_only_once(self):
        if __import__("importlib").util.find_spec("reportlab") is None:
            self.skipTest("reportlab is only used to build the PDF fixture")
        from io import BytesIO
        from reportlab.pdfgen import canvas

        folder = Path(self.temp.name) / "watched-papers"
        folder.mkdir()
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 750, "A Folder Monitored PDF Import Study")
        pdf.drawString(72, 730, "2023")
        pdf.drawString(72, 710, "Text extraction from the selected paper folder. " * 6)
        pdf.save()
        (folder / "monitored.pdf").write_bytes(buffer.getvalue())
        original = (app.model_settings, app.model_api_key)
        app.model_settings = lambda: {"enabled": False}
        app.model_api_key = lambda: ""
        try:
            source = app.register_paper_folder({"path": str(folder), "recursive": True, "enabled": True})
            first = app.scan_paper_folder(source["id"])
            self.assertTrue(first.get("id"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = app.pdf_ingest_job_snapshot(first["id"])
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            second = app.scan_paper_folder(source["id"])
            self.assertEqual(second["total"], 0)
            self.assertTrue((folder / "monitored.pdf").is_file())
            with app.db_session() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pdf_ingest_items WHERE source_path<>''").fetchone()[0], 1)
        finally:
            app.model_settings, app.model_api_key = original

    def test_metadata_draft_matches_crossref_by_title(self):
        original = app.http_json
        app.http_json = lambda _url, timeout=0: {"message": {"items": [{"title": ["Multimodal Retrieval for Evidence"], "author": [{"given": "Lin", "family": "Researcher"}], "published-print": {"date-parts": [[2025]]}, "DOI": "10.1234/example", "container-title": ["Research Journal"], "URL": "https://doi.org/10.1234/example"}]}}
        try:
            draft = app.complete_paper_metadata_draft({"title": "Multimodal Retrieval for Evidence"})
        finally:
            app.http_json = original
        self.assertEqual(draft["source"], "Crossref")
        self.assertEqual(draft["fields"]["year"], 2025)
        self.assertEqual(draft["fields"]["doi"], "10.1234/example")
        self.assertIn("Lin Researcher", draft["fields"]["authors"])

    def test_metadata_draft_background_route_returns_before_work_and_recovers_result(self):
        started = threading.Event()
        release = threading.Event()
        original = app.complete_paper_metadata_draft

        def delayed_completion(data, progress_callback=None):
            if progress_callback:
                progress_callback("model_request", 1, 2, "模型处理中")
            started.set()
            release.wait(3)
            return {"fields": {"title": data["title"], "year": 2025}, "source": "AI 引用识别", "warning": "核对来源", "partial": False}

        app.complete_paper_metadata_draft = delayed_completion
        server, thread, base = self._start_api_server()
        try:
            submitted = self._api_json(base, "/api/papers/metadata-draft", "POST", {"background": True, "title": "Persistent metadata task", "citation": "Persistent metadata task, 2025"}, expected_status=202)
            job_id = submitted["job"]["id"]
            self.assertTrue(started.wait(1))
            running = self._api_json(base, f"/api/metadata-draft-jobs/{job_id}")
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["stage"], "model_request")
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                completed = self._api_json(base, f"/api/metadata-draft-jobs/{job_id}")
                if completed["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(completed["status"], "success", completed.get("error") or completed)
            self.assertEqual(completed["result"]["fields"]["year"], 2025)
            self.assertNotIn("input_json", completed)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.complete_paper_metadata_draft = original

    def test_metadata_draft_retry_reuses_saved_input_after_failure(self):
        original = app.complete_paper_metadata_draft
        calls = []

        def flaky_completion(data, progress_callback=None):
            calls.append(data["title"])
            if len(calls) == 1:
                raise ValueError("temporary model error")
            return {"fields": {"title": data["title"]}, "source": "AI 引用识别", "warning": "核对来源", "partial": False}

        app.complete_paper_metadata_draft = flaky_completion
        try:
            submitted = app.submit_metadata_draft_job({"title": "Retry metadata task", "citation": "Retry metadata task, 2025"})
            job_id = submitted["job"]["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = app.get_metadata_draft_job(job_id)
                if job["status"] == "failed":
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "failed")
            retried = app.retry_metadata_draft_job(job_id)
            self.assertEqual(retried["job"]["status"], "queued")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = app.get_metadata_draft_job(job_id)
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(calls, ["Retry metadata task", "Retry metadata task"])
        finally:
            app.complete_paper_metadata_draft = original

    def test_metadata_draft_keeps_crossref_fields_when_model_extraction_fails(self):
        original = (app.crossref_metadata, app.model_settings, app.model_api_key, app.model_json_request)
        app.crossref_metadata = lambda _doi: {"title": "Verified title", "year": 2024, "doi": "10.1234/verified"}
        app.model_settings = lambda: {"enabled": True, "model": "fixture", "protocol": "chat_completions", "api_url": "https://model.example/v1/chat/completions", "base_url": "https://model.example/v1"}
        app.model_api_key = lambda: "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid model JSON"))
        try:
            result = app.complete_paper_metadata_draft({"doi": "10.1234/verified", "citation": "Verified title, 2024"})
        finally:
            app.crossref_metadata, app.model_settings, app.model_api_key, app.model_json_request = original
        self.assertTrue(result["partial"])
        self.assertEqual(result["fields"]["title"], "Verified title")
        self.assertEqual(result["fields"]["year"], 2024)
        self.assertIn("AI 引用识别未完成", result["warning"])

    def test_pdf_ai_failure_does_not_block_import_and_can_retry(self):
        if __import__("importlib").util.find_spec("reportlab") is None:
            self.skipTest("reportlab is only used to build the PDF fixture")
        from io import BytesIO
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 750, "Recoverable PDF Metadata Enrichment Test")
        pdf.drawString(72, 730, "Methods, evidence, and benchmark evaluation. " * 8)
        pdf.save()
        content = buffer.getvalue()
        original = (app.model_settings, app.model_api_key, app.model_json_request)
        app.model_settings = lambda: {"enabled": True}
        app.model_api_key = lambda: "fixture-key"
        calls = []

        def fake_model(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise ValueError("temporary model failure")
            return {"authors": "Researcher, Test", "year": 2024}

        app.model_json_request = fake_model
        try:
            job = app.create_pdf_ingest_job([{"filename": "recoverable.pdf", "data": base64.b64encode(content).decode("ascii")}], {"auto_ai": True})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = app.pdf_ingest_job_snapshot(job["id"])
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            item = job["items"][0]
            self.assertEqual(job["status"], "success")
            self.assertEqual(item["ai_status"], "failed")
            attachment_path = next(app.FILES_DIR.glob("*.pdf"))
            self.assertTrue(attachment_path.is_file())
            retried = app.retry_pdf_ingest_job(job["id"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                retried = app.pdf_ingest_job_snapshot(retried["id"])
                if retried["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(retried["items"][0]["ai_status"], "completed")
            self.assertTrue(attachment_path.is_file())
            with app.db_session() as db:
                paper = db.execute("SELECT authors,year FROM papers WHERE id=?", (item["paper_id"],)).fetchone()
            self.assertEqual(paper["authors"], "Researcher, Test")
            self.assertEqual(paper["year"], 2024)
        finally:
            app.model_settings, app.model_api_key, app.model_json_request = original

    def test_fulltext_ai_helpers_parse_json_chunk_pages_and_preserve_human_card(self):
        self.assertEqual(app.parse_model_json("```json\n{\"ok\": true}\n```"), {"ok": True})
        self.assertEqual(app.unwrap_model_json_layers({"analysis": {"result": {"ok": True}}}), {"ok": True})
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
        app.validate_fulltext_analysis("reading_card", {"reading_card": {"method": "有效方法", "results": "有效结果"}, "prereading_guide": {}})

    def test_deep_read_model_failure_keeps_exact_local_pdf_evidence(self):
        rows = [
            {"page_number": 1, "text": "We study a food volume estimation task. The method uses a convolutional encoder and attention."},
            {"page_number": 2, "text": "The experiment compares two baselines on a held-out test set."},
        ]
        fallback = app.build_local_deep_read_fallback(
            {"title": "Fixture paper"},
            rows,
            "HTTP 402（模型服务账户额度不足或计费未启用）",
        )
        self.assertEqual(fallback["analysis_status"], "partial")
        self.assertEqual(fallback["partial_kind"], "model_unavailable")
        self.assertTrue(fallback["partial_findings"])
        audit = app.validate_ai_evidence(fallback, rows, set())
        self.assertGreater(audit["verified_text"], 0)
        app.validate_fulltext_analysis("deep_read", fallback)
        self.assertIn("额度不足", " ".join(fallback["uncertainties"]))

    def test_model_error_detail_preserves_wrapped_provider_status_and_redacts_key(self):
        request = Request("https://model.example/v1/responses")
        provider_error = HTTPError(request.full_url, 402, "Payment Required", {}, io.BytesIO(b'{"error":"balance low"}'))
        wrapped = ValueError("读前准备补全失败：模型请求失败")
        wrapped.__cause__ = provider_error
        detail = app.model_error_detail(wrapped, "fixture-secret")
        self.assertIn("HTTP 402", detail)
        self.assertIn("额度不足", detail)
        self.assertIn("balance low", detail)
        self.assertNotIn("fixture-secret", detail)

    def test_json_contract_selects_complete_answer_after_schema_and_nested_objects(self):
        contract = {
            "required": ("overview", "research_question", "method", "main_findings"),
            "nonempty": ("overview", "research_question", "main_findings"),
            "any_nonempty": (("method.pipeline", "equation_analysis"),),
        }
        content = (
            '输出格式示例 {"overview":"","research_question":"","method":{"pipeline":""},"main_findings":[]}。'
            '中间证据对象 {"page":1,"quote":"source quote"}。'
            '最终答案 {"overview":"本文研究视觉特征与文本提示的融合。","research_question":"如何融合两类信息？",'
            '"method":{"pipeline":"提取图像特征后与文本提示共同编码。"},"main_findings":["融合两类信息完成目标任务。"]}'
        )
        self.assertEqual(
            app.parse_model_json(content, json_contract=contract),
            {"overview": "本文研究视觉特征与文本提示的融合。", "research_question": "如何融合两类信息？", "method": {"pipeline": "提取图像特征后与文本提示共同编码。"}, "main_findings": ["融合两类信息完成目标任务。"]},
        )
        with self.assertRaisesRegex(ValueError, "缺少必需字段或核心内容"):
            app.parse_model_json(
                '输出格式示例 {"overview":"","research_question":"","method":{"pipeline":""},"main_findings":[]}。'
                '{"page":1,"quote":"source quote"}',
                allow_repair=False,
                json_contract=contract,
            )

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

    def test_deep_read_samples_visual_pages_when_text_has_no_visual_cues(self):
        rows = [
            {"attachment_id": "att", "page_number": page, "text": f"正文第 {page} 页", "quality": "good"}
            for page in range(1, 10)
        ]
        selected = app.select_visual_page_rows(rows, 3, mode="deep_read")
        self.assertEqual([row["page_number"] for row in selected], [1, 5, 9])
        fast = app.select_visual_page_rows(rows, 3, mode="reading_card")
        self.assertEqual([row["page_number"] for row in fast], [1])

    def test_deep_read_visual_recheck_only_selects_rendered_cue_pages_without_observations(self):
        self.assertEqual(app.visual_page_cue_score("Experimental results are discussed."), 0)
        rows = [
            {"attachment_id": "att", "page_number": 8, "text": "Figure 1 compares the ablation results."},
            {"attachment_id": "att", "page_number": 9, "text": "Table 2 reports the benchmark results."},
            {"attachment_id": "att", "page_number": 10, "text": "The method is discussed in detail."},
        ]
        visual = {"images": [
            {"attachment_id": "att", "page": 8},
            {"attachment_id": "att", "page": 9},
            {"attachment_id": "att", "page": 10},
        ]}
        selected = app.select_visual_recheck_rows(
            rows,
            {"visual_observations": [{"attachment_id": "att", "page": 9, "type": "table"}]},
            visual,
            4,
        )
        self.assertEqual([row["page_number"] for row in selected], [8])

    def test_deep_read_visual_recheck_respects_page_budget_and_page_only_observation(self):
        rows = [
            {"attachment_id": "att", "page_number": page, "text": f"Figure {page} shows experimental results."}
            for page in range(1, 6)
        ]
        visual = {"images": [{"attachment_id": "att", "page": page} for page in range(1, 6)]}
        selected = app.select_visual_recheck_rows(rows, {"visual_observations": [{"page": 1}]}, visual, 2)
        self.assertEqual([row["page_number"] for row in selected], [2, 3])

    def test_deep_read_visual_recheck_prioritizes_transient_model_failures(self):
        rows = [
            {"attachment_id": "att", "page_number": 3, "text": "Figure 3 shows the ablation results."},
            {"attachment_id": "att", "page_number": 8, "text": "Table 8 reports the benchmark results."},
        ]
        # The first page was rendered successfully but its model response was
        # empty; the caller removes the image from the successful list while
        # preserving this failure record for a bounded retry.
        visual = {
            "images": [{"attachment_id": "att", "page": 8}],
            "failures": [{"attachment_id": "att", "page": 3, "error": "视觉页面分析失败：模型输出为空，未生成可用的结构化结果"}],
        }
        selected = app.select_visual_recheck_rows(rows, {"visual_observations": []}, visual, 1)
        self.assertEqual([row["page_number"] for row in selected], [3])

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

    def test_evidence_verification_normalizes_pdf_ligatures_and_line_wrap_hyphenation(self):
        analysis = {"evidence_spans": [
            {"page": 1, "quote": "The efficient multimodal model uses affine features."},
            {"page": 1, "quote": "This invented sentence is not in the PDF."},
        ], "uncertainties": []}
        audit = app.validate_ai_evidence(analysis, [{
            "page_number": 1,
            "text": "The ef\ufb01cient multi-\nmodal model uses af\ufb01ne features.",
        }])
        self.assertEqual(len(analysis["evidence_spans"]), 1)
        self.assertEqual(analysis["evidence_spans"][0]["quote"], "The efficient multimodal model uses affine features.")
        self.assertEqual(audit["verified_text"], 1)
        self.assertEqual(audit["removed_text"], 1)
        self.assertEqual(audit["quote_mismatch"], 1)

    def test_local_deep_read_evidence_uses_exact_quotes_and_marks_claim_link_as_inferred(self):
        analysis = {
            "research_question": "How can a project-specific model learn species labels from reviewer corrections?",
            "method": {"pipeline": "Train logistic regression on SpeciesNet feature vectors as reviewers correct species labels."},
            "main_findings": ["On Oregon Critters, the project model exceeded SpeciesNet with ten labels per category."],
        }
        rows = [{
            "page_number": 9,
            "text": "WildObserve uses a logistic regression on SpeciesNet feature vectors as reviewers confirm or correct labels. "
                    "On the Oregon Critters data-\nset, the project model exceeded SpeciesNet with only ten labels per category.",
        }]

        evidence = app.extract_local_deep_read_evidence(analysis, rows)
        verified = {"evidence_spans": evidence, "uncertainties": []}
        audit = app.validate_ai_evidence(verified, rows)

        self.assertGreaterEqual(len(verified["evidence_spans"]), 1)
        self.assertTrue(all(item["support"] == "inferred" for item in verified["evidence_spans"]))
        self.assertTrue(all("需人工确认" in item["claim"] for item in verified["evidence_spans"]))
        self.assertTrue(all(app._normalize_evidence_match(item["quote"]) in app._normalize_evidence_match(rows[0]["text"]) for item in verified["evidence_spans"]))
        self.assertEqual(audit["verified_text"], len(verified["evidence_spans"]))

    def test_local_deep_read_evidence_bridges_chinese_model_claims_to_english_pdf(self):
        analysis = {
            "research_question": "如何使用深度学习估计食物体积？",
            "method": {"pipeline": "采用卷积和注意力机制，从单张深度图估计食物体积。"},
            "main_findings": ["模型能够完成食物体积估计。"],
        }
        quote = (
            "The proposed deep learning model uses a convolutional neural network and an attention mechanism "
            "to estimate food volume from a single depth map."
        )
        rows = [{"page_number": 4, "text": quote}]

        evidence = app.extract_local_deep_read_evidence(analysis, rows)
        verified = {"evidence_spans": evidence, "uncertainties": []}
        audit = app.validate_ai_evidence(verified, rows)

        self.assertGreaterEqual(len(verified["evidence_spans"]), 1)
        self.assertEqual(audit["verified_text"], len(verified["evidence_spans"]))
        self.assertTrue(all(item["support"] == "inferred" for item in verified["evidence_spans"]))
        self.assertTrue(all("需人工确认" in item["claim"] for item in verified["evidence_spans"]))
        self.assertTrue(all(item["page"] == 4 for item in verified["evidence_spans"]))

    def test_chunk_evidence_validation_rejects_quotes_from_pages_outside_the_chunk(self):
        analysis = {"evidence_spans": [
            {"claim": "valid chunk quote", "page": 4, "quote": "Evidence from page four."},
            {"claim": "cross chunk quote", "page": 5, "quote": "Evidence from page five."},
        ], "uncertainties": []}
        chunk_rows = [{"attachment_id": "att", "page_number": 4, "text": "Evidence from page four."}]

        audit = app.validate_ai_evidence(analysis, chunk_rows)

        self.assertEqual(len(analysis["evidence_spans"]), 1)
        self.assertEqual(analysis["evidence_spans"][0]["page"], 4)
        self.assertEqual(audit["verified_text"], 1)
        self.assertEqual(audit["removed_text"], 1)
        self.assertEqual(audit["quote_mismatch"], 1)

    def test_evidence_audit_reports_unique_page_coverage_and_chunk_removals(self):
        analysis = {"evidence_spans": [
            {"page": 1, "quote": "Page one quote."},
            {"page": 3, "quote": "Page three quote."},
        ], "visual_evidence": [], "uncertainties": []}
        rows = [
            {"attachment_id": "att", "page_number": 1, "text": "Page one quote."},
            {"attachment_id": "att", "page_number": 2, "text": "Page two has text."},
            {"attachment_id": "att", "page_number": 3, "text": "Page three quote."},
            {"attachment_id": "att-copy", "page_number": 3, "text": "Duplicate page number."},
            {"attachment_id": "att", "page_number": 4, "text": "   "},
        ]

        report = app.deep_read_evidence_quality(analysis, rows, set(), {"removed_text": 1, "chunk_removed_text": 2})

        self.assertEqual(report["source_text_pages"], 3)
        self.assertEqual(report["verified_text_page_count"], 2)
        self.assertEqual(report["text_page_coverage"], 0.6667)
        self.assertEqual(report["removed_citations"], 3)

    def test_visual_analysis_only_keeps_claims_with_verified_rendered_page_evidence(self):
        analysis = {
            "visual_analysis": {
                "figures": [{"id": "Figure 1", "page": 6, "description": "Verified pipeline diagram."}],
                "tables": [
                    {"id": "Table 2", "page": 16, "description": "Verified result table."},
                    {"id": "Table S1", "page": 26, "description": "Text-only table mention."},
                ],
                "formulas": ["An equation claim without a page."],
                "diagrams": [],
                "visual_uncertainties": [],
            },
            "visual_evidence": [
                {"attachment_id": "att", "page": 6, "type": "diagram", "description": "Verified pipeline diagram."},
                {"attachment_id": "att", "page": 16, "type": "table", "description": "Verified result table."},
            ],
            "uncertainties": [],
        }

        removed = app.restrict_visual_analysis_to_verified_pages(
            analysis,
            {("att", 6), ("att", 16), ("att", 26)},
        )

        self.assertEqual(removed, 2)
        self.assertEqual([item["id"] for item in analysis["visual_analysis"]["figures"]], ["Figure 1"])
        self.assertEqual([item["id"] for item in analysis["visual_analysis"]["tables"]], ["Table 2"])
        self.assertEqual(analysis["visual_analysis"]["formulas"], [])
        self.assertEqual(analysis["visual_analysis"]["verified_pages"], [6, 16])
        self.assertTrue(any("已移出视觉识别结果" in item for item in analysis["uncertainties"]))

    def test_text_rich_deep_read_without_verified_quotes_is_partial_not_complete(self):
        analysis = {
            "overview": "A structured result.", "research_question": "A research question?",
            "method": {"pipeline": "A method."}, "main_findings": ["A result."],
            "evidence_spans": [], "visual_evidence": [{"page": 2, "type": "figure"}],
            "partial_findings": [], "uncertainties": [],
        }
        rows = [{"page_number": 1, "text": "Extracted article body. " * 15}]
        report = app.deep_read_evidence_quality(analysis, rows, {("att", 2)}, {"removed_text": 4})
        self.assertEqual(analysis["analysis_status"], "partial")
        self.assertEqual(analysis["partial_kind"], "evidence_gap")
        self.assertFalse(analysis["synthesis_retry_available"])
        self.assertEqual(report["verified_text_citations"], 0)
        self.assertEqual(report["removed_citations"], 4)
        self.assertEqual(report["rendered_visual_pages"], 1)
        self.assertEqual(report["visual_observations"], 1)

    def test_visual_only_deep_read_can_complete_when_pdf_has_no_extractable_body_text(self):
        analysis = {"evidence_spans": [], "visual_evidence": [{"page": 1, "type": "formula"}], "uncertainties": []}
        app.deep_read_evidence_quality(analysis, [{"page_number": 1, "text": ""}], {("att", 1)}, {})
        self.assertNotIn("analysis_status", analysis)
        self.assertEqual(analysis["evidence_audit"]["visual_observations"], 1)

    def test_deep_read_restores_page_visual_observations_dropped_by_final_synthesis(self):
        analysis = {"visual_evidence": [], "uncertainties": []}
        notes = [{"visual_observations": [
            {"type": "formula", "attachment_id": "att", "page": 3, "title": "Loss function", "description": "A weighted objective", "transcription_latex": "L = L_a + L_b", "confidence": "medium"},
            {"type": "formula", "attachment_id": "att", "page": 3, "title": "Loss function", "description": "A weighted objective", "transcription_latex": "L = L_a + L_b", "confidence": "medium"},
        ]}]

        app.merge_chunk_visual_evidence(analysis, notes)
        app.validate_ai_evidence(analysis, [{"attachment_id": "att", "page_number": 3, "text": ""}], {("att", 3)})

        self.assertEqual(len(analysis["visual_evidence"]), 1)
        self.assertEqual(analysis["visual_evidence"][0]["claim"], "Loss function")
        self.assertEqual(analysis["visual_evidence"][0]["attachment_id"], "att")
        self.assertEqual(analysis["visual_evidence"][0]["page"], 3)

    def test_visual_observation_normalizer_accepts_singular_provider_object(self):
        row = {"attachment_id": "singular-visual", "page_number": 6}
        result = {
            "type": "table",
            "page": 6,
            "description": "A comparison table returned as a top-level object.",
            "transcription_latex": "",
        }

        observations = app.normalize_visual_observations(result, row)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["type"], "table")
        self.assertEqual(observations[0]["attachment_id"], "singular-visual")
        self.assertEqual(observations[0]["page"], 6)

    def test_visual_observation_normalizer_accepts_singular_visual_evidence_object(self):
        row = {"attachment_id": "enveloped-visual", "page_number": 4}
        result = {"visual_evidence": {"type": "formula", "description": "A loss equation."}}

        observations = app.normalize_visual_observations(result, row)

        self.assertEqual(observations[0]["type"], "formula")
        self.assertEqual(observations[0]["attachment_id"], "enveloped-visual")
        self.assertEqual(observations[0]["page"], 4)

    def test_deep_read_pipeline_persists_chunk_visual_evidence_after_empty_synthesis(self):
        paper_id, _ = app.upsert_paper({"title": "Visual synthesis recovery fixture"}, source="manual")
        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "prepare_visual_pages": app.prepare_visual_pages,
            "model_multimodal_json_request": app.model_multimodal_json_request,
            "model_json_request": app.model_json_request,
            "model_settings": app.model_settings,
        }
        quote = "The objective combines two losses."
        rows = [{"attachment_id": "visual-recovery-attachment", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "f" * 64}]
        observation = {
            "type": "formula", "attachment_id": "visual-recovery-attachment", "page": 1,
            "title": "Combined objective", "description": "Two terms are added in the objective.",
            "transcription_latex": "L = L_a + L_b", "data_relationships": "The objective sums two losses.", "confidence": "medium",
        }

        def fake_visual(page_rows, max_pages=8):
            row = page_rows[0]
            return {"images": [{"attachment_id": row["attachment_id"], "page": row["page_number"], "sha256": "a" * 64, "data_url": "data:image/jpeg;base64,AA=="}], "failures": [], "rendered_pages": 1, "requested_pages": 1}

        def fake_multimodal(_prompt, _payload, _images, timeout=150, max_output_tokens=None):
            return {"notes": [{"category": "method", "finding": "The objective combines two losses.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [observation], "evidence_spans": [], "uncertainties": []}

        def fake_text(_prompt, _payload, timeout=120, max_output_tokens=None):
            return {"overview": "Two losses form the objective.", "research_question": "How is the objective defined?", "method": {"pipeline": "Optimize the combined objective."}, "visual_analysis": {"formulas": [], "figures": [], "tables": [], "diagrams": [], "visual_uncertainties": []}, "visual_evidence": [], "evidence_spans": [], "uncertainties": [], "reading_card": {}}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Visual synthesis recovery fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = fake_visual
        app.model_multimodal_json_request = fake_multimodal
        app.model_json_request = fake_text
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.prepare_visual_pages = original["prepare_visual_pages"]
            app.model_multimodal_json_request = original["model_multimodal_json_request"]
            app.model_json_request = original["model_json_request"]
            app.model_settings = original["model_settings"]

        self.assertEqual(result["visual_pages"], 1)
        self.assertEqual(result["prompt_version"], "multimodal-deep-read-v17")
        self.assertEqual(result["analysis"]["visual_evidence"][0]["transcription_latex"], "L = L_a + L_b")
        self.assertEqual(result["analysis"]["visual_evidence"][0]["page"], 1)
        self.assertEqual(result["analysis"]["evidence_spans"][0]["quote"], quote)
        with app.db_session() as db:
            artifact = db.execute("SELECT prompt_version,answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        saved = json.loads(artifact["answer"])
        self.assertEqual(artifact["prompt_version"], "multimodal-deep-read-v17")
        self.assertEqual(saved["meta"]["pipeline_version"], "fulltext-analysis-v21")
        self.assertEqual(saved["analysis"]["visual_evidence"][0]["attachment_id"], "visual-recovery-attachment")

    def test_deep_read_runs_text_chunks_and_visual_pages_concurrently(self):
        import threading

        paper_id, _ = app.upsert_paper({"title": "Parallel text and visual fixture"}, source="manual")
        quote = "Figure 1 shows the measured response curve."
        rows = []
        for page in (1, 2):
            text = f"{quote} The method uses visual evidence.\n" + (f"Page {page} background description. " * 450)
            rows.append({
                "attachment_id": f"parallel-attachment-{page}", "page_number": page, "text": text,
                "char_count": len(text), "quality": "good", "text_sha256": str(page) * 64,
            })

        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "prepare_visual_pages": app.prepare_visual_pages,
            "model_multimodal_json_request": app.model_multimodal_json_request,
            "model_json_request": app.model_json_request,
            "model_settings": app.model_settings,
            "separate_visual_min": app.DEEP_READ_SEPARATE_VISUAL_MIN_CHARS,
        }
        overlap = threading.Event()
        active_lock = threading.Lock()
        active_lanes = set()

        def mark_lane(lane):
            with active_lock:
                active_lanes.add(lane)
                if {"text", "visual"}.issubset(active_lanes):
                    overlap.set()
            overlap.wait(timeout=4)
            with active_lock:
                active_lanes.discard(lane)

        def fake_visual(page_rows, max_pages=8):
            row = page_rows[0]
            return {
                "images": [{"attachment_id": row["attachment_id"], "page": row["page_number"], "sha256": "a" * 64, "data_url": "data:image/jpeg;base64,AA=="}],
                "failures": [], "rendered_pages": 1, "requested_pages": 1,
            }

        def fake_multimodal(prompt, payload, _images, **_kwargs):
            self.assertIn("视觉证据抽取器", prompt)
            mark_lane("visual")
            page = payload["visual_context"]["page_refs"][0]["page"]
            return {"visual_observations": [{
                "type": "figure", "page": page, "title": f"Figure {page}",
                "description": "The page shows a measured response curve.",
                "data_relationships": "The plotted response varies across the page.", "confidence": "medium",
            }], "uncertainties": []}

        def fake_text(prompt, _payload, **_kwargs):
            if "研究生论文文本证据抽取器" in prompt:
                mark_lane("text")
                return {"notes": [{
                    "category": "results", "finding": "Figure 1 shows the measured response curve.",
                    "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}],
                }], "visual_observations": [], "evidence_spans": [], "uncertainties": []}
            return {
                "overview": "本文研究页面中的响应曲线。", "research_question": "响应曲线呈现什么结果？",
                "motivation": "需要理解页面报告的实验结果。", "contributions": ["分析响应曲线。"],
                "method": {"pipeline": "使用视觉证据分析页面中的曲线。", "components": [], "assumptions": [], "training_or_inference": "原文未说明"},
                "datasets": [], "experiments": [], "metrics": [], "main_findings": ["页面展示了响应曲线。"],
                "limitations": [], "reproducibility": {"code": "原文未说明", "data": "原文未说明", "environment": "原文未说明", "random_seed": "原文未说明", "missing_information": []},
                "critical_reading": {"strong_points": [], "weak_points": [], "possible_confounders": [], "questions_for_author": []},
                "evidence_spans": [{"claim": "页面展示响应曲线。", "page": 1, "quote": quote, "support": "direct"}],
                "prereading_guide": {"prerequisites": [], "glossary": [], "evidence": []}, "uncertainties": [],
                "reading_card": {field: "原文未说明" for field in app.READING_CARD_FIELDS},
            }

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Parallel text and visual fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = fake_visual
        app.model_multimodal_json_request = fake_multimodal
        app.model_json_request = fake_text
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        app.DEEP_READ_SEPARATE_VISUAL_MIN_CHARS = 1000
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.prepare_visual_pages = original["prepare_visual_pages"]
            app.model_multimodal_json_request = original["model_multimodal_json_request"]
            app.model_json_request = original["model_json_request"]
            app.model_settings = original["model_settings"]
            app.DEEP_READ_SEPARATE_VISUAL_MIN_CHARS = original["separate_visual_min"]

        self.assertTrue(overlap.is_set(), f"正文模型请求与视觉模型请求必须有重叠执行: {result.get('visual_pages')} {result.get('model_calls')}")
        self.assertEqual(result["analysis_status"], "complete")
        self.assertEqual(result["visual_pages"], 2)
        self.assertEqual([item["page"] for item in result["analysis"]["visual_evidence"]], [1, 2])

    def test_deep_read_pipeline_recovers_exact_local_quotes_when_model_omits_citations(self):
        paper_id, _ = app.upsert_paper({"title": "Local quote recovery fixture"}, source="manual")
        rows = [
            {"page_number": 1, "text": "WildObserve trains a logistic regression on SpeciesNet feature vectors as reviewers confirm or correct labels."},
            {"page_number": 2, "text": "On the Oregon Critters data-\nset, the project model exceeded SpeciesNet with only ten labels per category."},
        ]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None, json_contract=None):
            if "研究生论文精读助手" in prompt:
                return {
                    "overview": "The paper trains a project-specific species model from review corrections.",
                    "research_question": "How can project labels be learned from reviewer corrections?",
                    "motivation": "Projects need their own species names.",
                    "method": {"pipeline": "Train logistic regression on SpeciesNet feature vectors as reviewers correct labels."},
                    "main_findings": ["On Oregon Critters, the project model exceeded SpeciesNet with ten labels per category."],
                    "limitations": [],
                    "critical_reading": {},
                    "visual_analysis": {},
                    "visual_evidence": [],
                    "evidence_spans": [],
                    "uncertainties": [],
                    "reading_card": {},
                }
            return {"notes": [], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Local quote recovery fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertEqual(result["pipeline_version"], "fulltext-analysis-v21")
        self.assertGreater(result["text_evidence_count"], 0)
        self.assertEqual(result["analysis"]["evidence_audit"]["text_evidence_strategy"], "local_exact_text")
        self.assertTrue(all(item["support"] == "inferred" for item in result["analysis"]["evidence_spans"]))
        self.assertTrue(any("支持关系仍需人工确认" in item for item in result["analysis"]["uncertainties"]))
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        saved = json.loads(artifact["answer"])
        self.assertEqual(saved["meta"]["analysis_status"], "complete")
        self.assertGreater(saved["analysis"]["evidence_audit"]["verified_text_page_count"], 0)

    def test_deep_read_downgrades_failed_502_visual_request_and_does_not_claim_visual_coverage(self):
        from io import BytesIO
        from urllib.error import HTTPError

        paper_id, _ = app.upsert_paper({"title": "Visual gateway fallback fixture"}, source="manual")
        quote = "The method combines two independently trained components."
        rows = [{"attachment_id": "visual-fallback-attachment", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "b" * 64}]
        original = (app.get_paper_pages_for_ai, app.prepare_visual_pages, app.model_multimodal_json_request, app.model_json_request, app.model_settings)

        def fake_visual(page_rows, max_pages=8):
            row = page_rows[0]
            return {"images": [{"attachment_id": row["attachment_id"], "page": row["page_number"], "sha256": "c" * 64, "data_url": "data:image/jpeg;base64,AA=="}], "failures": [], "rendered_pages": 1, "requested_pages": 1}

        def fail_multimodal(*_args, **_kwargs):
            raise HTTPError("https://relay.example.test/v1", 502, "Bad Gateway", {}, BytesIO(b"openresty"))

        def fake_text(prompt, _payload, timeout=120, max_output_tokens=None, json_contract=None):
            if "研究生论文精读助手" in prompt:
                return {"overview": "本文组合两个独立训练的组件。", "research_question": "如何组合两个组件？", "method": {"pipeline": "分别训练后组合两个组件。"}, "main_findings": ["正文描述了两个组件的组合。"], "evidence_spans": [{"claim": "方法组合两个组件。", "page": 1, "quote": quote, "support": "direct"}], "visual_evidence": [], "uncertainties": [], "reading_card": {}}
            return {"notes": [{"category": "method", "finding": "The method combines two independently trained components.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Visual gateway fallback fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = fake_visual
        app.model_multimodal_json_request = fail_multimodal
        app.model_json_request = fake_text
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            (app.get_paper_pages_for_ai, app.prepare_visual_pages, app.model_multimodal_json_request, app.model_json_request, app.model_settings) = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertEqual(result["visual_pages"], 0)
        self.assertTrue(any("HTTP 502" in item["error"] for item in result["visual_failures"]))
        self.assertTrue(any("没有页面图像被视觉模型成功分析" in item for item in result["analysis"]["uncertainties"]))

    def test_deep_read_persists_verified_chunk_findings_when_final_synthesis_gets_502(self):
        from io import BytesIO
        from urllib.error import HTTPError

        paper_id, _ = app.upsert_paper({"title": "Synthesis gateway fallback fixture"}, source="manual")
        quote = "The model combines local features with a global attention layer."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "d" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None, json_contract=None):
            if "研究生论文精读助手" in prompt:
                raise HTTPError("https://relay.example.test/v1", 502, "Bad Gateway", {}, BytesIO(b"openresty"))
            return {"notes": [{"category": "method", "finding": "The model combines local features with global attention.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Synthesis gateway fallback fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "partial")
        self.assertEqual(result["analysis"]["analysis_status"], "partial")
        self.assertIn("HTTP 502", result["analysis"]["partial_reason"])
        self.assertEqual(result["analysis"]["partial_findings"][0]["evidence_spans"][0]["quote"], quote)
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        saved = json.loads(artifact["answer"])
        self.assertEqual(saved["meta"]["analysis_status"], "partial")
        self.assertEqual(saved["analysis"]["evidence_spans"][0]["quote"], quote)

    def test_deep_read_rechecks_a_figure_page_missed_by_first_visual_pass(self):
        paper_id, _ = app.upsert_paper({"title": "Visual recheck fixture"}, source="manual")
        quote = "Figure 1 compares the model variants."
        attachment_id = "visual-recheck-attachment"
        rows = [{"attachment_id": attachment_id, "page_number": 8, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "a" * 64}]
        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "prepare_visual_pages": app.prepare_visual_pages,
            "model_multimodal_json_request": app.model_multimodal_json_request,
            "model_json_request": app.model_json_request,
            "model_settings": app.model_settings,
        }
        multimodal_prompts = []

        def fake_visual(page_rows, max_pages=8):
            row = page_rows[0]
            return {"images": [{"attachment_id": row["attachment_id"], "page": row["page_number"], "sha256": "b" * 64, "data_url": "data:image/jpeg;base64,AA=="}], "failures": [], "rendered_pages": 1, "requested_pages": 1}

        def fake_multimodal(prompt, _payload, _images, timeout=150, max_output_tokens=None):
            multimodal_prompts.append(prompt)
            if "补查器" in prompt:
                return {"visual_observations": [{"type": "figure", "page": 8, "title": "Figure 1", "description": "The plot compares model variants.", "data_relationships": "Several model variants are compared.", "confidence": "high"}], "uncertainties": []}
            return {"notes": [{"category": "results", "finding": "Figure 1 compares the model variants.", "evidence_spans": [{"page": 8, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        def fake_text(_prompt, _payload, timeout=150, max_output_tokens=None, json_contract=None):
            return {"overview": "The paper compares model variants.", "research_question": "How do the model variants compare?", "method": {"pipeline": "Compare the model variants."}, "main_findings": ["The paper reports a comparison."], "visual_evidence": [], "evidence_spans": [{"claim": "Figure 1 compares the model variants.", "page": 8, "quote": quote}], "uncertainties": [], "reading_card": {}}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Visual recheck fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = fake_visual
        app.model_multimodal_json_request = fake_multimodal
        app.model_json_request = fake_text
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.prepare_visual_pages = original["prepare_visual_pages"]
            app.model_multimodal_json_request = original["model_multimodal_json_request"]
            app.model_json_request = original["model_json_request"]
            app.model_settings = original["model_settings"]

        self.assertEqual(len(multimodal_prompts), 2)
        self.assertEqual(result["visual_recheck_attempt_count"], 1)
        self.assertEqual(result["visual_recheck_count"], 1)
        chunk_call = next(item for item in result["model_calls"] if item["phase"] == "chunk_analysis")
        recheck_call = next(item for item in result["model_calls"] if item["phase"] == "visual_recheck")
        self.assertEqual(chunk_call["pages"], [8])
        self.assertEqual(recheck_call["pages"], [8])
        self.assertEqual(result["analysis"]["visual_evidence"][0]["page"], 8)
        self.assertEqual(result["analysis"]["visual_evidence"][0]["attachment_id"], attachment_id)
        self.assertEqual(result["prompt_version"], "multimodal-deep-read-v17")
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(artifact["answer"])["analysis"]["visual_evidence"][0]["type"], "figure")

    def test_deep_read_repairs_an_empty_synthesis_once_and_persists_complete_output(self):
        paper_id, _ = app.upsert_paper({"title": "Synthesis repair fixture"}, source="manual")
        quote = "The method combines visual and textual evidence."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "1" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings
        calls = []

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None):
            calls.append(prompt)
            if "证据综合器" in prompt:
                return {"overview": "本文融合视觉与文本证据。", "research_question": "如何联合两种证据？", "method": {"pipeline": "分别提取并融合两种模态。"}, "main_findings": ["融合两种证据用于任务预测。"], "evidence_spans": [{"claim": "方法联合视觉和文本证据", "page": 1, "quote": quote}]}
            if "研究生论文精读助手" in prompt:
                return {"overview": "", "research_question": "", "method": {}, "main_findings": [], "evidence_spans": [], "visual_evidence": []}
            return {"notes": [{"category": "method", "finding": "The method combines visual and textual evidence.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Synthesis repair fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertTrue(result["synthesis_repair_attempted"])
        self.assertTrue(result["synthesis_repair_succeeded"])
        self.assertEqual(result["analysis"]["overview"], "本文融合视觉与文本证据。")
        self.assertEqual(sum("证据综合器" in prompt for prompt in calls), 1)
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(artifact["answer"])["meta"]["analysis_status"], "complete")

    def test_deep_read_rejects_template_placeholders_as_completed_content(self):
        self.assertFalse(app._meaningful_model_value("2-3句论文概述"))
        self.assertFalse(app._meaningful_model_value("研究问题"))
        self.assertFalse(app._meaningful_model_value("方法主线"))
        with self.assertRaisesRegex(ValueError, "核心结构"):
            app.validate_fulltext_analysis("deep_read", {
                "overview": "2-3句论文概述",
                "research_question": "研究问题",
                "method": {"pipeline": "方法主线"},
                "main_findings": [],
                "evidence_spans": [{"page": 1, "quote": "text"}],
            })

    def test_reading_card_rejects_provider_ellipsis_and_empty_markers(self):
        for value in ("...", "…", "<...>", "原文未说明。", "待补充", "N/A"):
            self.assertFalse(app._meaningful_model_value(value), value)
        with self.assertRaisesRegex(ValueError, "有效字段不足"):
            app.validate_fulltext_analysis("reading_card", {
                "reading_card": {field: "..." for field in app.READING_CARD_FIELDS},
                "evidence": [],
            })
        with self.assertRaisesRegex(ValueError, "有效字段不足"):
            app.validate_fulltext_analysis("reading_card", {
                "reading_card": {field: "原文未说明。" for field in app.READING_CARD_FIELDS},
                "evidence": [],
            })

    def test_deep_read_synthesis_retry_reuses_saved_page_verified_evidence(self):
        paper_id, _ = app.upsert_paper({"title": "Synthesis-only retry fixture"}, source="manual")
        artifact_id = "synthesis-only-source-fixture"
        quote = "The method combines image features with text prompts."
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings
        calls = []
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "4" * 64}]
        saved_answer = {
            "analysis": {
                "analysis_status": "partial",
                "partial_findings": [{"category": "method", "finding": "The method combines image features with text prompts.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}],
                "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}],
                "visual_evidence": [], "prereading_guide": {}, "uncertainties": [],
            },
            "meta": {"analysis_status": "partial", "duration_ms": 120000, "chunk_count": 3, "pipeline_version": "fulltext-analysis-v11", "visual_page_refs": []},
        }
        with app.db_session() as db:
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "deep_read", "fixture", "fixture", "v11", "d" * 64, json.dumps(saved_answer, ensure_ascii=False), app.now_iso()))

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None):
            calls.append(prompt)
            return {
                "overview": "本文研究如何结合图像特征和文本提示。实验结果需回到论文相应章节核验。",
                "research_question": "图像特征与文本提示如何共同支持目标任务？",
                "method": {"pipeline": "模型先提取图像特征，再与文本提示共同处理。"},
                "main_findings": ["输入证据说明方法结合了图像特征与文本提示。"],
                "evidence_spans": [], "visual_evidence": [], "uncertainties": [],
            }

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Synthesis-only retry fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        stages = []
        try:
            result = app.run_deep_read_synthesis_retry(paper_id, artifact_id, lambda stage, _current, _total, _message: stages.append(stage))
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["analysis"]["evidence_spans"][0]["quote"], quote)
        self.assertIn("重新读取 PDF", calls[0])
        self.assertEqual(stages, ["synthesis_retry", "saving"])
        with app.db_session() as db:
            source = db.execute("SELECT 1 FROM ai_artifacts WHERE id=?", (artifact_id,)).fetchone()
            generated = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertTrue(source)
        self.assertTrue(json.loads(generated["answer"])["meta"]["synthesis_retry"])

    def test_deep_read_synthesis_retry_preserves_verified_partial_when_model_fails(self):
        paper_id, _ = app.upsert_paper({"title": "Synthesis retry fallback fixture"}, source="manual")
        artifact_id = "synthesis-retry-fallback-source"
        quote = "The model combines image features with text prompts."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "5" * 64}]
        saved_answer = {
            "analysis": {
                "analysis_status": "partial",
                "partial_findings": [{"category": "method", "finding": "The method combines image features with text prompts.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}],
                "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}],
                "visual_evidence": [], "prereading_guide": {}, "uncertainties": [],
            },
            "meta": {"analysis_status": "partial", "duration_ms": 1000, "chunk_count": 1, "visual_page_refs": []},
        }
        with app.db_session() as db:
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "deep_read", "fixture", "fixture", "v1", "e" * 64, json.dumps(saved_answer, ensure_ascii=False), app.now_iso()))

        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings
        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Synthesis retry fallback fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("fixture: invalid synthesis response"))
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        stages = []
        try:
            result = app.run_deep_read_synthesis_retry(paper_id, artifact_id, lambda stage, _current, _total, message: stages.append((stage, message)))
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "partial")
        self.assertEqual(result["analysis"]["partial_findings"][0]["finding"], "The method combines image features with text prompts.")
        self.assertEqual(result["analysis"]["evidence_spans"][0]["quote"], quote)
        self.assertIn("fixture: invalid synthesis response", result["synthesis_repair_error"])
        self.assertEqual(stages[-1][0], "saving")
        self.assertIn("可核验的分块结果", stages[-1][1])
        with app.db_session() as db:
            saved = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(saved["answer"])["meta"]["analysis_status"], "partial")

    def test_deep_read_repairs_pipeline_only_synthesis_and_records_response_shape(self):
        paper_id, _ = app.upsert_paper({"title": "Pipeline-only synthesis fixture"}, source="manual")
        quote = "The model combines image features with text prompts."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "3" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings
        prompts = []

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None):
            prompts.append(prompt)
            if "证据综合器" in prompt:
                return {
                    "overview": "本文研究如何将图像特征与文本提示结合。",
                    "research_question": "图像特征如何与文本提示共同用于模型任务？",
                    "method": {"pipeline": "模型提取图像特征，再与文本提示共同处理。", "algorithm_steps": [], "components": [], "assumptions": [], "training_or_inference": ""},
                    "main_findings": ["正文说明模型结合图像特征与文本提示。"],
                    "limitations": [], "critical_reading": {"strong_points": [], "weak_points": [], "possible_confounders": [], "questions_for_author": []},
                    "evidence_spans": [], "visual_evidence": [], "uncertainties": [],
                }
            if "研究生论文精读助手" in prompt:
                return {"pipeline": "模型提取图像特征，再与文本提示共同处理。"}
            return {"notes": [{"category": "method", "finding": "The model combines image features with text prompts.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Pipeline-only synthesis fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertTrue(result["synthesis_repair_attempted"])
        self.assertTrue(result["synthesis_repair_succeeded"])
        self.assertIn("不能只返回 pipeline", prompts[-1])
        self.assertEqual(result["analysis"]["method"]["pipeline"], "模型提取图像特征，再与文本提示共同处理。")
        self.assertIn("pipeline", result["model_calls"][1]["response_fields"])
        self.assertIn("main_findings", result["model_calls"][2]["response_fields"])

    def test_deep_read_saves_only_verified_partial_findings_when_synthesis_repair_is_empty(self):
        paper_id, _ = app.upsert_paper({"title": "Partial synthesis fixture"}, source="manual")
        quote = "The method combines visual and textual evidence."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "2" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings

        def fake_model(prompt, _payload, timeout=120, max_output_tokens=None):
            if "证据综合器" in prompt or "研究生论文精读助手" in prompt:
                return {"overview": "", "research_question": "", "method": {}, "main_findings": [], "evidence_spans": [], "visual_evidence": []}
            return {"notes": [{"category": "method", "finding": "The method combines visual and textual evidence.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Partial synthesis fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "partial")
        self.assertEqual(result["analysis"]["analysis_status"], "partial")
        self.assertEqual(len(result["analysis"]["partial_findings"]), 1)
        self.assertEqual(result["analysis"]["partial_findings"][0]["evidence_spans"][0]["quote"], quote)
        self.assertTrue(result["analysis"]["partial_reason"])
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(artifact["answer"])["meta"]["analysis_status"], "partial")

    def test_deep_read_retries_a_truncated_chunk_with_smaller_chunks(self):
        paper_id, _ = app.upsert_paper({"title": "Truncated chunk retry fixture"}, source="manual")
        quote = "The method combines convolution and attention."
        text = quote + "\n\n" + "Background detail supports the method.\n\n" * 280
        rows = [{"page_number": 1, "text": text, "char_count": len(text), "quality": "good", "text_sha256": "6" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings
        request_sizes = []

        def fake_model(prompt, payload, timeout=120, max_output_tokens=None, json_contract=None):
            pages = payload.get("pages", "") if isinstance(payload, dict) else ""
            request_sizes.append(len(pages))
            if "研究生论文精读助手" in prompt:
                return {
                    "overview": "本文分析卷积特征与注意力机制的结合。",
                    "research_question": "如何结合卷积与注意力提取特征？",
                    "method": {"pipeline": "先提取局部卷积特征，再通过注意力进行加权。"},
                    "main_findings": ["原文描述了卷积和注意力的组合方法。"],
                    "evidence_spans": [{"claim": "方法结合卷积与注意力。", "page": 1, "quote": quote, "support": "direct"}],
                    "visual_evidence": [], "uncertainties": [],
                }
            if len(pages) > 9000:
                raise ValueError("模型输出因达到输出上限而被截断")
            if quote in pages:
                return {"notes": [{"category": "method", "finding": "The method combines convolution and attention.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}
            return {"notes": [], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Truncated chunk retry fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "complete")
        self.assertGreaterEqual(len(request_sizes), 3)
        self.assertTrue(any(size > 9000 for size in request_sizes))
        self.assertTrue(any(0 < size <= 9000 for size in request_sizes))
        self.assertEqual(result["analysis"]["evidence_spans"][0]["quote"], quote)

    def test_deep_read_keeps_verified_results_partial_when_a_retried_subchunk_still_truncates(self):
        paper_id, _ = app.upsert_paper({"title": "Partially truncated chunk fixture"}, source="manual")
        quote = "The method combines convolution and attention."
        text = quote + "\n\n" + "Background detail supports the method.\n\n" * 180 + "FORCE_FAIL " + "Tail detail. " * 120
        rows = [{"page_number": 1, "text": text, "char_count": len(text), "quality": "good", "text_sha256": "7" * 64}]
        original = app.get_paper_pages_for_ai, app.model_json_request, app.model_settings

        def fake_model(prompt, payload, timeout=120, max_output_tokens=None, json_contract=None):
            pages = payload.get("pages", "") if isinstance(payload, dict) else ""
            if "研究生论文精读助手" in prompt:
                return {
                    "overview": "本文分析卷积特征与注意力机制的结合。",
                    "research_question": "如何结合卷积与注意力提取特征？",
                    "method": {"pipeline": "先提取局部卷积特征，再通过注意力进行加权。"},
                    "main_findings": ["原文描述了卷积和注意力的组合方法。"],
                    "evidence_spans": [{"claim": "方法结合卷积与注意力。", "page": 1, "quote": quote, "support": "direct"}],
                    "visual_evidence": [], "uncertainties": [],
                }
            if "FORCE_FAIL" in pages or len(pages) > 9000:
                raise ValueError("模型输出因达到输出上限而被截断")
            if quote in pages:
                return {"notes": [{"category": "method", "finding": "The method combines convolution and attention.", "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}]}], "visual_observations": [], "evidence_spans": [], "uncertainties": []}
            return {"notes": [], "visual_observations": [], "evidence_spans": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Partially truncated chunk fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request, app.model_settings = original

        self.assertEqual(result["analysis_status"], "partial")
        self.assertEqual(result["failed_pages"], [1])
        self.assertEqual(result["analysis"]["analysis_status"], "partial")
        self.assertTrue(result["analysis"]["partial_findings"])
        self.assertIn("影响 PDF 第 1 页", result["analysis"]["partial_reason"])
        self.assertIn("影响 PDF 第 1 页", result["analysis"]["uncertainties"][-1])
        with app.db_session() as db:
            artifact = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(json.loads(artifact["answer"])["meta"]["analysis_status"], "partial")
        self.assertEqual(json.loads(artifact["answer"])["meta"]["failed_pages"], [1])

    def test_partial_ai_job_restores_result_and_can_be_retried(self):
        paper_id, _ = app.upsert_paper({"title": "Partial job fixture"}, source="manual")
        job_id, artifact_id, now = "partial-job-fixture", "partial-artifact-fixture", app.now_iso()
        answer = json.dumps({"analysis": {"analysis_status": "partial", "partial_reason": "fixture", "partial_findings": []}, "meta": {"analysis_status": "partial"}}, ensure_ascii=False)
        with app.db_session() as db:
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "deep_read", "fixture", "fixture", "v1", "c" * 64, answer, now))
            db.execute("INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,artifact_id,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (job_id, paper_id, "deep_read", "queued", "queued", "", "{}", now, now, ""))

        original_run, original_submit = app.run_fulltext_ai_analysis, app.submit_ai_job
        app.run_fulltext_ai_analysis = lambda *_args, **_kwargs: {"analysis_status": "partial", "analysis": {"analysis_status": "partial", "partial_reason": "fixture"}, "artifact_id": artifact_id, "chunk_count": 1}
        submitted = []
        app.submit_ai_job = lambda target, kind, options: submitted.append((target, kind, options)) or {"job": {"status": "queued"}}
        try:
            app.run_ai_job(job_id)
            restored = app.get_ai_job(job_id, include_result=True)
            retried = app.retry_ai_job(job_id)
        finally:
            app.run_fulltext_ai_analysis, app.submit_ai_job = original_run, original_submit

        self.assertEqual(restored["status"], "partial")
        self.assertEqual(restored["result"]["analysis_status"], "partial")
        self.assertEqual(retried["retried_from"], job_id)
        self.assertEqual(submitted[0][:2], (paper_id, "deep_read"))
        self.assertEqual(submitted[0][2]["synthesis_only_artifact_id"], artifact_id)
        self.assertFalse(submitted[0][2]["reuse"])

    def test_evidence_gap_partial_artifact_is_not_offered_as_synthesis_retry_source(self):
        paper_id, _ = app.upsert_paper({"title": "Evidence gap partial fixture"}, source="manual")
        artifact_id, now = "evidence-gap-partial-fixture", app.now_iso()
        answer = json.dumps({"analysis": {"analysis_status": "partial", "partial_kind": "evidence_gap"}, "meta": {"analysis_status": "partial", "partial_kind": "evidence_gap", "synthesis_retry_available": False}}, ensure_ascii=False)
        with app.db_session() as db:
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "deep_read", "fixture", "fixture", "v1", "e" * 64, answer, now))
        self.assertEqual(app._partial_deep_read_artifact_id(paper_id, artifact_id), "")

    def test_failed_synthesis_only_job_retries_without_reprocessing_pdf(self):
        source_artifact = "saved-partial-for-failed-retry"
        job = {
            "id": "failed-synthesis-retry-job", "paper_id": "paper-fixture", "job_type": "deep_read",
            "status": "failed", "artifact_id": "", "options": {"template": "graduate_deep_read", "apply": False, "synthesis_only_artifact_id": source_artifact},
        }
        original_get, original_submit = app.get_ai_job, app.submit_ai_job
        calls = []
        app.get_ai_job = lambda _job_id: job
        app.submit_ai_job = lambda paper_id, job_type, data: calls.append((paper_id, job_type, data)) or {"job": {"id": "retry"}}
        try:
            result = app.retry_ai_job(job["id"])
        finally:
            app.get_ai_job, app.submit_ai_job = original_get, original_submit

        self.assertEqual(result["retried_from"], job["id"])
        self.assertEqual(calls[0][0:2], ("paper-fixture", "deep_read"))
        self.assertEqual(calls[0][2]["synthesis_only_artifact_id"], source_artifact)
        self.assertFalse(calls[0][2]["reuse"])

    def test_reading_card_repair_returns_page_evidence_for_local_validation(self):
        quote = "Our method estimates food volume from a single image."
        output = {"reading_card": {field: "" for field in app.READING_CARD_FIELDS} | {"research_question": "How can food volume be estimated?", "method": "The model predicts volume from image features.", "evidence": [{"claim": "The task is food-volume estimation.", "page": 1, "quote": quote, "support": "direct"}]}}
        original = app.model_json_request
        app.model_json_request = lambda *_args, **_kwargs: output
        evidence = []

        def model_call(_stage, _kind, request, _input_chars):
            return request()

        try:
            card = app.complete_reading_card({"title": "Fixture"}, [{"page_number": 1, "text": quote}], [], {}, model_call, evidence_out=evidence)
        finally:
            app.model_json_request = original

        self.assertTrue(card["research_question"])
        self.assertEqual(evidence, output["reading_card"]["evidence"])
        analysis = {"reading_card": card, "evidence": evidence, "uncertainties": []}
        app.validate_ai_evidence(analysis, [{"page_number": 1, "text": quote}])
        self.assertEqual(len(analysis["evidence"]), 1)
        self.assertEqual(analysis["evidence"][0]["quote"], quote)

    def test_reading_card_repair_evidence_avoids_duplicate_locator_request(self):
        paper_id, _ = app.upsert_paper({"title": "Repair evidence reuse fixture"}, source="manual")
        quote = "The method combines image features with text prompts."
        rows = [{"attachment_id": "repair-evidence-fixture", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "b" * 64}]
        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "model_json_request": app.model_json_request,
            "complete_reading_card": app.complete_reading_card,
            "extract_reading_card_evidence": app.extract_reading_card_evidence,
        }

        def partial_card(_prompt, _payload, timeout=120):
            return {"reading_card": {field: "" for field in app.READING_CARD_FIELDS} | {"method": "本文结合图像特征与文本提示。"}, "evidence": []}

        def repaired_card(_paper, _rows, _notes, _current, _model_call, evidence_out=None):
            if evidence_out is not None:
                evidence_out.append({"claim": "方法结合图像与文本", "page": 1, "quote": quote, "support": "direct"})
            return {field: "原文未说明" for field in app.READING_CARD_FIELDS} | {"method": "本文结合图像特征与文本提示。", "results": "结果需回到原文确认。"}

        def duplicate_locator(*_args, **_kwargs):
            raise AssertionError("a verified quote from card repair should avoid a second locator request")

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Repair evidence reuse fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = partial_card
        app.complete_reading_card = repaired_card
        app.extract_reading_card_evidence = duplicate_locator
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.model_json_request = original["model_json_request"]
            app.complete_reading_card = original["complete_reading_card"]
            app.extract_reading_card_evidence = original["extract_reading_card_evidence"]

        self.assertEqual(result["analysis"]["evidence"][0]["quote"], quote)
        self.assertEqual(result["model_call_count"], 1)

    def test_ai_artifact_evidence_verification_discards_fabricated_page_or_quote(self):
        analysis = {"evidence_spans": [
            {"claim": "valid", "page": 1, "quote": "The benchmark contains 10 classes."},
            {"claim": "bad page", "page": 99, "quote": "invented quote"},
        ], "uncertainties": []}
        app.validate_ai_evidence(analysis, [{"page_number": 1, "text": "Results: The benchmark contains 10 classes."}])
        self.assertEqual(len(analysis["evidence_spans"]), 1)
        self.assertIn("已移除 1 条", analysis["uncertainties"][0])

    def test_ai_analysis_history_filters_nested_bad_evidence_and_marks_legacy_results(self):
        paper_id, _ = app.upsert_paper({"title": "Historical analysis validation fixture"}, source="manual")
        valid_quote = "The method combines image features and text prompts."
        now = app.now_iso()
        partial_id, legacy_id = "history-partial-fixture", "history-legacy-fixture"
        partial_answer = {
            "analysis": {
                "analysis_status": "partial",
                "partial_reason": "fixture partial result",
                "partial_findings": [
                    {"category": "method", "finding": "Verified method finding.", "evidence_spans": [{"page": 1, "quote": valid_quote, "support": "direct"}]},
                    {"category": "unknown", "finding": "Unverified finding.", "evidence_spans": [{"page": 9, "quote": "invented quote", "support": "direct"}]},
                ],
                "evidence_spans": [{"claim": "bad anchor", "page": 1, "quote": "not present in the page", "support": "direct"}],
                "visual_evidence": [], "uncertainties": [],
            },
            "meta": {"analysis_status": "partial", "pipeline_version": "fulltext-analysis-v12", "visual_page_refs": []},
        }
        legacy_answer = {
            "analysis": {"research_question": "A legacy question", "method_summary": "An older method", "key_findings": ["Old result"]},
            "meta": {"pipeline_version": "fulltext-analysis-v7", "page_count": 1},
        }
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("history-validation-attachment", paper_id, "fixture.pdf", "fixture.pdf", "8" * 64, 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("history-validation-attachment", paper_id, 1, valid_quote, len(valid_quote), "good", "fixture", "9" * 64, now))
            for artifact_id, answer in ((partial_id, partial_answer), (legacy_id, legacy_answer)):
                db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (artifact_id, paper_id, "deep_read", "fixture", "fixture", "fixture", "a" * 64, json.dumps(answer, ensure_ascii=False), now))

        server, thread, base = self._start_api_server()
        try:
            payload = self._api_json(base, f"/api/papers/{paper_id}/ai-analysis")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        items = {item["id"]: item for item in payload["analyses"]}
        partial = items[partial_id]
        self.assertEqual(partial["analysis_status"], "partial")
        self.assertEqual(len(partial["analysis"]["partial_findings"]), 1)
        self.assertEqual(partial["analysis"]["partial_findings"][0]["evidence_spans"][0]["quote"], valid_quote)
        self.assertFalse(partial["analysis"]["evidence_spans"])
        self.assertTrue(any("已移除" in item for item in partial["analysis"]["uncertainties"]))
        self.assertEqual(items[legacy_id]["analysis_status"], "legacy_unverified")
        self.assertTrue(items[legacy_id]["analysis_warning"])
        with app.db_session() as db:
            stored = json.loads(db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (partial_id,)).fetchone()[0])
        self.assertEqual(len(stored["analysis"]["partial_findings"]), 2)

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

    def test_text_only_deep_read_never_sends_images_and_clears_visual_claims(self):
        paper_id, _ = app.upsert_paper({"title": "Text-only deep-read fixture"}, source="manual")
        quote = "The method minimizes a contrastive objective over paired samples."
        rows = [{
            "attachment_id": "text-only-attachment", "page_number": 1, "text": quote,
            "char_count": len(quote), "quality": "good", "text_sha256": "a" * 64,
        }]
        original = (
            app.get_paper_pages_for_ai,
            app.prepare_visual_pages,
            app.model_multimodal_json_request,
            app.model_json_request,
            app.model_settings,
        )
        prompts = []

        def reject_image_render(*_args, **_kwargs):
            raise AssertionError("text-only deep read must not render PDF images")

        def reject_multimodal(*_args, **_kwargs):
            raise AssertionError("text-only deep read must not call a multimodal model")

        def fake_text_model(prompt, payload, **_kwargs):
            prompts.append(prompt)
            if "文本证据抽取器" in prompt:
                return {
                    "notes": [{
                        "category": "method", "finding": "The method minimizes a contrastive objective.",
                        "evidence_spans": [{"page": 1, "quote": quote, "support": "direct"}],
                    }],
                    "visual_observations": [{"type": "figure", "page": 1, "description": "Fabricated chart trend."}],
                    "evidence_spans": [], "uncertainties": [],
                }
            self.assertIn("仅综合输入的逐页文字证据", prompt)
            self.assertFalse(payload["extracted_notes"][0].get("visual_observations"))
            return {
                "overview": "The paper optimizes a contrastive objective over paired samples.",
                "research_question": "How should the paired-sample objective be optimized?",
                "motivation": "The objective is the method's training signal.",
                "contributions": ["A contrastive training objective."],
                "problem_formulation": {"task": "Optimize a paired-sample objective."},
                "method": {"pipeline": "Minimize the contrastive objective over paired samples."},
                "equation_analysis": [], "datasets": [], "experiments": [], "result_tables": [], "metrics": [],
                "main_findings": ["The text describes a contrastive objective."], "limitations": [],
                "reproducibility": {"code": "原文未说明", "data": "原文未说明", "environment": "原文未说明", "random_seed": "原文未说明", "missing_information": []},
                "critical_reading": {"strong_points": [], "weak_points": [], "possible_confounders": [], "questions_for_author": []},
                "visual_analysis": {
                    "formulas": ["A fabricated visual formula"],
                    "figures": [{"page": 1, "description": "Fabricated chart trend."}],
                    "tables": [{"page": 1, "description": "Fabricated table result."}],
                    "diagrams": [{"page": 1, "description": "Fabricated diagram."}],
                },
                "visual_evidence": [{"type": "figure", "page": 1, "description": "Fabricated chart trend."}],
                "evidence_spans": [{"claim": "The method minimizes a contrastive objective.", "page": 1, "quote": quote, "support": "direct"}],
                "prereading_guide": {"prerequisites": [], "glossary": [], "evidence": []},
                "uncertainties": [], "reading_card": {},
            }

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Text-only deep-read fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = reject_image_render
        app.model_multimodal_json_request = reject_multimodal
        app.model_json_request = fake_text_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "deep_read", visual_enabled=False)
        finally:
            (
                app.get_paper_pages_for_ai,
                app.prepare_visual_pages,
                app.model_multimodal_json_request,
                app.model_json_request,
                app.model_settings,
            ) = original

        visual = result["analysis"]["visual_analysis"]
        self.assertTrue(any("未向模型发送页面图像" in item for item in result["analysis"]["uncertainties"]))
        self.assertTrue(all(not visual[key] for key in ("formulas", "figures", "tables", "diagrams")))
        self.assertEqual(result["analysis"]["visual_evidence"], [])
        self.assertEqual(result["visual_pages"], 0)
        self.assertFalse(result["visual_requested"])
        self.assertFalse(result["visual_enabled"])
        self.assertEqual(result["visual_strategy"], "disabled_text_only")
        self.assertTrue(result["model_calls"])
        self.assertTrue(all(call["image_count"] == 0 for call in result["model_calls"]))
        self.assertTrue(any("研究生论文文本证据抽取器" in prompt for prompt in prompts))
        self.assertFalse(any("研究生论文多模态证据抽取器" in prompt for prompt in prompts))

    def test_clear_unverified_visual_analysis_removes_claims_and_records_limit(self):
        analysis = {
            "visual_analysis": {"formulas": ["unverified"], "figures": ["unverified"], "tables": ["unverified"], "diagrams": ["unverified"]},
            "visual_evidence": [{"type": "figure", "page": 1}],
            "visual_observations": [{"type": "table", "page": 1}],
            "uncertainties": [],
        }
        app.clear_unverified_visual_analysis(analysis, "本次没有发送页面图像")
        self.assertEqual(analysis["visual_analysis"]["figures"], [])
        self.assertEqual(analysis["visual_analysis"]["verified_pages"], [])
        self.assertEqual(analysis["visual_evidence"], [])
        self.assertNotIn("visual_observations", analysis)
        self.assertIn("本次没有发送页面图像", analysis["uncertainties"])

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
                "prereading_guide": {"prerequisites": [{"concept": "视觉证据", "necessity": "recommended", "basis": "inferred", "why_needed": "理解输入来源", "minimum_to_know": "图像可作为模型输入"}], "glossary": [{"term": "visual evidence", "plain_definition": "图像中可观察的信息", "role_in_paper": "作为模型输入", "definition_basis": "background"}], "evidence": [{"term": "visual evidence", "page": 1, "quote": "The method uses visual evidence."}]},
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
        self.assertEqual(artifact["prompt_version"], "fulltext-deep-read-v17")
        self.assertEqual(len(artifact["input_sha256"]), 64)
        self.assertIn('"page_count": 1', artifact["answer"])
        self.assertIn("10% improvement", artifact["answer"])
        guide = result["analysis"]["prereading_guide"]
        self.assertEqual(guide["prerequisites"][0]["concept"], "视觉证据")
        self.assertEqual(guide["evidence"][0]["quote"], "The method uses visual evidence.")

    def test_fulltext_pages_deduplicate_same_attachment_hash(self):
        paper_id, _ = app.upsert_paper({"title": "Duplicate attachment fixture"}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            for attachment_id, name, file_hash in (("attachment-a", "downloaded.pdf", "file-hash-a"), ("attachment-b", "manual-copy.pdf", "file-hash-b")):
                db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", (attachment_id, paper_id, name, name, file_hash, 10, now))
                db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id, paper_id, 1, "The same extracted page.", 23, "good", "fixture", "page-hash", now))
        _paper, rows = app.get_paper_pages_for_ai(paper_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attachment_id"], "attachment-a")

    def test_reading_card_uses_fast_text_path_and_records_model_latency(self):
        self.assertEqual(app.READING_CARD_VISUAL_PAGES_PER_CHUNK, 0)
        paper_id, _ = app.upsert_paper({"title": "Fast reading card fixture"}, source="manual")
        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "prepare_visual_pages": app.prepare_visual_pages,
            "model_multimodal_json_request": app.model_multimodal_json_request,
            "model_json_request": app.model_json_request,
            "chunk_chars": app.READING_CARD_CHUNK_CHARS,
            "visual_budget": app.READING_CARD_VISUAL_PAGES_PER_CHUNK,
        }
        rows = []
        for page in range(1, 9):
            cue = " Figure 2 and Table 1 show the experimental results." if page in {3, 6} else ""
            text = f"Page {page} describes the method and dataset." + cue + (" evidence" * 520)
            rows.append({"attachment_id": "fixture-attachment", "page_number": page, "text": text, "char_count": len(text), "quality": "good", "text_sha256": str(page) * 64})
        selected_pages = []
        def fake_visual(page_rows, max_pages=8):
            selected_pages.extend(row["page_number"] for row in page_rows)
            images = [{"attachment_id": row["attachment_id"], "page": row["page_number"], "sha256": str(row["page_number"]) * 64, "data_url": "data:image/jpeg;base64,AA=="} for row in page_rows[:max_pages]]
            return {"images": images, "failures": [], "rendered_pages": len(images), "requested_pages": len(images)}
        def fake_multimodal(prompt, payload, images, timeout=150):
            if "证据抽取器" in prompt:
                return {"notes": [], "visual_observations": [], "evidence_spans": [], "uncertainties": []}
            return {
                "reading_card": {
                    field: ("fixture " + field) if field in {"method", "results"} else ""
                    for field in app.READING_CARD_FIELDS
                },
                "evidence": [],
                "visual_analysis": {},
                "visual_evidence": [],
                "uncertainties": [],
            }
        def fake_text_model(prompt, payload, timeout=120):
            if "原文引文定位器" in prompt:
                return {"evidence": [{"claim": "本文方法", "page": 1, "quote": "Page 1 describes the method and dataset.", "support": "direct"}]}
            if "读前辅导助手" in prompt:
                return {"prereading_guide": {"glossary": [{"term": "fixture term", "plain_definition": "测试论文中的一个术语", "role_in_paper": "用于解释本文方法", "definition_basis": "background"}]}}
            return {
                "reading_card": {
                    field: ("fixture " + field) if field in {"method", "results"} else ""
                    for field in app.READING_CARD_FIELDS
                },
                "evidence": [],
                "visual_analysis": {},
                "visual_evidence": [],
                "uncertainties": [],
            }
        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Fast reading card fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.prepare_visual_pages = fake_visual
        app.model_multimodal_json_request = fake_multimodal
        app.model_json_request = fake_text_model
        app.READING_CARD_CHUNK_CHARS = 22000
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.prepare_visual_pages = original["prepare_visual_pages"]
            app.model_multimodal_json_request = original["model_multimodal_json_request"]
            app.model_json_request = original["model_json_request"]
            app.READING_CARD_CHUNK_CHARS = original["chunk_chars"]
            app.READING_CARD_VISUAL_PAGES_PER_CHUNK = original["visual_budget"]
        self.assertEqual(result["chunk_count"], 2)
        self.assertEqual(result["model_call_count"], 3)
        self.assertEqual(selected_pages, [])
        self.assertEqual(result["visual_pages"], 0)
        self.assertEqual(result["visual_strategy"], "disabled_text_only")
        self.assertEqual(result["pipeline_version"], app.READING_CARD_PIPELINE_VERSION)
        self.assertTrue(all(call["duration_ms"] >= 0 for call in result["model_calls"]))
        self.assertEqual(result["model_call_count"], len(result["model_calls"]))
        self.assertEqual(result["evidence_strategy"], "local_exact_text")
        self.assertGreater(result["model_call_max_ms"], -1)
        self.assertTrue(all(call["image_count"] == 0 for call in result["model_calls"]))
        self.assertEqual(result["analysis"]["evidence"][0]["page"], 1)
        self.assertEqual(result["analysis"]["evidence"][0]["quote"], "Page 1 describes the method and dataset.")

    def test_single_chunk_reading_card_uses_one_text_request_and_verifies_evidence(self):
        paper_id, _ = app.upsert_paper({"title": "Single pass reading card fixture"}, source="manual")
        original = {
            "get_paper_pages_for_ai": app.get_paper_pages_for_ai,
            "prepare_visual_pages": app.prepare_visual_pages,
            "model_multimodal_json_request": app.model_multimodal_json_request,
            "model_json_request": app.model_json_request,
            "chunk_chars": app.READING_CARD_CHUNK_CHARS,
        }
        quote = "Our method improves accuracy by 10 percent."
        rows = [{"attachment_id": "single-pass-attachment", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "a" * 64}]
        calls = []

        def fake_text(prompt, payload, timeout=120):
            calls.append((prompt, payload))
            return {
                "reading_card": {field: "" for field in app.READING_CARD_FIELDS} | {"method": "视觉证据方法", "results": "准确率提升 10%。", "evidence": [{"claim": "准确率提升", "page": 1, "quote": quote, "support": "direct"}]},
                "visual_analysis": {"formulas": [], "figures": [], "tables": [], "diagrams": [], "visual_uncertainties": ["本次快速卡未进行页面视觉识别"]},
                "visual_evidence": [],
                "prereading_guide": {"prerequisites": [{"concept": "视觉特征", "necessity": "recommended", "basis": "inferred", "why_needed": "理解特征提取", "minimum_to_know": "特征概括图像信息"}], "glossary": [{"term": "visual evidence", "plain_definition": "图像中可观察的信息", "role_in_paper": "作为模型输入", "definition_basis": "background"}], "evidence": [{"term": "visual evidence", "page": 1, "quote": quote}]},
                "uncertainties": [],
            }

        def reject_multimodal_request(*_args, **_kwargs):
            raise AssertionError("快速阅读卡不应发送页面图像")

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Single pass reading card fixture", "authors": "A. Author", "year": 2026, "venue": "Fixture", "topic": ""}, rows)
        app.model_multimodal_json_request = reject_multimodal_request
        app.model_json_request = fake_text
        app.READING_CARD_CHUNK_CHARS = 100000
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai = original["get_paper_pages_for_ai"]
            app.model_multimodal_json_request = original["model_multimodal_json_request"]
            app.model_json_request = original["model_json_request"]
            app.READING_CARD_CHUNK_CHARS = original["chunk_chars"]

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["chunk_count"], 1)
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["analysis"]["reading_card"]["results"], "准确率提升 10%。")
        self.assertEqual(result["analysis"]["evidence"][0]["quote"], quote)
        self.assertEqual(result["analysis"]["visual_evidence"], [])
        self.assertEqual(result["analysis"]["prereading_guide"]["glossary"][0]["term"], "visual evidence")
        self.assertNotIn("prereading_guide", calls[0][0])
        self.assertEqual(result["analysis"]["prereading_guide"]["evidence"][0]["quote"], quote)
        self.assertEqual(result["visual_pages"], 0)
        self.assertEqual(result["model_calls"][0]["image_count"], 0)

    def test_long_model_request_persists_progress_heartbeat_until_request_finishes(self):
        """A slow inference should keep the persisted job visibly alive."""
        paper_id, _ = app.upsert_paper({"title": "Progress heartbeat fixture"}, source="manual")
        quote = "Our method combines image and text features."
        rows = [{"attachment_id": "heartbeat-attachment", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "b" * 64}]
        originals = (
            app.get_paper_pages_for_ai,
            app.model_json_request,
            app.model_settings,
            app.MODEL_PROGRESS_HEARTBEAT_SECONDS,
        )
        request_started = threading.Event()
        release_request = threading.Event()

        def delayed_model(_prompt, _payload, timeout=120, **_kwargs):
            request_started.set()
            release_request.wait(5)
            return {
                "reading_card": {field: "原文未说明" for field in app.READING_CARD_FIELDS} | {"method": "融合图像与文本特征。", "results": "实验报告了该方法的结果。"},
                "evidence": [{"claim": "方法融合图像与文本特征", "page": 1, "quote": quote, "support": "direct"}],
                "visual_analysis": {"formulas": [], "figures": [], "tables": [], "diagrams": [], "visual_uncertainties": []},
                "visual_evidence": [],
                "prereading_guide": {},
                "uncertainties": [],
            }

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Progress heartbeat fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = delayed_model
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "protocol": "chat_completions", "base_url": "https://model.example/v1", "api_url": "https://model.example/v1/chat/completions"}
        app.MODEL_PROGRESS_HEARTBEAT_SECONDS = 2.0
        job_id = "long-model-progress-heartbeat-fixture"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, paper_id, "reading_card", "queued", "queued", "queued", json.dumps({"apply": False}), now, now),
            )
        worker = threading.Thread(target=app.run_ai_job, args=(job_id,), daemon=True)
        try:
            worker.start()
            self.assertTrue(request_started.wait(2), "the model request did not start")
            deadline = time.monotonic() + 4
            running = app.get_ai_job(job_id)
            while time.monotonic() < deadline:
                running = app.get_ai_job(job_id)
                if "已等待" in (running.get("message") or ""):
                    break
                time.sleep(0.025)
            self.assertIn("已等待", running.get("message", ""))
            self.assertEqual(running["status"], "running")
            release_request.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "the AI job did not finish after the request was released")
            completed = app.get_ai_job(job_id)
            self.assertEqual(completed["status"], "success", completed.get("error") or completed)
            self.assertEqual(completed["stage"], "completed")
            self.assertEqual(completed["message"], "分析完成")
        finally:
            release_request.set()
            if worker.is_alive():
                worker.join(timeout=1)
            (
                app.get_paper_pages_for_ai,
                app.model_json_request,
                app.model_settings,
                app.MODEL_PROGRESS_HEARTBEAT_SECONDS,
            ) = originals

    def test_large_reading_card_stays_single_pass_below_default_budget(self):
        paper_id, _ = app.upsert_paper({"title": "Large single pass reading card fixture"}, source="manual")
        original_pages, original_model = app.get_paper_pages_for_ai, app.model_json_request
        original_budget = app.READING_CARD_CHUNK_CHARS
        rows = []
        for page in range(1, 4):
            quote = f"Page {page} states that the method combines image and text evidence."
            text = quote + " context" * 4200
            rows.append({"attachment_id": "large-card-attachment", "page_number": page, "text": text, "char_count": len(text), "quality": "good", "text_sha256": str(page) * 64})
        calls = []

        def fake_model(prompt, payload, timeout=120):
            calls.append((prompt, payload))
            card = {field: "原文未说明" for field in app.READING_CARD_FIELDS}
            card.update({"method": "方法结合图像与文本证据。", "results": "原文报告了方法结果。"})
            quote = "Page 1 states that the method combines image and text evidence."
            return {
                "reading_card": card,
                "evidence": [{"claim": "方法结合图像与文本证据", "page": 1, "quote": quote, "support": "direct"}],
                "visual_analysis": {"formulas": [], "figures": [], "tables": [], "diagrams": [], "visual_uncertainties": []},
                "visual_evidence": [],
                "prereading_guide": {},
                "uncertainties": [],
            }

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Large single pass reading card fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_model
        try:
            self.assertGreaterEqual(original_budget, 110000)
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai, app.model_json_request = original_pages, original_model
            app.READING_CARD_CHUNK_CHARS = original_budget

        self.assertGreater(sum(len(row["text"]) for row in rows), 100000)
        self.assertEqual(result["chunk_count"], 1)
        self.assertTrue(result["fast_first_pass"])
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["analysis"]["evidence"][0]["quote"], "Page 1 states that the method combines image and text evidence.")

    def test_missing_prereading_guide_does_not_block_reading_card(self):
        paper_id, _ = app.upsert_paper({"title": "Prereading recovery fixture"}, source="manual")
        original_pages, original_model = app.get_paper_pages_for_ai, app.model_json_request
        quote = "Cross-attention combines image and text features."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "d" * 64}]
        calls = []

        def fake_text(prompt, payload, timeout=120):
            calls.append(prompt)
            if "原文引文定位器" in prompt:
                return {"evidence": [{"claim": "跨模态融合方法", "page": 1, "quote": quote, "support": "direct"}]}
            if "读前辅导助手" in prompt:
                return {"prereading_guide": {"glossary": [{"term": "cross-attention", "plain_definition": "让两类特征互相查询和融合", "role_in_paper": "连接图像与文本信息", "definition_basis": "background"}], "self_check": [{"question": "交叉注意力在本文中连接什么？", "key_point": "图像与文本特征"}], "evidence": []}}
            return {"reading_card": {field: "" for field in app.READING_CARD_FIELDS} | {"research_question": "如何融合视觉与文本特征？", "method": "cross-attention"}, "evidence": [], "visual_analysis": {}, "visual_evidence": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Prereading recovery fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_text
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai = original_pages
            app.model_json_request = original_model

        self.assertEqual(len(calls), 1)
        self.assertNotIn("读前辅导助手", calls[0])
        self.assertNotIn("prereading_guide", calls[0])
        self.assertNotIn("原文引文定位器", calls[0])
        self.assertEqual(result["model_call_count"], 1)
        self.assertEqual(result["evidence_strategy"], "local_exact_text")
        guide = result["analysis"]["prereading_guide"]
        self.assertEqual(guide["glossary"], [])
        self.assertEqual(result["analysis"]["evidence"][0]["quote"], quote)
        with app.db_session() as db:
            saved = db.execute("SELECT answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertIn('"method": "cross-attention"', saved["answer"])

    def test_prereading_completion_accepts_flat_model_json_without_envelope(self):
        calls = []
        output_limits = []
        quote = "Cross-attention combines image and text features."
        flat_result = {
            "prerequisites": [{"concept": "attention mechanism", "necessity": "recommended", "basis": "inferred", "why_needed": "理解跨模态特征交互", "minimum_to_know": "注意力根据相似度聚合信息"}],
            "glossary": [{"term": "cross-attention", "expansion": "交叉注意力", "plain_definition": "用一类特征查询另一类特征", "role_in_paper": "融合图像和文本", "definition_basis": "background"}],
            "evidence": [{"term": "cross-attention", "page": 1, "quote": quote}],
        }

        original_model = app.model_json_request

        prompts = []

        def fake_model_request(_prompt, _payload, timeout=120, max_output_tokens=None):
            prompts.append(_prompt)
            output_limits.append(max_output_tokens)
            return flat_result

        def flat_model_call(stage, _kind, request, _input_chars):
            calls.append(stage)
            return request()

        app.model_json_request = fake_model_request
        try:
            guide = app.complete_prereading_guide(
                {"title": "Flat prereading fixture"},
                [{"page_number": 1, "text": quote}],
                [],
                flat_model_call,
            )
        finally:
            app.model_json_request = original_model
        self.assertEqual(calls, ["prereading_guide"])
        self.assertEqual(output_limits, [app.MODEL_SYNTHESIS_MAX_OUTPUT_TOKENS])
        self.assertIn("零基础研究生", prompts[0])
        self.assertIn("具体例子", prompts[0])
        self.assertIn("背景释义与本文原文定义必须明确区分", prompts[0])
        self.assertEqual(guide["glossary"][0]["term"], "cross-attention")
        self.assertEqual(guide["evidence"][0]["quote"], quote)

    def test_english_prereading_guide_gets_chinese_language_repair(self):
        original_model = app.model_json_request
        stages = []
        english = {
            "prerequisites": [{"concept": "Transformer architecture", "necessity": "recommended", "basis": "inferred", "why_needed": "It explains how the model processes inputs.", "minimum_to_know": "Self-attention weighs information from other tokens."}],
            "glossary": [{"term": "cross-attention", "expansion": "", "plain_definition": "One representation queries another representation.", "role_in_paper": "It fuses image and text features.", "definition_basis": "background"}],
            "notation": [], "self_check": [], "uncertainties": [],
        }
        chinese = {
            "prerequisites": [{"concept": "Transformer 架构", "necessity": "recommended", "basis": "inferred", "why_needed": "它说明模型如何处理输入信息。", "minimum_to_know": "自注意力会按相关性汇集其他词元的信息。"}],
            "glossary": [{"term": "cross-attention", "expansion": "交叉注意力", "plain_definition": "一种特征表示可以查询另一种特征表示。", "role_in_paper": "它用于融合图像与文本特征。", "definition_basis": "background"}],
            "notation": [], "self_check": [], "uncertainties": [],
        }

        def fake_model(prompt, payload, timeout=120, max_output_tokens=None):
            return chinese if "guide" in payload else english

        def model_call(stage, _kind, request, _input_chars):
            stages.append(stage)
            return request()

        app.model_json_request = fake_model
        try:
            guide = app.complete_prereading_guide(
                {"title": "Language repair fixture"},
                [{"page_number": 2, "text": "Cross-attention combines image and text features."}],
                [], model_call,
            )
        finally:
            app.model_json_request = original_model

        self.assertEqual(stages, ["prereading_guide", "prereading_guide_localization"])
        self.assertFalse(app.prereading_guide_needs_chinese(guide))
        self.assertEqual(guide["glossary"][0]["term"], "cross-attention")
        self.assertEqual(guide["glossary"][0]["definition_basis"], "background")
        self.assertEqual(guide["evidence"][0]["page"], 2)

    def test_standalone_prereading_guide_accepts_aliases_and_persists_study_items(self):
        paper_id, _ = app.upsert_paper({"title": "Standalone prereading fixture", "year": 2026}, source="manual")
        original_pages, original_model = app.get_paper_pages_for_ai, app.model_json_request
        quote = "Cross-attention combines image and text features."
        rows = [{"attachment_id": "fixture-pdf", "page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "e" * 64}]
        app.get_paper_pages_for_ai = lambda _paper_id: ({"id": paper_id, "title": "Standalone prereading fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = lambda *_args, **_kwargs: {
            "prerequisite_knowledge": [{"concept": "attention mechanism", "necessity": "recommended", "basis": "inferred", "why_needed": "理解两种特征如何交互", "minimum_to_know": "用相似度给信息分配权重"}],
            "technical_terms": [{"term": "cross-attention", "expansion": "交叉注意力", "plain_definition": "一类特征查询另一类特征", "role_in_paper": "融合图像和文本表示", "definition_basis": "background"}],
            "symbols": ["alpha_i"],
            "self_test": [{"question": "交叉注意力连接什么？", "key_point": "图像与文本特征"}],
            "evidence": [{"term": "cross-attention", "page": 1, "quote": quote}],
        }
        try:
            result = app.run_prereading_guide_analysis(paper_id)
        finally:
            app.get_paper_pages_for_ai, app.model_json_request = original_pages, original_model

        self.assertEqual(result["saved_items"], 3)
        self.assertEqual(result["analysis"]["prereading_guide"]["glossary"][0]["term"], "cross-attention")
        self.assertEqual(result["analysis"]["prereading_guide"]["notation"][0]["symbol"], "alpha_i")
        self.assertEqual(app.study_items_page(query="cross-attention")["total"], 1)
        with app.db_session() as db:
            artifact = db.execute("SELECT artifact_type,answer FROM ai_artifacts WHERE id=?", (result["artifact_id"],)).fetchone()
        self.assertEqual(artifact["artifact_type"], "prereading_guide")
        self.assertIn('"term": "cross-attention"', artifact["answer"])

    def test_standalone_prereading_guide_has_an_async_consent_gated_route(self):
        paper_id, _ = app.upsert_paper({"title": "Prereading route fixture"}, source="manual")
        original_settings, original_key, original_submit = app.model_settings, app.model_api_key, app.submit_ai_job
        original_has_source = app.paper_has_fulltext_source
        original_runtime_key = app.RUNTIME_MODEL_KEY
        submitted = []
        app.model_settings = lambda: {"enabled": True, "model": "fixture", "base_url": "https://model.example/v1"}
        app.model_api_key = lambda: "fixture-key"
        app.RUNTIME_MODEL_KEY = "fixture-key"
        app.paper_has_fulltext_source = lambda _paper_id: False
        app.submit_ai_job = lambda target, kind, data: submitted.append((target, kind, data)) or {"job": {"id": "fixture-job", "paper_id": target, "job_type": kind, "status": "queued"}}
        server, thread, base = self._start_api_server()
        try:
            try:
                self._api_json(base, f"/api/papers/{paper_id}/ai-prereading-guide", "POST", {"background": True, "consent": False})
                self.fail("missing consent must be rejected")
            except HTTPError as exc:
                self.assertEqual(exc.code, 400)
                missing_consent = json.loads(exc.read().decode("utf-8"))
            self.assertIn("确认", missing_consent["error"])
            try:
                self._api_json(base, f"/api/papers/{paper_id}/ai-prereading-guide", "POST", {"background": True, "consent": True})
                self.fail("a full-text job without a local PDF must be rejected before queueing")
            except HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertIn("本地 PDF", json.loads(exc.read().decode("utf-8"))["error"])
            self.assertEqual(submitted, [])
            app.paper_has_fulltext_source = lambda _paper_id: True
            accepted = self._api_json(base, f"/api/papers/{paper_id}/ai-prereading-guide", "POST", {"background": True, "consent": True}, expected_status=202)
            self.assertEqual(accepted["job"]["job_type"], "prereading_guide")
            self.assertEqual(submitted[0][1], "prereading_guide")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app.model_settings, app.model_api_key, app.submit_ai_job = original_settings, original_key, original_submit
            app.paper_has_fulltext_source = original_has_source
            app.RUNTIME_MODEL_KEY = original_runtime_key

    def test_prereading_items_form_a_searchable_persistent_study_notebook(self):
        paper_id, _ = app.upsert_paper({"title": "Cross-attention preparation fixture", "year": 2026}, source="manual")
        quote = "Cross-attention combines image and text features."
        guide = {
            "prerequisites": [{"concept": "attention mechanism", "necessity": "recommended", "basis": "inferred", "why_needed": "帮助理解特征交互", "minimum_to_know": "查询和键值之间计算权重"}],
            "glossary": [{"term": "cross-attention", "expansion": "交叉注意力", "plain_definition": "一类输入向另一类特征发起查询的注意力机制", "role_in_paper": "融合图像与文本", "definition_basis": "background"}],
            "notation": [{"symbol": "alpha_i", "meaning": "第 i 个区域的注意力权重", "caveat": "以论文符号表为准"}],
            "evidence": [{"term": "cross-attention", "page": 2, "quote": quote}, {"term": "alpha_i", "page": 3, "quote": "alpha_i denotes the attention weight."}],
        }
        with app.db_session() as db:
            self.assertEqual(app.sync_prereading_study_items(db, paper_id, guide, "artifact-1", "2026-10-14T00:00:00+00:00"), 3)
        items = app.study_items_page()
        self.assertEqual(items["total"], 3)
        self.assertEqual(items["counts"]["new"], 3)
        self.assertEqual(items["readiness"]["total_items"], 3)
        self.assertEqual(items["readiness"]["percent"], 0)
        term = next(item for item in items["items"] if item["label"] == "cross-attention")
        self.assertEqual(term["page_number"], 2)
        self.assertEqual(term["quote"], quote)
        self.assertEqual(term["basis"], "background")

        app.update_study_item(term["id"], {"mastery": "mastered", "user_note": "两路特征互相查询"})
        self.assertEqual(app.study_items_page()["readiness"]["percent"], 33)
        revised = {**guide, "glossary": [{**guide["glossary"][0], "plain_definition": "改进后的释义"}]}
        with app.db_session() as db:
            self.assertEqual(app.sync_prereading_study_items(db, paper_id, revised, "artifact-2"), 3)
        refreshed = next(item for item in app.study_items_page(query="cross-attention")["items"] if item["label"] == "cross-attention")
        self.assertEqual(refreshed["explanation"], "改进后的释义")
        self.assertEqual(refreshed["mastery"], "mastered")
        self.assertEqual(refreshed["user_note"], "两路特征互相查询")

        bilingual = app.attach_local_prereading_evidence(
            {"prerequisites": [{"concept": "视觉-语言模型（VLM）"}], "glossary": [], "notation": []},
            [{"page_number": 4, "text": "ORIGINAL_START_MARKER" + "-" * 120 + "The vision-language model combines visual and text features."}],
        )
        self.assertEqual(bilingual["evidence"][0]["page"], 4)
        self.assertIn("vision-language model", bilingual["evidence"][0]["quote"])
        self.assertNotIn("ORIGINAL_START_MARKER", bilingual["evidence"][0]["quote"])

        server, thread, base = self._start_api_server()
        try:
            listed = self._api_json(base, "/api/study-items?type=glossary&mastery=mastered")
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["items"][0]["paper_title"], "Cross-attention preparation fixture")
            updated = self._api_json(base, f"/api/study-items/{term['id']}", "PATCH", {"mastery": "learning"})
            self.assertEqual(updated["mastery"], "learning")
            deleted = self._api_json(base, f"/api/study-items/{term['id']}", "DELETE", {})
            self.assertTrue(deleted["deleted"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_prereading_spaced_review_transitions_and_due_queue(self):
        state = {"mastery": "new", "review_interval_days": 0, "review_count": 0, "review_lapses": 0, "review_ease": 2.5}
        first = app.study_review_transition(state, "good", "2026-10-20T00:00:00+00:00")
        self.assertEqual(first["review_due_at"], "2026-10-21T00:00:00+00:00")
        self.assertEqual(first["review_count"], 1)
        self.assertEqual(first["mastery"], "learning")
        second = app.study_review_transition(first | {"mastery": "learning"}, "good", "2026-10-21T00:00:00+00:00")
        self.assertEqual(second["review_due_at"], "2026-10-24T00:00:00+00:00")
        self.assertEqual(second["mastery"], "mastered")
        failed = app.study_review_transition(second, "again", "2026-10-24T00:00:00+00:00")
        self.assertEqual(failed["review_interval_days"], 1)
        self.assertEqual(failed["review_count"], 0)
        self.assertEqual(failed["review_lapses"], 1)
        self.assertEqual(failed["mastery"], "learning")
        self.assertEqual(app.study_review_transition(state, "easy", "2026-10-20T00:00:00+00:00")["review_interval_days"], 3)
        self.assertEqual(app.study_review_transition(state | {"review_interval_days": 10}, "hard", "2026-10-20T00:00:00+00:00")["review_interval_days"], 12)

        paper_id, _ = app.upsert_paper({"title": "Spaced review queue fixture"}, source="manual")
        with app.db_session() as db:
            app.sync_prereading_study_items(db, paper_id, {"glossary": [{"term": "retrieval practice", "plain_definition": "主动回忆并检索所学内容"}]}, "artifact-review")
        item = app.study_items_page()["items"][0]
        self.assertTrue(item["review_due"])
        self.assertEqual(app.study_items_page(due_only=True)["total"], 1)
        server, thread, base = self._start_api_server()
        try:
            updated = self._api_json(base, f"/api/study-items/{item['id']}", "PATCH", {"review_rating": "good"})
            self.assertEqual(updated["review_interval_days"], 1)
            self.assertEqual(app.study_items_page(due_only=True)["total"], 0)
            filtered = self._api_json(base, "/api/study-items?due=1")
            self.assertEqual(filtered["total"], 0)
            self.assertEqual(filtered["review"]["due_count"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_schema_17_migration_adds_review_schedule_without_resetting_learner_data(self):
        paper_id, _ = app.upsert_paper({"title": "Study migration fixture"}, source="manual")
        with app.db_session() as db:
            app.sync_prereading_study_items(db, paper_id, {"glossary": [{"term": "contrastive objective", "plain_definition": "A paired-sample training objective."}]}, "artifact-migration")
        item = app.study_items_page()["items"][0]
        app.update_study_item(item["id"], {"mastery": "learning", "user_note": "需要回看正负样本构造"})
        with app.db_session() as db:
            db.execute("DROP INDEX IF EXISTS study_items_review_due_idx")
            for column in ("last_reviewed_at", "review_ease", "review_lapses", "review_count", "review_interval_days", "review_due_at"):
                db.execute(f"ALTER TABLE study_items DROP COLUMN {column}")
            db.execute("UPDATE app_meta SET value='17' WHERE key='schema_version'")

        app.init_db()
        migrated = app.study_items_page(query="contrastive objective")["items"][0]
        self.assertEqual(migrated["mastery"], "learning")
        self.assertEqual(migrated["user_note"], "需要回看正负样本构造")
        self.assertEqual(migrated["review_interval_days"], 0)
        self.assertEqual(migrated["review_count"], 0)
        self.assertEqual(migrated["review_ease"], 2.5)
        self.assertEqual(app.health_summary()["database"]["schema_version"], str(app.DB_SCHEMA_VERSION))

    def test_prereading_notebook_learning_state_roundtrips_through_json_migration(self):
        paper_id, _ = app.upsert_paper({"title": "Study notebook migration fixture", "year": 2025}, source="manual")
        guide = {"glossary": [{"term": "contrastive learning", "plain_definition": "通过正负样本学习表示", "role_in_paper": "训练目标", "definition_basis": "background"}]}
        with app.db_session() as db:
            app.sync_prereading_study_items(db, paper_id, guide, "artifact-fixture")
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("artifact-fixture", paper_id, "reading_card_draft", "local", "fixture", "v1", "b" * 64, json.dumps({"analysis": {"prereading_guide": guide}}), app.now_iso()))
        item = app.study_items_page()["items"][0]
        app.update_study_item(item["id"], {"mastery": "mastered", "user_note": "拉近正样本并区分负样本"})
        reviewed = app.update_study_item(item["id"], {"review_rating": "easy"})
        self.assertEqual(reviewed["review_count"], 1)
        server, thread, base = self._start_api_server()
        try:
            with urlopen(base + "/api/export/json", timeout=10) as response:
                exported = json.loads(response.read().decode("utf-8"))
            self.assertEqual(exported["study_items"][0]["mastery"], "mastered")
            self.assertEqual(exported["study_items"][0]["review_count"], 1)
            self.assertTrue(exported["study_items"][0]["review_due_at"])

            restored_dir = Path(self.temp.name) / "restored"
            app.DATA_DIR = restored_dir
            app.FILES_DIR = restored_dir / "files"
            app.BACKUP_DIR = restored_dir / "backups"
            app.DB_PATH = restored_dir / "literature.sqlite3"
            app.init_db()
            imported = self._api_json(base, "/api/import", "POST", {"format": "json", "content": json.dumps(exported, ensure_ascii=False)})
            self.assertEqual(imported["study_items"], 1)
            restored = app.study_items_page(query="contrastive learning")["items"]
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["mastery"], "mastered")
            self.assertEqual(restored[0]["user_note"], "拉近正样本并区分负样本")
            self.assertEqual(restored[0]["source_artifact_id"], "artifact-fixture")
            self.assertEqual(restored[0]["review_count"], 1)
            self.assertEqual(restored[0]["review_due_at"], reviewed["review_due_at"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_thin_reading_card_repairs_before_fallback_evidence_lookup(self):
        paper_id, _ = app.upsert_paper({"title": "Thin card recovery fixture"}, source="manual")
        original_pages, original_model = app.get_paper_pages_for_ai, app.model_json_request
        quote = "Our method uses a calibrated visual encoder."
        rows = [{"page_number": 1, "text": quote, "char_count": len(quote), "quality": "good", "text_sha256": "e" * 64}]
        calls = []
        output_limits = []
        repair_order = []

        def fake_text(prompt, payload, timeout=120, max_output_tokens=None):
            calls.append(prompt)
            output_limits.append(max_output_tokens)
            if "阅读卡修复器" in prompt:
                repair_order.append("card")
                return {"reading_card": {"research_question": "如何校准视觉编码器？", "method": "使用校准视觉编码器"}}
            if "原文引文定位器" in prompt:
                repair_order.append("evidence")
                return {"evidence": [{"claim": "方法使用视觉编码器", "page": 1, "quote": quote, "support": "direct"}]}
            return {"reading_card": {field: "" for field in app.READING_CARD_FIELDS} | {"method": "单字段结果"}, "prereading_guide": {"glossary": [{"term": "视觉编码器", "plain_definition": "把图像转成特征的模型", "role_in_paper": "输入表示", "definition_basis": "background"}]}, "evidence": [], "visual_analysis": {}, "visual_evidence": [], "uncertainties": []}

        app.get_paper_pages_for_ai = lambda _paper_id: ({"title": "Thin card recovery fixture", "authors": "", "year": 2026, "venue": "", "topic": ""}, rows)
        app.model_json_request = fake_text
        try:
            result = app.run_fulltext_ai_analysis(paper_id, "reading_card")
        finally:
            app.get_paper_pages_for_ai = original_pages
            app.model_json_request = original_model
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair_order, ["card"])
        limit_by_prompt = dict(zip(calls, output_limits))
        self.assertEqual(next(limit for prompt, limit in limit_by_prompt.items() if "阅读卡分析器" in prompt), app.READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS)
        self.assertEqual(next(limit for prompt, limit in limit_by_prompt.items() if "阅读卡修复器" in prompt), app.READING_CARD_SYNTHESIS_MAX_OUTPUT_TOKENS)
        self.assertNotIn("原文引文定位器", " ".join(calls))
        self.assertEqual(result["evidence_strategy"], "local_exact_text")
        self.assertEqual(result["analysis"]["reading_card"]["research_question"], "如何校准视觉编码器？")
        self.assertEqual(result["analysis"]["evidence"][0]["page"], 1)
        self.assertEqual(result["analysis"]["evidence"][0]["quote"], quote)

    def test_page_chunk_builder_allows_reading_card_budget_above_24k(self):
        text = "A" * 50000
        chunks = app.build_page_chunks([{"attachment_id": "large-paper", "page_number": 1, "text": text}], max_chars=100000)
        self.assertEqual(len(chunks), 1)
        self.assertIn(text, chunks[0]["text"])

    def test_reading_card_context_keeps_signal_pages_with_bounded_input(self):
        rows = [
            {"attachment_id": "fixture", "page_number": 1, "text": "Abstract and introduction explain the research question." + (" context" * 500), "char_count": 4000},
            {"attachment_id": "fixture", "page_number": 2, "text": "Method and algorithm describe the proposed approach." + (" method" * 500), "char_count": 4000},
            {"attachment_id": "fixture", "page_number": 3, "text": "References bibliography" + (" citation" * 500), "char_count": 4000},
            {"attachment_id": "fixture", "page_number": 4, "text": "Experiment results and ablation evaluation." + (" result" * 500), "char_count": 4000},
            {"attachment_id": "fixture", "page_number": 5, "text": "Unrelated appendix material." + (" appendix" * 500), "char_count": 4000},
        ]
        selected = app.select_reading_card_context(rows, max_chars=18000)
        pages = {row["page_number"] for row in selected}
        self.assertIn(1, pages)
        self.assertIn(2, pages)
        self.assertIn(4, pages)
        self.assertNotIn(3, pages)
        self.assertLessEqual(sum(len(row["text"]) for row in selected), 18000 + 200)

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
            original_has_source = app.paper_has_fulltext_source
            app.paper_has_fulltext_source = lambda _paper_id: True
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
                app.paper_has_fulltext_source = original_has_source
            self.assertEqual(status, 200)
            self.assertFalse(draft_only["auto_applied"])
            self.assertIsNone(draft_only["applied"])
            source = (Path(__file__).resolve().parents[1] / "static" / "js" / "actions.js").read_text(encoding="utf-8")
            self.assertIn("consent:true,apply:true,merge_empty:true", source)
        finally:
            app.RUNTIME_MODEL_KEY = ""
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_schema_migrates_from_v5_to_v14_and_adds_durable_search_memory(self):
        legacy_paper_id, _ = app.upsert_paper({"title": "Legacy prereading backfill fixture"}, source="manual")
        with app.db_session() as db:
            intent = app.discovery_intent_key("migration memory fixture", {})
            now = app.now_iso()
            record = {"title": "Migrated historical candidate", "abstract": "legacy search memory", "year": 2025, "doi": "10.1000/migrated-memory", "intent_key": intent}
            db.execute("INSERT INTO runs(id,query_name,status,started_at,completed_at,sources_json) VALUES(?,?,?,?,?,?)", ("migration-memory-run", "migration memory fixture", "success", now, now, '["arxiv"]'))
            db.execute("INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)", ("migration-memory-run", "migration memory fixture", "{}", json.dumps({"intent_key": intent}), now))
            db.execute("INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)", ("candidate:migration-memory", "migration-memory-run", app.candidate_identity(record), json.dumps(record), now))
            legacy_answer = {"analysis": {"prereading_guide": {"glossary": [{"term": "legacy terminology", "plain_definition": "A migrated explanation", "definition_basis": "background"}]}}}
            db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("legacy-prereading-artifact", legacy_paper_id, "reading_card_draft", "", "fixture", "v1", "a" * 64, json.dumps(legacy_answer), now))
            db.execute("DROP TABLE study_items")
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
            triage_columns = [row[1] for row in db.execute("PRAGMA table_info(discovery_triage_jobs)").fetchall()]
            feedback_columns = [row[1] for row in db.execute("PRAGMA table_info(discovery_candidate_feedback)").fetchall()]
            memory_columns = [row[1] for row in db.execute("PRAGMA table_info(discovery_search_memory)").fetchall()]
            metadata_job_columns = [row[1] for row in db.execute("PRAGMA table_info(metadata_draft_jobs)").fetchall()]
            migrated_memory = db.execute("SELECT 1 FROM discovery_search_memory WHERE intent_key=? AND identity_key=?", (intent, "doi:10.1000/migrated-memory")).fetchone()
            study_columns = [row[1] for row in db.execute("PRAGMA table_info(study_items)").fetchall()]
            backfilled_study_item = db.execute("SELECT label,basis FROM study_items WHERE paper_id=?", (legacy_paper_id,)).fetchone()
        self.assertEqual(version, str(app.DB_SCHEMA_VERSION))
        self.assertIn("text_sha256", columns)
        self.assertIn("search_options_json", query_columns)
        self.assertIn("source_details_json", run_columns)
        self.assertIn("paper_id", card_columns)
        self.assertIn("content_json", history_columns)
        self.assertIn("pdf_text", search_columns)
        self.assertIn("processed_count", triage_columns)
        self.assertIn("candidate_key", feedback_columns)
        self.assertIn("identity_key", memory_columns)
        self.assertIn("input_json", metadata_job_columns)
        self.assertIn("result_json", metadata_job_columns)
        self.assertIn("necessity", study_columns)
        self.assertTrue(migrated_memory)
        self.assertEqual(tuple(backfilled_study_item), ("legacy terminology", "background"))

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

    def test_search_query_plan_describes_tavily_as_verified_discovery(self):
        plan = app.search_query_plan("multimodal food volume estimation", ["tavily"])
        self.assertEqual(plan["sources"][0]["id"], "tavily")
        self.assertIn("联网发现", plan["sources"][0]["role"])
        self.assertIn("核验", plan["sources"][0]["role"])

    def test_search_query_plan_can_disable_concept_expansion(self):
        plan = app.search_query_plan("multimodal reasoning", ["arxiv"], {"expand_concepts": False})
        self.assertEqual(len(plan["query_variants"]), 1)
        self.assertFalse(plan["filters"]["expand_concepts"])

    def test_chinese_query_plan_bridges_to_english_index_terms(self):
        plan = app.search_query_plan("多模态推理", ["arxiv"])
        queries = [item["query"] for item in plan["query_variants"]]
        self.assertIn("multimodal reasoning", queries)
        self.assertTrue(any("multimodal inference" in item for item in queries))

    def test_natural_language_food_volume_query_builds_compact_semantic_lanes(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        concepts, expanded_terms = app.search_plan_terms(query)
        self.assertIn("食物体积", concepts)
        self.assertIn("深度学习", concepts)
        self.assertIn("卷积", concepts)
        self.assertIn("注意力", concepts)
        self.assertFalse(any(fragment in concepts for fragment in ("习模型", "的估", "模型卷")))
        self.assertIn("food volume estimation", expanded_terms)
        self.assertIn("deep learning", expanded_terms)
        plan = app.search_query_plan(query, ["openalex", "crossref"])
        self.assertIn("对象", plan["facets"])
        self.assertIn("任务", plan["facets"])
        self.assertIn("方法", plan["facets"])
        variants = plan["query_variants"]
        compact = [item["query"].casefold() for item in variants if item.get("lane") == "semantic"]
        self.assertTrue(any("food volume estimation" in item and "deep learning" in item for item in compact))
        self.assertTrue(any("portion size" in item or "food portion" in item for item in compact))
        self.assertFalse(any("deep learning 模型" in item for item in compact))

    def test_facet_relevance_requires_object_and_task_not_just_method(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        relevant = app.score_relevance({
            "title": "Food Volume Estimation from Images Using Deep Learning",
            "abstract": "We estimate food volume and portion size from meal images.",
            "doi": "10.1000/food-volume",
        }, query, [], [])
        method_only = app.score_relevance({
            "title": "YOLO Attention Convolution for Garbage Detection",
            "abstract": "An attention-based convolutional model for object detection.",
            "doi": "10.1000/garbage-detection",
        }, query, [], [])
        object_only = app.score_relevance({
            "title": "Food Image Classification with Convolutional Neural Networks",
            "abstract": "We classify meals into food categories.",
            "doi": "10.1000/food-classification",
        }, query, [], [])
        self.assertGreater(relevant["score"], method_only["score"])
        self.assertGreater(relevant["score"], object_only["score"])
        self.assertLessEqual(method_only["score"], 32)
        self.assertLessEqual(object_only["score"], 32)

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

    def test_ad_hoc_search_defaults_to_incremental_topic_results(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = {"count": 0}

        def fake_search(_query, _limit, offset=0):
            if offset:
                return []
            calls["count"] += 1
            records = [{"title": "Previously seen multimodal paper", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/previous", "source": "arxiv"}]
            if calls["count"] > 1:
                records.append({"title": "New multimodal reasoning paper", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/new", "source": "arxiv"})
            return records

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            options = {"expand_concepts": False, "ai_expand": False, "ai_screen": False}
            app.run_query("multimodal reasoning", ["arxiv"], 10, options=options)
            second = app.run_query("multimodal reasoning", ["arxiv"], 10, options=options)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual([paper["doi"] for paper in second["papers"]], ["10.1000/new", "10.1000/previous"])
        self.assertEqual(second["summary"]["repeat_fill_count"], 1)
        self.assertTrue(second["papers"][0]["seen_before"] is False)
        self.assertTrue(second["papers"][1]["seen_before"] is True)

    def test_repeat_history_refills_an_extra_page_before_filtering(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []
        first_page = [{
            "title": f"Repeated page paper {index}", "abstract": "multimodal reasoning", "year": 2026,
            "doi": f"10.1000/repeated-page-{index}", "source": "arxiv",
        } for index in range(3)]
        second_page = [{
            "title": f"Fresh refill paper {index}", "abstract": "multimodal reasoning", "year": 2026,
            "doi": f"10.1000/fresh-refill-{index}", "source": "arxiv",
        } for index in range(3)]

        def fake_search(_query, _limit, offset=0):
            calls.append(offset)
            return [dict(item) for item in (first_page if offset == 0 else second_page if offset == 3 else [])]

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            options = {"expand_concepts": False, "ai_expand": False, "ai_screen": False}
            first = app.run_query("repeat refill fixture", ["arxiv"], 3, options=options)
            second = app.run_query("repeat refill fixture", ["arxiv"], 3, options=options)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(first["papers"]), 3)
        self.assertEqual([paper["doi"] for paper in second["papers"]], [
            "10.1000/fresh-refill-0", "10.1000/fresh-refill-1", "10.1000/fresh-refill-2",
        ])
        self.assertIn(3, calls)
        self.assertTrue(second["plan"]["repeat_refill"]["enabled"])

    def test_repeat_connector_search_uses_history_when_unique_page_is_short(self):
        """Duplicate/repeat preference must not make a connector page short."""
        original = app.SOURCE_SEARCH["arxiv"]
        calls = {"run": 0}
        first_page = [
            {"title": f"History refill connector paper {index}", "abstract": "multimodal reasoning", "year": 2026,
             "doi": f"10.1000/history-connector-{index}", "source": "arxiv",
             "url": f"https://example.test/history-connector-{index}"}
            for index in range(3)
        ]

        def fake_search(_query, _limit, offset=0):
            if offset == 0:
                calls["run"] += 1
                return first_page if calls["run"] == 1 else [dict(first_page[0])]
            return []

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            options = {"expand_concepts": False, "ai_expand": False, "ai_screen": False}
            first = app.run_query("connector history refill", ["arxiv"], 3, options=options)
            second = app.run_query("connector history refill", ["arxiv"], 3, options=options)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(first["papers"]), 3)
        self.assertEqual(len(second["papers"]), 3)
        self.assertTrue(second["repeat_fallback"])
        self.assertEqual(second["shortfall_count"], 0)

    def test_repeat_history_can_cross_four_continuation_pages_to_fill_requested_count(self):
        """Several repeat-heavy pages must not leave the review queue short."""
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []
        round_number = {"value": 0}
        pages = {
            0: [{"title": "Historical page A", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/history-a"}],
            2: [{"title": "Historical page B", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/history-b"}],
            4: [{"title": "Historical page C", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/history-c"}],
            6: [{"title": "Fresh page D", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/fresh-d"}],
        }

        def fake_search(_query, _limit, offset=0):
            calls.append(offset)
            if offset == 0:
                round_number["value"] += 1
            if round_number["value"] == 1:
                return pages[0] if offset == 0 else []
            return pages.get(offset, [])

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            app.run_query("deep repeat refill fixture", ["arxiv"], 2, options={"expand_concepts": False, "ai_expand": False, "ai_screen": False})
            second = app.run_query("deep repeat refill fixture", ["arxiv"], 2, options={"expand_concepts": False, "ai_expand": False, "ai_screen": False})
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(len(second["papers"]), 2)
        self.assertIn("10.1000/fresh-d", {paper["doi"] for paper in second["papers"]})
        self.assertIn(6, calls)

    def test_history_refill_counts_only_candidates_that_pass_filters(self):
        """Raw filtered rows must not suppress an eligible history refill."""
        query = "filtered repeat refill fixture"
        options = {"year_from": 2025, "expand_concepts": False, "ai_expand": False, "ai_screen": False}
        intent = app.discovery_intent_key(query, options)
        prior_run = "filtered-history-refill-run"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?)",
                (prior_run, query, "success", now, now),
            )
            for index in range(2):
                record = {
                    "id": f"candidate:filtered-history-{index}",
                    "title": f"Eligible historical paper {index}",
                    "abstract": "filtered repeat refill fixture",
                    "year": 2026,
                    "doi": f"10.4242/filtered-history-{index}",
                    "url": f"https://doi.org/10.4242/filtered-history-{index}",
                    "source": "arxiv",
                    "intent_key": intent,
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], prior_run, app.candidate_identity(record), json.dumps(record, ensure_ascii=False), now),
                )
                app.record_discovery_search_memory(db, intent, record, prior_run, now)

        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [
            {"title": f"Filtered raw paper {index}", "abstract": "filtered repeat refill fixture", "year": 2018,
             "doi": f"10.4242/filtered-raw-{index}", "source": "arxiv"}
            for index in range(2)
        ]
        try:
            result = app.run_query(query, ["arxiv"], 2, options=options)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(result["shortfall_count"], 0)
        self.assertEqual({item["title"] for item in result["papers"]}, {"Eligible historical paper 0", "Eligible historical paper 1"})

    def test_full_duplicate_connector_page_fetches_next_page_to_keep_requested_count(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []
        first_page = [
            {"title": "Same indexed work", "abstract": "multimodal reasoning", "year": 2026,
             "doi": "10.1000/same-indexed", "source": "arxiv"},
            {"title": "Same indexed work", "abstract": "multimodal reasoning", "year": 2026,
             "doi": "10.1000/same-indexed", "source": "arxiv"},
        ]
        second_page = [
            {"title": "Fresh page work one", "abstract": "multimodal reasoning", "year": 2026,
             "doi": "10.1000/fresh-one", "source": "arxiv"},
            {"title": "Fresh page work two", "abstract": "multimodal reasoning", "year": 2026,
             "doi": "10.1000/fresh-two", "source": "arxiv"},
        ]

        def fake_search(_query, _limit, offset=0):
            calls.append(offset)
            return first_page if offset == 0 else second_page if offset == 3 else []

        app.SOURCE_SEARCH["arxiv"] = fake_search
        try:
            result = app.run_query(
                "duplicate page fixture", ["arxiv"], 3,
                options={"expand_concepts": False, "ai_expand": False, "ai_screen": False},
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(result["papers"]), 3)
        self.assertIn(3, calls)
        self.assertEqual({paper["doi"] for paper in result["papers"]}, {"10.1000/same-indexed", "10.1000/fresh-one", "10.1000/fresh-two"})

    def test_incremental_search_keeps_explained_history_when_no_new_results(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{"title": "Only matching multimodal paper", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/only-match", "source": "arxiv"}]
        try:
            options = {"expand_concepts": False, "ai_expand": False, "ai_screen": False}
            first = app.run_query("multimodal reasoning", ["arxiv"], 10, options=options)
            second = app.run_query("multimodal reasoning", ["arxiv"], 10, options=options)
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(len(first["papers"]), 1)
        self.assertEqual(len(second["papers"]), 1)
        self.assertTrue(second["repeat_fallback"])
        self.assertTrue(second["papers"][0]["seen_before"])

    def test_candidate_search_merges_cross_source_doi_arxiv_aliases(self):
        originals = {source: app.SOURCE_SEARCH[source] for source in ("arxiv", "openalex")}
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "Cross indexed multimodal paper", "abstract": "multimodal reasoning", "year": 2026,
            "arxiv_id": "2601.00001v2", "source": "arxiv", "external_id": "2601.00001v2",
        }]
        app.SOURCE_SEARCH["openalex"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "Cross indexed multimodal paper", "abstract": "multimodal reasoning", "year": 2026,
            "doi": "10.1000/cross-indexed", "source": "openalex", "external_id": "W123",
        }]
        try:
            result = app.run_query("multimodal reasoning", ["arxiv", "openalex"], 10, options={"expand_concepts": False, "ai_expand": False, "ai_screen": False})
        finally:
            app.SOURCE_SEARCH.update(originals)
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(result["papers"][0]["doi"], "10.1000/cross-indexed")
        self.assertEqual(result["papers"][0]["arxiv_id"], "2601.00001")
        self.assertEqual(result["papers"][0]["source_count"], 2)

    def test_search_memory_lookup_is_indexed_and_intent_scoped(self):
        intent_a, intent_b = "intent:field-a", "intent:field-b"
        with app.db_session() as db:
            app.record_discovery_search_memory(db, intent_a, {"title": "Long-lived fixture", "year": 2024, "doi": "10.1000/long-lived"}, "run-a", "2024-01-01T00:00:00+00:00")
            self.assertTrue(db.execute("SELECT 1 FROM discovery_search_memory WHERE intent_key=? AND identity_key=?", (intent_a, "doi:10.1000/long-lived")).fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM discovery_search_memory WHERE intent_key=? AND identity_key=?", (intent_b, "doi:10.1000/long-lived")).fetchone())

    def test_tavily_search_verifies_publisher_hit_against_crossref(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": [{
                    "title": "Food Volume Estimation Based on Deep Learning View Synthesis",
                    "url": "https://cvpr.thecvf.com/virtual/2026/poster/39733",
                    "content": "A deep learning method estimates food volume from a depth image.",
                    "score": 0.91,
                }]}).encode("utf-8")

        canonical = [{
            "title": "Food Volume Estimation Based on Deep Learning View Synthesis",
            "authors": "A Researcher", "year": 2018,
            "abstract": "Food volume estimation from a single depth image.",
            "doi": "10.1234/food-volume", "venue": "Nutrients",
            "url": "https://doi.org/10.1234/food-volume", "pdf_url": "",
            "source": "crossref", "external_id": "10.1234/food-volume",
            "type": "journal-article", "open_access": False,
        }]
        originals = (app._tavily_api_key, app.urlopen, app.search_crossref, app.search_openalex)
        app._tavily_api_key = lambda: "fixture-tavily-key"
        app.urlopen = lambda *_args, **_kwargs: FakeResponse()
        app.search_crossref = lambda _query, _limit, offset=0: canonical
        app.search_openalex = lambda *_args, **_kwargs: []
        try:
            rows = app.search_tavily("food volume estimation", 5)
        finally:
            app._tavily_api_key, app.urlopen, app.search_crossref, app.search_openalex = originals
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verification_status"], "verified")
        self.assertEqual(rows[0]["academic_source"], "crossref")
        self.assertFalse(rows[0]["discovery_only"])
        self.assertEqual(rows[0]["doi"], "10.1234/food-volume")
        self.assertEqual(rows[0]["url"], "https://doi.org/10.1234/food-volume")
        self.assertEqual(rows[0]["discovery_url"], "https://cvpr.thecvf.com/virtual/2026/poster/39733")

    def test_tavily_unverified_result_remains_discovery_only(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": [{
                    "title": "A Useful Research Blog",
                    "url": "https://example.org/blog/research",
                    "content": "This page discusses food volume estimation.",
                }]}).encode("utf-8")

        originals = (app._tavily_api_key, app.urlopen, app.search_crossref, app.search_openalex)
        app._tavily_api_key = lambda: "fixture-tavily-key"
        app.urlopen = lambda *_args, **_kwargs: FakeResponse()
        app.search_crossref = lambda *_args, **_kwargs: []
        app.search_openalex = lambda *_args, **_kwargs: []
        try:
            rows = app.search_tavily("food volume estimation", 5)
        finally:
            app._tavily_api_key, app.urlopen, app.search_crossref, app.search_openalex = originals
        self.assertEqual(rows[0]["verification_status"], "unverified")
        self.assertTrue(rows[0]["discovery_only"])
        self.assertEqual(rows[0]["url"], "https://example.org/blog/research")

    def test_tavily_caps_title_only_verification_and_keeps_discovery_rows(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": [
                    {"title": f"Web lead {index}", "url": f"https://example.org/paper-{index}", "content": "food volume estimation", "score": 0.9 - index / 100}
                    for index in range(5)
                ]}).encode("utf-8")

        originals = (app._tavily_api_key, app.urlopen, app._verify_tavily_result, dict(app.TAVILY_VERIFY_CACHE))
        calls = []
        fast_flags = []

        def fake_verify(item):
            calls.append(item["url"])
            fast_flags.append(bool(getattr(app.MODEL_SEARCH_FAST_SOURCE, "enabled", False)))
            result = dict(item)
            result.update({"source": "tavily", "external_id": item["url"], "verification_status": "unverified", "verification_sources": [], "discovery_only": True, "tavily_score": item.get("score", 0)})
            return result

        app._tavily_api_key = lambda: "fixture-tavily-key"
        app.urlopen = lambda *_args, **_kwargs: FakeResponse()
        app._verify_tavily_result = fake_verify
        with app.TAVILY_VERIFY_CACHE_LOCK:
            app.TAVILY_VERIFY_CACHE.clear()
        try:
            rows = app.search_tavily("food volume estimation", 5)
        finally:
            app._tavily_api_key, app.urlopen, app._verify_tavily_result = originals[:3]
            with app.TAVILY_VERIFY_CACHE_LOCK:
                app.TAVILY_VERIFY_CACHE.clear()
                app.TAVILY_VERIFY_CACHE.update(originals[3])
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(calls), app.TAVILY_VERIFY_MAX_TITLE_ONLY)
        self.assertEqual(fast_flags, [True] * app.TAVILY_VERIFY_MAX_TITLE_ONLY)
        self.assertTrue(all(row["discovery_only"] for row in rows))

    def test_model_search_tool_can_select_tavily_lane(self):
        originals = (app._tavily_api_key, app.SOURCE_SEARCH["tavily"])
        app._tavily_api_key = lambda: "fixture-tavily-key"
        app.SOURCE_SEARCH["tavily"] = lambda _query, _limit, offset=0: [{
            "title": "Verified food volume paper", "url": "https://doi.org/10.1234/volume",
            "discovery_url": "https://cvpr.thecvf.com/virtual/2026/poster/39733",
            "doi": "10.1234/volume", "abstract": "food volume estimation", "source": "tavily",
            "verification_status": "verified", "verification_sources": ["crossref"],
            "academic_source": "crossref", "discovery_only": False,
        }]
        try:
            result = app._model_search_tool_result({"query": "food volume estimation", "sources": ["tavily"], "limit": 2}, {}, {}, set(), set(), {}, [])
        finally:
            app._tavily_api_key, app.SOURCE_SEARCH["tavily"] = originals
        self.assertEqual(result["source_results"][0]["source"], "tavily")
        self.assertEqual(result["results"][0]["verification_status"], "verified")
        self.assertEqual(result["results"][0]["url"], "https://doi.org/10.1234/volume")
        self.assertEqual(result["results"][0]["provider_sources"][0]["url"], "https://cvpr.thecvf.com/virtual/2026/poster/39733")

    def test_model_search_prefetch_skips_source_with_open_cooldown(self):
        """A rate-limited index must not be called again in later tool turns."""
        original = app.SOURCE_SEARCH["openalex"]
        calls = []

        def should_not_run(*_args, **_kwargs):
            calls.append(True)
            return [{"title": "unexpected", "url": "https://doi.org/10.1/unexpected"}]

        app.SOURCE_SEARCH["openalex"] = should_not_run
        host = "api.openalex.org"
        with app.PUBLIC_SOURCE_STATE_LOCK:
            app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        app._cooldown_public_source(app.MODEL_SEARCH_SOURCE_PROBE_URLS["openalex"], 60)
        try:
            fetched = app._model_search_prefetch(
                {"query": "food volume estimation", "sources": ["openalex"], "limit": 3},
                [],
            )
        finally:
            app.SOURCE_SEARCH["openalex"] = original
            with app.PUBLIC_SOURCE_STATE_LOCK:
                app.PUBLIC_SOURCE_COOLDOWNS.pop(host, None)
        self.assertEqual(calls, [])
        self.assertEqual(fetched[0][1], [])
        self.assertIn("本轮已跳过", fetched[0][2])

    def test_model_search_prefetch_runs_independent_angles_in_parallel(self):
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        original = app.SOURCE_SEARCH["arxiv"]

        def delayed_search(query, _limit, offset=0):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            suffix = "01" if "angle one" in query else "02"
            return [{"title": f"Parallel candidate {suffix}", "arxiv_id": f"2601.000{suffix}", "url": f"https://arxiv.org/abs/2601.000{suffix}", "source": "arxiv"}]

        app.SOURCE_SEARCH["arxiv"] = delayed_search
        calls = [
            {"query": "angle one food volume", "sources": ["arxiv"], "limit": 2},
            {"query": "angle two food volume", "sources": ["arxiv"], "limit": 2},
        ]
        try:
            with app.ThreadPoolExecutor(max_workers=2) as pool:
                prefetched = list(pool.map(lambda call: app._model_search_prefetch(call, []), calls))
            inventory, identity_index = {}, {}
            outputs = [app._model_search_tool_result(call, inventory, identity_index, set(), set(), {}, [], batch) for call, batch in zip(calls, prefetched)]
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual([row["title"] for output in outputs for row in output["results"]], ["Parallel candidate 01", "Parallel candidate 02"])

    def test_model_search_prefetch_fetches_continuation_only_for_repeat_heavy_page(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []

        def repeated_search(_query, _limit, offset=0):
            calls.append(offset)
            if offset == 0:
                return [
                    {"title": "Previously seen paper", "arxiv_id": "2606.00001", "url": "https://arxiv.org/abs/2606.00001"},
                    {"title": "Previously seen paper two", "arxiv_id": "2606.00002", "url": "https://arxiv.org/abs/2606.00002"},
                ]
            return [{"title": "Fresh continuation paper", "arxiv_id": "2606.00003", "url": "https://arxiv.org/abs/2606.00003"}]

        app.SOURCE_SEARCH["arxiv"] = repeated_search
        try:
            fetched = app._model_search_prefetch(
                {"query": "food volume estimation", "sources": ["arxiv"], "limit": 2},
                [], {app.title_key("Previously seen paper"), app.title_key("Previously seen paper two")}, set(), True,
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(calls, [0, 2])
        self.assertEqual(len(fetched[0][1]), 3)

    def test_model_search_prefetch_crosses_multiple_repeat_pages_to_keep_requested_count(self):
        original = app.SOURCE_SEARCH["arxiv"]
        calls = []
        seen = {app.title_key("Historical paper one"), app.title_key("Historical paper two")}

        def repeated_search(_query, _limit, offset=0):
            calls.append(offset)
            if offset == 0:
                return [
                    {"title": "Historical paper one", "arxiv_id": "2401.00001", "url": "https://arxiv.org/abs/2401.00001"},
                    {"title": "Historical paper two", "arxiv_id": "2401.00002", "url": "https://arxiv.org/abs/2401.00002"},
                ]
            if offset == 2:
                return [
                    {"title": "Fresh paper one", "arxiv_id": "2601.00001", "url": "https://arxiv.org/abs/2601.00001"},
                    {"title": "Historical paper one", "arxiv_id": "2401.00001", "url": "https://arxiv.org/abs/2401.00001"},
                ]
            if offset == 4:
                return [
                    {"title": "Fresh paper two", "arxiv_id": "2601.00002", "url": "https://arxiv.org/abs/2601.00002"},
                    {"title": "Historical paper two", "arxiv_id": "2401.00002", "url": "https://arxiv.org/abs/2401.00002"},
                ]
            return []

        app.SOURCE_SEARCH["arxiv"] = repeated_search
        try:
            fetched = app._model_search_prefetch(
                {"query": "food volume estimation", "sources": ["arxiv"], "limit": 2},
                [], seen, set(), True,
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original
        self.assertEqual(calls, [0, 2, 4])
        self.assertEqual(len(fetched[0][1]), 6)

    def test_model_web_search_executes_same_turn_tool_calls_concurrently(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"])
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_connector(query, _limit, offset=0):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            suffix = "01" if "angle one" in query else "02"
            return [{"title": f"Integrated parallel paper {suffix}", "abstract": "Food volume estimation.", "year": 2026,
                     "arxiv_id": f"2601.000{suffix}", "url": f"https://arxiv.org/abs/2601.000{suffix}", "source": "arxiv"}]

        requests = []

        def fake_request(_endpoint, _payload, _key, timeout, accept="application/json"):
            requests.append(_payload)
            if len(requests) == 1:
                calls = []
                for index, angle in enumerate(("angle one food volume", "angle two food volume"), start=1):
                    calls.append({"id": f"call-{index}", "type": "function", "function": {"name": app.MODEL_SEARCH_TOOL_NAME,
                                  "arguments": json.dumps({"query": angle, "sources": ["arxiv"], "limit": 2})}})
                return FakeResponse({"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]})
            return FakeResponse({"choices": [{"message": {"content": json.dumps({
                "results": [{"result_id": "C1", "relevance_label": "direct", "relevance_reason": "matching food volume task"},
                            {"result_id": "C2", "relevance_label": "direct", "relevance_reason": "matching food volume task"}],
                "gaps": [], "next_queries": [],
            })}}]})

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "fixture-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.SOURCE_SEARCH["arxiv"] = fake_connector
        try:
            result = app._model_web_search_unbounded("food volume estimation", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"] = originals
        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual([paper["title"] for paper in result["results"]], ["Integrated parallel paper 01", "Integrated parallel paper 02"])
        self.assertEqual([search["query"] for search in result["searches"]], ["angle one food volume", "angle two food volume"])

    def test_model_web_search_normalizes_only_source_backed_results(self):
        result = app.normalize_model_search_results({"results": [
            {"title": "A real paper", "url": "https://arxiv.org/abs/2601.00001", "arxiv_id": "2601.00001v2", "authors": ["A", "B"], "year": "2026", "abstract": "A useful abstract", "source_citations": ["https://arxiv.org/abs/2601.00001"]},
            {"title": "Unrelated result", "url": "https://journal.example/unrelated", "relevance_label": "unrelated"},
            {"title": "A result with no source", "doi": "10.1000/missing"},
            {"title": "Duplicate paper", "url": "https://doi.org/10.1000/duplicate", "doi": "10.1000/duplicate"},
            {"title": "Same duplicate", "url": "https://publisher.example/paper", "doi": "10.1000/duplicate"},
        ]}, "research question", "custom", "model-x")
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["unrelated_results_filtered"], 1)
        self.assertEqual(result["results"][0]["arxiv_id"], "2601.00001")
        self.assertFalse(result["results"][0]["verified"])
        self.assertTrue(result["results"][0]["source_attested"])

    def test_model_web_search_chat_function_call_executes_connector_and_validates_ids(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"])
        requests = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_connector(_query, _limit, offset=0):
            return [] if offset else [{
                "title": "Real multimodal volume estimation paper",
                "abstract": "Food volume estimation from images with depth cues.",
                "year": 2026,
                "arxiv_id": "2601.00001v1",
                "url": "https://arxiv.org/abs/2601.00001",
                "external_id": "2601.00001v1",
                "source": "arxiv",
            }]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            requests.append(payload)
            if len(requests) == 1:
                response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "food volume estimation depth", "sources": ["arxiv"], "limit": 3})},
                }]}}]}
            else:
                response = {"choices": [{"message": {"content": json.dumps({
                    "results": [
                        {"result_id": "C999", "relevance_label": "direct", "relevance_reason": "fabricated id"},
                        {"result_id": "C1", "relevance_label": "direct", "relevance_reason": "same object and task"},
                    ], "gaps": [], "next_queries": [],
                }, ensure_ascii=False)}}]}
            return FakeResponse(response)

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "relay-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.SOURCE_SEARCH["arxiv"] = fake_connector
        try:
            result = app._model_web_search_unbounded("用深度学习估计食物体积", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"] = originals
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual([item["title"] for item in result["results"]], ["Real multimodal volume estimation paper"])
        self.assertEqual(result["results"][0]["search_paths"][0]["source"], "arxiv")
        self.assertNotIn("C999", json.dumps(result, ensure_ascii=False))
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], app.MODEL_SEARCH_TOOL_NAME)

    def test_model_web_search_responses_function_call_executes_connector(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["openalex"])
        requests = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        app.SOURCE_SEARCH["openalex"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "OpenAlex volume benchmark",
            "abstract": "A benchmark for visual volume measurement.",
            "year": 2025,
            "doi": "10.1000/volume-benchmark",
            "url": "https://doi.org/10.1000/volume-benchmark",
            "external_id": "W123",
            "source": "openalex",
        }]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            requests.append(payload)
            if len(requests) == 1:
                response = {"output": [{"type": "function_call", "call_id": "resp-call-1", "name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "visual volume measurement benchmark", "sources": ["openalex"], "limit": 2})}]}
            else:
                response = {"output_text": json.dumps({"results": [{"result_id": "C1", "relevance_label": "direct", "relevance_reason": "matching task"}], "gaps": [], "next_queries": []}, ensure_ascii=False)}
            return FakeResponse(response)

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "responses-model", "protocol": "responses", "api_url": "https://relay.example/v1/responses", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        try:
            result = app._model_web_search_unbounded("视觉体积测量", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["openalex"] = originals
        self.assertEqual(result["results"][0]["doi"], "10.1000/volume-benchmark")
        self.assertEqual(requests[0]["tools"][0]["name"], app.MODEL_SEARCH_TOOL_NAME)
        self.assertEqual(requests[0]["input"][0]["role"], "user")

    def test_model_web_search_accepts_selection_aliases_but_validates_candidate_ids(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["openalex"])

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        app.SOURCE_SEARCH["openalex"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "Food volume estimation with depth cues",
            "abstract": "Estimates food volume from meal images and depth cues.",
            "year": 2025,
            "doi": "10.4242/food-volume-depth",
            "url": "https://doi.org/10.4242/food-volume-depth",
            "external_id": "WFOODVOLUME",
            "source": "openalex",
        }]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            if "tools" in payload:
                return FakeResponse({"output": [{"type": "function_call", "call_id": "lookup-1", "name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "food volume image depth", "sources": ["openalex"], "limit": 3})}]})
            return FakeResponse({"output_text": json.dumps({
                "selected_papers": [
                    {"candidate_id": "C1", "relevance": "direct", "reason": "same object and estimation task"},
                    {"candidate_id": "C999", "relevance": "direct", "reason": "must be rejected"},
                ],
                "gaps": [], "next_queries": [],
            }, ensure_ascii=False)})

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "fixture-model", "protocol": "responses", "api_url": "https://relay.example/v1/responses", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        try:
            result = app._model_web_search_unbounded("根据餐盘图像估算食物体积", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["openalex"] = originals

        self.assertEqual([item["title"] for item in result["results"]], ["Food volume estimation with depth cues"])
        self.assertEqual(result["results"][0]["relevance_label"], "direct")
        self.assertEqual(result["results"][0]["relevance_reason"], "same object and estimation task")

    def test_model_search_selection_accepts_single_item_object(self):
        rows = app._model_search_selection_rows({
            "result_id": "C3",
            "relevance_label": "adjacent",
            "relevance_reason": "研究对象相同，但聚焦于份量估计而非体积重建。",
        })
        self.assertEqual(rows, [{
            "result_id": "C3",
            "relevance_label": "adjacent",
            "relevance_reason": "研究对象相同，但聚焦于份量估计而非体积重建。",
        }])

    def test_model_search_selection_decodes_nested_json_string(self):
        rows = app._model_search_selection_rows({
            "selection": json.dumps({
                "ranked_results": [{
                    "result_id": "C4",
                    "relevance_label": "direct",
                    "relevance_reason": "对象和任务一致",
                }],
            }, ensure_ascii=False),
        })
        self.assertEqual(rows[0]["result_id"], "C4")

    def test_model_web_search_empty_selection_forces_new_search_angle(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"])
        requests = []
        queries = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_connector(query, _limit, offset=0):
            queries.append(query)
            if "alternative" in query:
                return [{"title": "New angle paper", "abstract": "new food volume angle", "year": 2026, "arxiv_id": "2601.00002", "url": "https://arxiv.org/abs/2601.00002", "source": "arxiv"}]
            return [{"title": "Previously seen paper", "abstract": "old food volume", "year": 2024, "arxiv_id": "2401.00001", "url": "https://arxiv.org/abs/2401.00001", "source": "arxiv"}]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            requests.append(payload)
            step = len(requests)
            if step == 1:
                body = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "call-old", "type": "function", "function": {"name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "food volume baseline", "sources": ["arxiv"], "limit": 2})}}]}}]}
            elif step == 2:
                body = {"choices": [{"message": {"content": json.dumps({"results": [], "gaps": ["first angle was already seen"], "next_queries": ["alternative food volume method"]})}}]}
            elif step == 3:
                body = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "call-new", "type": "function", "function": {"name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "alternative food volume method", "sources": ["arxiv"], "limit": 2})}}]}}]}
            else:
                body = {"choices": [{"message": {"content": json.dumps({"results": [{"result_id": "C1", "relevance_label": "direct", "relevance_reason": "new angle"}], "gaps": [], "next_queries": []})}}]}
            return FakeResponse(body)

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "relay-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.SOURCE_SEARCH["arxiv"] = fake_connector
        try:
            result = app._model_web_search_unbounded("food volume estimation", 5, {"_seen_titles": ["Previously seen paper"]})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"] = originals
        self.assertEqual(result["results"][0]["title"], "New angle paper")
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(queries[:2], ["food volume baseline", "alternative food volume method"])
        self.assertIn("food volume estimation", queries)
        self.assertTrue(any("换一个明确的检索角度" in json.dumps(payload, ensure_ascii=False) for payload in requests[2:]))

    def test_model_web_search_repeat_short_selection_uses_connector_fill_without_hints(self):
        """A repeat-heavy page must be refilled even when final JSON has no gaps."""
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"])
        requests = []
        connector_queries = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_connector(query, _limit, offset=0):
            connector_queries.append((query, offset))
            if offset:
                return []
            if query == "food volume baseline":
                return [
                    {"title": "Previously seen paper", "abstract": "food volume estimation", "year": 2024, "arxiv_id": "2401.00001", "url": "https://arxiv.org/abs/2401.00001", "source": "arxiv"},
                    {"title": "Fresh baseline paper", "abstract": "food volume estimation", "year": 2026, "arxiv_id": "2601.00001", "url": "https://arxiv.org/abs/2601.00001", "source": "arxiv"},
                ]
            return [{"title": "Local refill paper", "abstract": "food volume estimation", "year": 2026, "arxiv_id": "2601.00002", "url": "https://arxiv.org/abs/2601.00002", "source": "arxiv"}]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            requests.append(payload)
            if len(requests) == 1:
                body = {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                    "id": "repeat-call", "type": "function",
                    "function": {"name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({"query": "food volume baseline", "sources": ["arxiv"], "limit": 3})},
                }]}}]}
            else:
                # Deliberately omit gaps/next_queries. The repeat counter from
                # the tool result alone must still trigger the bounded fill.
                body = {"choices": [{"message": {"content": json.dumps({
                    "results": [{"result_id": "C1", "relevance_label": "direct", "relevance_reason": "same task"}],
                }, ensure_ascii=False)}}]}
            return FakeResponse(body)

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "relay-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.SOURCE_SEARCH["arxiv"] = fake_connector
        try:
            result = app._model_web_search_unbounded(
                "food volume estimation", 3,
                {"_seen_titles": ["Previously seen paper"]},
            )
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"] = originals

        self.assertEqual({item["title"] for item in result["results"]}, {
            "Fresh baseline paper", "Previously seen paper", "Local refill paper",
        })
        self.assertGreaterEqual(result["local_fill_count"], 1)
        self.assertTrue(any(query != "food volume baseline" for query, _offset in connector_queries))

    def test_model_web_search_recovers_real_candidates_when_final_json_is_invalid(self):
        """A malformed ranking response must not discard the tool inventory."""
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"])
        requests = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        def fake_connector(_query, _limit, offset=0):
            if offset:
                return []
            return [{
                "title": f"Food volume estimation candidate {index}",
                "abstract": "Food volume estimation from meal images with deep learning.",
                "year": 2026,
                "arxiv_id": f"2601.1000{index}",
                "url": f"https://arxiv.org/abs/2601.1000{index}",
                "source": "arxiv",
            } for index in range(1, 7)]

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            requests.append(payload)
            if len(requests) == 1:
                return FakeResponse({"choices": [{"message": {"role": "assistant", "tool_calls": [{
                    "id": "inventory-call", "type": "function",
                    "function": {"name": app.MODEL_SEARCH_TOOL_NAME, "arguments": json.dumps({
                        "query": "food volume estimation", "sources": ["arxiv"], "limit": 6,
                    })},
                }]}}]})
            # Simulate a relay that repeatedly emits malformed final JSON.
            return FakeResponse({"choices": [{"message": {"content": "{results: ["}}]})

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "fixture-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.SOURCE_SEARCH["arxiv"] = fake_connector
        try:
            result = app._model_web_search_unbounded("food volume estimation", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.SOURCE_SEARCH["arxiv"] = originals

        self.assertEqual(len(result["results"]), 5)
        self.assertTrue(result["recovered_from_model_error"])
        self.assertTrue(all(item.get("selection_backfilled") for item in result["results"]))
        self.assertGreaterEqual(len(requests), 3)

    def test_repeat_search_history_fill_reaches_requested_page_size(self):
        """Repeat avoidance is a ranking preference, not a page-size reducer."""
        query = "用图像估计食物体积"
        options = {"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False}
        intent = app.discovery_intent_key(query, options)
        prior_run = "history-page-size-fixture"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO runs(id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?)", (prior_run, query, "success", now, now))
            for index in range(30):
                record = {
                    "id": f"candidate:history-page-size-{index}",
                    "title": f"Historical food volume paper {index}",
                    "abstract": "Food volume estimation from meal images.",
                    "year": 2025,
                    "doi": f"10.4242/history-page-size-{index}",
                    "url": f"https://doi.org/10.4242/history-page-size-{index}",
                    "source": "model_web",
                    "external_id": f"10.4242/history-page-size-{index}",
                    "intent_key": intent,
                    "history_quality_accepted": True,
                }
                db.execute("INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)", (record["id"], prior_run, app.candidate_identity(record), json.dumps(record, ensure_ascii=False), now))
                app.record_discovery_search_memory(db, intent, record, prior_run, now)

        original_model = app.model_web_search
        app.model_web_search = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("模型最终相关性筛选输出无效"))
        try:
            result = app.run_query(query, [], 25, options=options)
        finally:
            app.model_web_search = original_model

        self.assertEqual(result["accepted_count"], 25)
        self.assertEqual(result["shortfall_count"], 0)
        self.assertEqual(len(result["papers"]), 25)
        self.assertTrue(all(item.get("seen_before") for item in result["papers"]))
        self.assertTrue(result["repeat_fallback"])

    def test_model_web_search_rejects_model_without_function_calling(self):
        originals = (app.model_settings, app.model_api_key, app.open_compatible_model_request, app.time.sleep)
        calls = []

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                value = {"results": [], "gaps": ["the model returned JSON but did not call tools"], "next_queries": []}
                return json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode("utf-8")

        def fake_request(_endpoint, payload, _key, timeout, accept="application/json"):
            calls.append(payload)
            return FakeResponse()

        app.model_settings = lambda: {"enabled": True, "api_key_set": True, "provider": "Relay", "model": "relay-model", "protocol": "chat_completions", "api_url": "https://relay.example/v1/chat/completions", "base_url": "https://relay.example/v1", "temperature": 0}
        app.model_api_key = lambda: "fixture-key"
        app.open_compatible_model_request = fake_request
        app.time.sleep = lambda _seconds: None
        try:
            with self.assertRaises(app.ModelWebSearchUnsupported):
                app._model_web_search_unbounded("food volume estimation", 5, {})
        finally:
            app.model_settings, app.model_api_key, app.open_compatible_model_request, app.time.sleep = originals
        self.assertEqual(len(calls), 2)
        self.assertIn("必须先调用", json.dumps(calls[1], ensure_ascii=False))
    def test_model_web_backend_uses_normalized_candidates(self):
        original = app.model_web_search
        original_connector = app.SOURCE_SEARCH["arxiv"]
        captured = {}
        intent = app.discovery_intent_key("multimodal reasoning", {})
        with app.db_session() as db:
            app.record_discovery_search_memory(db, intent, {"title": "Previously surfaced multimodal paper", "doi": "10.1000/old"}, "prior-run")

        def fake_model_search(_query, _limit, search_options):
            captured.update(search_options)
            return {"results": [{
            "title": "Model web multimodal paper", "abstract": "multimodal reasoning", "year": 2026,
            "doi": "10.1000/model-web", "url": "https://publisher.example/paper", "source": "model_web",
            "relevance_label": "direct", "relevance_reason": "same task and modality",
            "source_citations": ["https://publisher.example/paper"], "source_attested": True,
            }]}

        app.model_web_search = fake_model_search
        app.SOURCE_SEARCH["arxiv"] = lambda *_args, **_kwargs: self.fail("connector should not run after model search succeeds")
        try:
            result = app.run_query("multimodal reasoning", [], 10, options={"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False})
        finally:
            app.model_web_search = original
            app.SOURCE_SEARCH["arxiv"] = original_connector
        self.assertEqual(result["papers"][0]["source"], "model_web")
        self.assertEqual(result["papers"][0]["model_relevance_label"], "direct")
        self.assertEqual(result["papers"][0]["relevance_reason"], "same task and modality")
        self.assertEqual(result["papers"][0]["source_citations"], ["https://publisher.example/paper"])
        self.assertEqual(result["summary"]["search_backend"], "model_web")
        self.assertIn("Previously surfaced multimodal paper", captured["_seen_titles"])

    def test_model_web_source_degradation_is_persisted_as_partial(self):
        """Useful candidates must keep an explicit retryable partial state."""
        original = app.model_web_search

        def fake_model_search(_query, _limit, _search_options):
            return {
                "results": [{
                    "title": "Food volume estimation with RGB-D images",
                    "abstract": "We estimate food volume from RGB-D meal images.",
                    "year": 2026,
                    "doi": "10.1000/source-degraded",
                    "url": "https://doi.org/10.1000/source-degraded",
                    "source": "model_web",
                    "relevance_label": "direct",
                    "relevance_reason": "same object and task",
                }],
                "searches": [{
                    "query": "food volume estimation",
                    "sources": [{"source": "arxiv", "error": "read operation timed out"}],
                }],
            }

        app.model_web_search = fake_model_search
        try:
            result = app.run_query(
                "food volume estimation",
                [],
                5,
                options={"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False},
            )
        finally:
            app.model_web_search = original

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accepted_count"], 1)
        self.assertIn("模型检索部分来源不可用", result["error"])
        snapshot = app.run_results_snapshot(result["id"])
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["run"]["job"]["status"], "partial")
        self.assertTrue(snapshot["run"]["job"]["next_retry_at"])

    def test_model_web_quality_gate_refills_short_repeat_page_from_history(self):
        """Quality filtering must not turn a repeated page into a short page."""
        query = "用图像估计食物体积"
        options = {"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False}
        intent = app.discovery_intent_key(query, options)
        prior_run = "quality-refill-history-run"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?)",
                (prior_run, query, "success", now, now),
            )
            for index in range(4):
                record = {
                    "id": f"candidate:history-quality-{index}",
                    "title": f"Historical food volume paper {index}",
                    "abstract": "Food volume estimation from meal images with a validated measurement method.",
                    "year": 2025,
                    "doi": f"10.4242/history-quality-{index}",
                    "url": f"https://doi.org/10.4242/history-quality-{index}",
                    "source": "model_web",
                    "external_id": f"10.4242/history-quality-{index}",
                    "intent_key": intent,
                    "history_quality_accepted": True,
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], prior_run, app.candidate_identity(record), json.dumps(record, ensure_ascii=False), now),
                )
                app.record_discovery_search_memory(db, intent, record, prior_run, now)

        original = app.model_web_search

        def fake_model_search(_query, _limit, _search_options):
            # The first pool is full, but only one candidate passes the local
            # object/task gate. This is the case that previously stayed short.
            rows = [{
                "title": "Current food volume estimation paper",
                "abstract": "Food volume estimation from meal images.",
                "year": 2026,
                "doi": "10.4242/current-quality-refill",
                "url": "https://doi.org/10.4242/current-quality-refill",
                "source": "model_web",
                "relevance_label": "direct",
                "relevance_reason": "same object and task",
            }]
            rows.extend({
                "title": f"Current food security distractor {index}",
                "abstract": "Food security policy analysis without image measurement.",
                "year": 2026,
                "doi": f"10.4242/current-distractor-{index}",
                "url": f"https://doi.org/10.4242/current-distractor-{index}",
                "source": "model_web",
                "relevance_label": "direct",
            } for index in range(9))
            return {"results": rows, "searches": []}

        app.model_web_search = fake_model_search
        try:
            result = app.run_query(query, [], 5, options=options)
        finally:
            app.model_web_search = original

        self.assertEqual(result["accepted_count"], 5)
        self.assertEqual(result["shortfall_count"], 0)
        self.assertEqual(result["summary"]["model_web_quality_refill"], 4)
        self.assertEqual(sum(item["title"].startswith("Historical") for item in result["papers"]), 4)
        self.assertEqual(sum(bool(item.get("seen_before")) for item in result["papers"]), 4)
        self.assertTrue(all(item.get("selection_backfilled") for item in result["papers"][1:]))
        # History refill happens after the initial candidate staging pass;
        # the current run must still expose those rows after a page reload.
        snapshot = app.run_results_snapshot(result["id"])
        self.assertEqual(snapshot["accepted_count"], 5)
        self.assertEqual(snapshot["run"]["candidate_count"], 5)
        self.assertEqual(len(snapshot["papers"]), 5)

    def test_model_web_repeat_refill_recovers_legacy_candidates_without_intent_field(self):
        """Legacy snapshots must still count toward the requested page size."""
        query = "用图像估计食物体积"
        options = {"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False}
        intent = app.discovery_intent_key(query, options)
        prior_run = "legacy-history-intent-run"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?)",
                (prior_run, query, "success", now, now),
            )
            for index in range(2):
                record = {
                    "id": f"candidate:legacy-history-{index}",
                    "title": f"Legacy food volume paper {index}",
                    "abstract": "Food volume estimation from meal images.",
                    "year": 2024,
                    "doi": f"10.4242/legacy-history-{index}",
                    "url": f"https://doi.org/10.4242/legacy-history-{index}",
                    "source": "model_web",
                    # Deliberately omit intent_key: this mirrors snapshots
                    # written before intent-scoped history was introduced.
                    "history_quality_accepted": True,
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], prior_run, app.candidate_identity(record), json.dumps(record, ensure_ascii=False), now),
                )
                app.record_discovery_search_memory(db, intent, record, prior_run, now)

        original = app.model_web_search

        def fake_model_search(_query, _limit, _search_options):
            return {"results": [{
                "title": "Current food volume paper",
                "abstract": "Food volume estimation from meal images.",
                "year": 2026,
                "doi": "10.4242/current-legacy-refill",
                "url": "https://doi.org/10.4242/current-legacy-refill",
                "source": "model_web",
                "relevance_label": "direct",
                "relevance_reason": "same object and task",
            }], "searches": []}

        app.model_web_search = fake_model_search
        try:
            result = app.run_query(query, [], 3, options=options)
        finally:
            app.model_web_search = original

        self.assertEqual(result["accepted_count"], 3)
        self.assertEqual(result["shortfall_count"], 0)
        self.assertEqual(
            {item["title"] for item in result["papers"]},
            {"Current food volume paper", "Legacy food volume paper 0", "Legacy food volume paper 1"},
        )

    def test_model_web_failure_keeps_history_page_as_partial_and_reloadable(self):
        query = "用图像估计食物体积"
        options = {"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False}
        intent = app.discovery_intent_key(query, options)
        prior_run = "model-web-failure-history-run"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,completed_at) VALUES(?,?,?,?,?)",
                (prior_run, query, "success", now, now),
            )
            for index in range(3):
                record = {
                    "id": f"candidate:model-failure-history-{index}",
                    "title": f"Historical fallback food volume paper {index}",
                    "abstract": "Food volume estimation from meal images.",
                    "year": 2025,
                    "doi": f"10.4242/model-failure-history-{index}",
                    "url": f"https://doi.org/10.4242/model-failure-history-{index}",
                    "source": "model_web",
                    "external_id": f"10.4242/model-failure-history-{index}",
                    "intent_key": intent,
                    "history_quality_accepted": True,
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], prior_run, app.candidate_identity(record), json.dumps(record, ensure_ascii=False), now),
                )
                app.record_discovery_search_memory(db, intent, record, prior_run, now)

        original_model = app.model_web_search
        app.model_web_search = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("模型最终相关性筛选输出无效"))
        try:
            result = app.run_query(query, [], 3, options=options)
        finally:
            app.model_web_search = original_model

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accepted_count"], 3)
        snapshot = app.run_results_snapshot(result["id"])
        self.assertEqual(snapshot["status"], "partial")
        self.assertEqual(snapshot["accepted_count"], 3)
        self.assertEqual(len(snapshot["papers"]), 3)

    def test_model_web_failure_falls_back_only_when_explicitly_enabled(self):
        original_model = app.model_web_search
        original_connector = app.SOURCE_SEARCH["arxiv"]

        def unsupported(*_args, **_kwargs):
            raise app.ModelWebSearchUnsupported("当前模型接口不支持联网搜索工具")

        app.model_web_search = unsupported
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{"title": "Connector fallback paper", "abstract": "multimodal reasoning", "year": 2026, "doi": "10.1000/fallback", "source": "arxiv"}]
        try:
            result = app.run_query("multimodal reasoning", ["arxiv"], 10, options={"search_backend": "model_web", "model_web_fallback_to_connectors": True, "expand_concepts": False, "ai_expand": False, "ai_screen": False})
        finally:
            app.model_web_search = original_model
            app.SOURCE_SEARCH["arxiv"] = original_connector
        self.assertEqual(result["papers"][0]["title"], "Connector fallback paper")
        self.assertIn("切换到学术数据库", result["model_web_warning"])
        self.assertTrue(result["summary"]["model_web_fallback"])

    def test_model_web_failure_does_not_silently_call_academic_connectors(self):
        original_model = app.model_web_search
        original_connector = app.SOURCE_SEARCH["arxiv"]
        app.model_web_search = lambda *_args, **_kwargs: (_ for _ in ()).throw(app.ModelWebSearchUnsupported("当前模型 API 没有联网搜索工具"))
        app.SOURCE_SEARCH["arxiv"] = lambda *_args, **_kwargs: self.fail("AI-only search must not silently use academic connectors")
        try:
            result = app.run_query("multimodal reasoning", [], 10, options={"search_backend": "model_web", "expand_concepts": False, "ai_expand": False, "ai_screen": False})
        finally:
            app.model_web_search = original_model
            app.SOURCE_SEARCH["arxiv"] = original_connector
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["papers"], [])
        self.assertIn("没有联网搜索工具", result["model_web_warning"])
        self.assertEqual(result["plan"]["search_backend_executed"], "model_web_failed")

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
        self.assertEqual(result["release"]["schema_version"], str(app.DB_SCHEMA_VERSION))
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

    def test_tavily_html_arxiv_url_is_normalized(self):
        self.assertEqual(
            app._tavily_extract_identifiers("https://arxiv.org/html/2106.11776v3"),
            ("", "2106.11776"),
        )

    def test_cross_provider_doi_arxiv_alias_merges_without_year(self):
        records, duplicates = app.merge_search_records([
            {
                "title": "A Comprehensive Survey of Image-Based Food Recognition and Volume Estimation Methods for Dietary Assessment",
                "year": 2021,
                "doi": "10.3390/healthcare9121676",
                "abstract": "journal metadata",
                "source": "crossref",
            },
            {
                "title": "A Comprehensive Survey of Image-Based Food Recognition and Volume Estimation Methods for Dietary Assessment",
                "year": "",
                "arxiv_id": "2106.11776v3",
                "abstract": "preprint metadata",
                "source": "tavily",
            },
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(duplicates, 1)
        self.assertEqual(records[0]["doi"], "10.3390/healthcare9121676")
        self.assertEqual(records[0]["arxiv_id"], "2106.11776")

    def test_agent_discovery_plan_has_safe_local_fallback_without_key(self):
        plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore"})
        self.assertFalse(plan["ai_expansion"]["enabled"])
        self.assertTrue(plan["query_variants"])
        self.assertNotIn("api_key", json.dumps(plan, ensure_ascii=False).lower())

    def test_agent_discovery_plan_uses_structured_model_request_and_repairs_schema(self):
        original_settings = app.model_settings
        original_key = app.model_api_key
        original_request = app.model_json_request
        calls = []
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "temperature": 0.2}
        app.model_api_key = lambda: "fixture-key"

        def fake_request(prompt, payload, timeout=120, max_output_tokens=None):
            calls.append({"prompt": prompt, "payload": payload, "timeout": timeout, "max_output_tokens": max_output_tokens})
            if len(calls) == 1:
                return {"synonyms": ["vision-language model"], "adjacent_terms": "cross-modal retrieval"}
            return {
                "synonyms": "vision-language model",
                "adjacent_terms": ["cross-modal retrieval", 42],
                "queries": ["multimodal reasoning benchmark"],
            }

        app.model_json_request = fake_request
        try:
            plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore", "search_depth": "deep"})
        finally:
            app.model_settings = original_settings
            app.model_api_key = original_key
            app.model_json_request = original_request
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["timeout"], 60)
        self.assertEqual(calls[0]["max_output_tokens"], 1200)
        self.assertEqual(calls[0]["payload"]["query"], "多模态推理")
        self.assertTrue(plan["ai_expansion"]["enabled"])
        queries = [item["query"] for item in plan["query_variants"]]
        self.assertIn("vision-language model", queries)
        self.assertIn("cross-modal retrieval", queries)
        self.assertIn("multimodal reasoning benchmark", queries)

    def test_agent_discovery_plan_falls_back_when_model_json_stays_invalid(self):
        original_settings = app.model_settings
        original_key = app.model_api_key
        original_request = app.model_json_request
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "temperature": 0.2}
        app.model_api_key = lambda: "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("模型输出不是有效 JSON"))
        try:
            plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore", "search_depth": "deep"})
        finally:
            app.model_settings = original_settings
            app.model_api_key = original_key
            app.model_json_request = original_request
        self.assertFalse(plan["ai_expansion"]["enabled"])
        self.assertTrue(plan["ai_expansion"]["fallback"])
        self.assertIn("已回退本地规则检索计划", plan["ai_expansion"]["reason"])
        self.assertTrue(plan["query_variants"])

    def test_agent_discovery_plan_falls_back_on_provider_transport_failure(self):
        """A provider outage must not turn optional AI expansion into a 502."""
        original_settings = app.model_settings
        original_key = app.model_api_key
        original_request = app.model_json_request
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "temperature": 0.2}
        app.model_api_key = lambda: "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(app.URLError("fixture timeout"))
        try:
            plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore"})
        finally:
            app.model_settings = original_settings
            app.model_api_key = original_key
            app.model_json_request = original_request
        self.assertFalse(plan["ai_expansion"]["enabled"])
        self.assertTrue(plan["ai_expansion"]["fallback"])
        self.assertTrue(plan["query_variants"])
        self.assertIn("已回退本地规则检索计划", plan["ai_expansion"]["reason"])

    def test_agent_discovery_plan_bounds_ai_variants_by_search_depth(self):
        original_settings = app.model_settings
        original_key = app.model_api_key
        original_request = app.model_json_request
        app.model_settings = lambda: {"enabled": True, "model": "fixture-model", "temperature": 0.2}
        app.model_api_key = lambda: "fixture-key"
        app.model_json_request = lambda *_args, **_kwargs: {
            "synonyms": ["term one", "term two", "term three"],
            "adjacent_terms": ["term four"],
            "queries": ["term five"],
        }
        try:
            focused = app.agent_discovery_plan("multimodal reasoning", ["arxiv"], {"search_depth": "focused"})
            deep = app.agent_discovery_plan("multimodal reasoning", ["arxiv"], {"search_depth": "deep"})
        finally:
            app.model_settings = original_settings
            app.model_api_key = original_key
            app.model_json_request = original_request
        self.assertEqual(focused["ai_expansion"]["added"], 2)
        self.assertEqual(deep["ai_expansion"]["added"], 5)

    def test_parse_model_json_accepts_preamble_and_trailing_fence(self):
        self.assertEqual(
            app.parse_model_json("以下是规划结果：\n```json\n{\"synonyms\": [\"multimodal\"]}\n```\n"),
            {"synonyms": ["multimodal"]},
        )

    def test_complete_json_is_accepted_when_provider_marks_length(self):
        result = {
            "choices": [{
                "message": {"content": '{"queries":["food volume estimation"]}'},
                "finish_reason": "length",
            }]
        }
        self.assertEqual(app._parse_model_json_result(result), {"queries": ["food volume estimation"]})

    def test_completed_stream_snapshot_wins_over_partial_delta(self):
        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"{\\"queries\\":["}'
                yield b'data: {"type":"response.incomplete","response":{"incomplete_details":{"reason":"max_output_tokens"}}}'
                completed = {"type": "response.completed", "response": {"output_text": '{"queries":["food volume estimation"]}'}}
                yield ("data: " + json.dumps(completed, ensure_ascii=False) + "\n\n").encode()

        response = app.read_model_response(Response())
        self.assertEqual(app._parse_model_json_result(response), {"queries": ["food volume estimation"]})

    def test_truncated_json_retries_with_increasing_budgets(self):
        original = app._model_json_request_once
        attempts = []

        def fake_once(_prompt, _payload, _timeout, max_output_tokens):
            attempts.append(max_output_tokens)
            if len(attempts) < 3:
                raise ValueError("模型输出因达到输出上限而被截断")
            return {"queries": ["food volume estimation"]}

        app._model_json_request_once = fake_once
        try:
            result = app._model_json_request_unbounded("return JSON", {}, max_output_tokens=1200)
        finally:
            app._model_json_request_once = original
        self.assertEqual(result["queries"], ["food volume estimation"])
        self.assertEqual(attempts, [1200, 2400, 3600])

    def test_agent_discovery_plan_parses_fenced_stream_from_model_provider(self):
        original = {
            "model_settings": app.model_settings,
            "model_api_key": app.model_api_key,
            "urlopen": app.urlopen,
        }
        captured = {}
        app.model_settings = lambda: {
            "enabled": True,
            "model": "fixture-model",
            "protocol": "chat_completions",
            "api_url": "https://model.example/v1/chat/completions",
            "base_url": "https://model.example/v1",
            "temperature": 0.2,
        }
        app.model_api_key = lambda: "fixture-key"

        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def __iter__(self):
                content = (
                    "规划结果如下：\n```json\n"
                    '{"synonyms":["vision-language model"],'
                    '"adjacent_terms":["cross-modal retrieval"],'
                    '"queries":["multimodal reasoning benchmark"]}\n```'
                )
                event = {"choices": [{"delta": {"content": content}, "finish_reason": None}]}
                yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response()

        app.urlopen = fake_open
        try:
            plan = app.agent_discovery_plan("多模态推理", ["arxiv"], {"discovery_mode": "explore"})
        finally:
            app.model_settings = original["model_settings"]
            app.model_api_key = original["model_api_key"]
            app.urlopen = original["urlopen"]
        self.assertEqual(captured["url"], "https://model.example/v1/chat/completions")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["body"]["max_tokens"], 1200)
        self.assertEqual(captured["timeout"], 60)
        self.assertTrue(plan["ai_expansion"]["enabled"])
        # Balanced/explore discovery is intentionally capped at two AI
        # variants to keep the multimodal/search path responsive. Deep mode
        # is covered separately by the bounds test above and may add five.
        self.assertGreaterEqual(plan["ai_expansion"]["added"], 2)

    def test_reading_segments_are_stable_and_page_anchored(self):
        paper_id = "segment-paper"
        attachment_id = "segment-attachment"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO papers(id,title,title_key,created_at,updated_at) VALUES(?,?,?,?,?)",
                (paper_id, "Segment paper", "segment paper", now, now),
            )
            db.execute(
                "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
                (attachment_id, paper_id, "paper.pdf", "segment.pdf", "s" * 64, 1, now),
            )
            db.execute(
                "INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (attachment_id, paper_id, 2, "First paragraph." + chr(10) + chr(10) + "Second paragraph with x.", 38, "good", "fixture", "p" * 64, now),
            )
        first = app.reading_segments(paper_id)
        second = app.reading_segments(paper_id)
        self.assertEqual([item["segment_key"] for item in first["segments"]], [item["segment_key"] for item in second["segments"]])
        self.assertEqual([item["page"] for item in first["segments"]], [2, 2])
        self.assertEqual(first["segments"][0]["annotation"], {})

    def test_paragraph_analysis_persists_and_reuses_annotation(self):
        paper_id = "annotation-paper"
        attachment_id = "annotation-attachment"
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO papers(id,title,title_key,created_at,updated_at) VALUES(?,?,?,?,?)",
                (paper_id, "Annotation paper", "annotation paper", now, now),
            )
            db.execute(
                "INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)",
                (attachment_id, paper_id, "paper.pdf", "annotation.pdf", "a" * 64, 1, now),
            )
            db.execute(
                "INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (attachment_id, paper_id, 1, "A paragraph about the method.", 31, "good", "fixture", "q" * 64, now),
            )
        original = app.limited_text_model_request
        calls = []
        app.limited_text_model_request = lambda *args, **kwargs: calls.append(1) or {"explanation": "解释", "role": "方法作用", "logic": "前提到结论", "terms": [], "formula": [], "evidence": "仅凭本段", "questions": []}
        try:
            segment = app.reading_segments(paper_id)["segments"][0]
            payload = {"segment_key": segment["segment_key"], "text": segment["text"]}
            result = app.analyze_paper_segment(paper_id, payload)
            reused = app.analyze_paper_segment(paper_id, payload)
        finally:
            app.limited_text_model_request = original
        self.assertFalse(result["reused"])
        self.assertTrue(reused["reused"])
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()
