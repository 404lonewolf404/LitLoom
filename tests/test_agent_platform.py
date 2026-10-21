import json
import json
import os
import tempfile
import time
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ["LITLOOM_DATABASE_URL"] = ""
import app


class AgentPlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        app.DATA_DIR = root / "data"
        app.FILES_DIR = app.DATA_DIR / "files"
        app.BACKUP_DIR = app.DATA_DIR / "backups"
        app.DB_PATH = app.DATA_DIR / "literature.sqlite3"
        app.RUNTIME_MODEL_KEY = ""
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def test_model_search_backfills_unselected_fresh_candidates_before_repeats(self):
        inventory = {
            "C1": {"result_id": "C1", "title": "Multimodal reasoning paper one", "abstract": "multimodal reasoning", "citation_count": 3, "_previously_seen": False, "url": "https://example.test/1"},
            "C2": {"result_id": "C2", "title": "Multimodal reasoning paper two", "abstract": "multimodal reasoning", "citation_count": 8, "_previously_seen": False, "url": "https://example.test/2"},
            "C3": {"result_id": "C3", "title": "Multimodal reasoning paper three", "abstract": "multimodal reasoning", "citation_count": 1, "_previously_seen": False, "url": "https://example.test/3"},
            "R1": {"result_id": "R1", "title": "Historical multimodal reasoning paper", "abstract": "multimodal reasoning", "citation_count": 20, "_previously_seen": True, "url": "https://example.test/r1"},
        }
        selected, added = app._model_search_backfill_selection(
            [dict(inventory["C1"])], inventory, {"C1"}, "multimodal reasoning", 4,
            {"avoid_repeats": True, "suppress_seen": False},
        )
        self.assertEqual(added, 3)
        self.assertEqual([item["result_id"] for item in selected], ["C1", "C2", "C3", "R1"])
        self.assertTrue(all(item["relevance_label"] in {"adjacent", "uncertain"} for item in selected[1:]))
        self.assertTrue(all("补位" in item["relevance_reason"] for item in selected[1:]))

    def test_model_search_backfill_strict_fresh_mode_skips_history(self):
        inventory = {
            "C1": {"result_id": "C1", "title": "Multimodal reasoning paper one", "abstract": "multimodal reasoning", "url": "https://example.test/1", "_previously_seen": False},
            "R1": {"result_id": "R1", "title": "Historical multimodal reasoning paper", "abstract": "multimodal reasoning", "url": "https://example.test/r1", "_previously_seen": True},
        }
        selected, added = app._model_search_backfill_selection(
            [], inventory, set(), "multimodal reasoning", 3,
            {"avoid_repeats": True, "suppress_seen": True},
        )
        self.assertEqual(added, 1)
        self.assertEqual([item["result_id"] for item in selected], ["C1"])

    def test_model_search_backfill_fills_history_when_browser_requests_repeat_fill(self):
        inventory = {
            "C1": {"result_id": "C1", "title": "Multimodal reasoning paper one", "abstract": "multimodal reasoning", "url": "https://example.test/1", "_previously_seen": False},
            "R1": {"result_id": "R1", "title": "Historical multimodal reasoning paper", "abstract": "multimodal reasoning", "url": "https://example.test/r1", "_previously_seen": True},
        }
        selected, added = app._model_search_backfill_selection(
            [], inventory, set(), "multimodal reasoning", 2,
            {"avoid_repeats": True, "suppress_seen": True, "fill_repeats": True},
        )
        self.assertEqual(added, 2)
        self.assertEqual([item["result_id"] for item in selected], ["C1", "R1"])
        self.assertTrue("历史候选" in selected[1]["relevance_reason"])

    def test_model_search_local_fill_keeps_requested_page_from_real_connectors(self):
        original_search = app.SOURCE_SEARCH["arxiv"]
        original_plan = app.search_query_plan

        def fake_search(query, _limit, offset=0):
            if offset:
                return []
            prefix = "A" if "angle-a" in query else "B"
            return [{
                "title": f"Food volume paper {prefix}{index}",
                "abstract": "Food volume estimation from images using deep learning.",
                "year": 2026,
                "arxiv_id": f"2601.00{prefix}{index:02d}",
                "url": f"https://arxiv.org/abs/2601.00{prefix}{index:02d}",
            } for index in range(1, 3)]

        app.SOURCE_SEARCH["arxiv"] = fake_search
        app.search_query_plan = lambda *_args, **_kwargs: {"query_variants": [
            {"query": "angle-a food volume"}, {"query": "angle-b food volume"},
        ]}
        try:
            selected, added, local_count = app._model_search_local_fill(
                [{"result_id": "C0", "title": "Already ranked paper", "url": "https://example.test/c0"}],
                {}, {}, {"C0"}, "food volume estimation", 4,
                {"avoid_repeats": True, "suppress_seen": False},
                ["arxiv"], set(), set(), [],
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original_search
            app.search_query_plan = original_plan
        self.assertEqual(len(selected), 4)
        self.assertEqual(added, 3)
        self.assertEqual(local_count, 3)
        self.assertTrue(all(item.get("_selection_backfilled") for item in selected[1:]))

    def test_model_search_local_fill_uses_sources_selected_by_ai_when_preference_is_empty(self):
        """AI-only searches still refill from the connectors chosen by tool calls."""
        original_search = app.SOURCE_SEARCH["arxiv"]
        original_plan = app.search_query_plan
        calls = []

        def fake_search(query, _limit, offset=0):
            calls.append((query, offset))
            if offset:
                return []
            return [{
                "title": f"AI selected source refill {index}",
                "abstract": "Food volume estimation from meal images using deep learning.",
                "year": 2026,
                "arxiv_id": f"2601.010{index}",
                "url": f"https://arxiv.org/abs/2601.010{index}",
            } for index in range(1, 4)]

        app.SOURCE_SEARCH["arxiv"] = fake_search
        app.search_query_plan = lambda *_args, **_kwargs: {"query_variants": [
            {"query": "new angle food volume"},
        ]}
        executed_searches = [{
            "query": "already searched angle",
            "sources": [{"source": "arxiv", "status": "ok"}],
        }]
        try:
            selected, added, local_count = app._model_search_local_fill(
                [{"result_id": "C0", "title": "Already ranked paper", "url": "https://example.test/c0"}],
                {}, {}, {"C0"}, "food volume estimation", 4,
                {"avoid_repeats": True, "suppress_seen": False},
                [], set(), set(), executed_searches,
            )
        finally:
            app.SOURCE_SEARCH["arxiv"] = original_search
            app.search_query_plan = original_plan
        self.assertEqual(len(selected), 4)
        self.assertEqual(added, 3)
        self.assertEqual(local_count, 3)
        self.assertTrue(calls)
        self.assertTrue(all(query == "new angle food volume" and offset == 0 for query, offset in calls))

    def test_model_web_quality_gate_drops_method_only_repeat_candidates(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        direct = {
            "title": "Image-Based Food Volume Estimation Using Monocular Depth Prediction",
            "abstract": "",
            "authors": "",
            "model_relevance_label": "direct",
        }
        method_only = {
            "title": "Attention Augmented Convolutional Neural Network for Machine State Estimation",
            "abstract": "",
            "authors": "",
            "model_relevance_label": "adjacent",
        }
        for item in (direct, method_only):
            triage = app.score_relevance(item, query, [], [])
            item["relevance_score"] = triage["score"]
            item["relevance_reasons"] = triage["reasons"]
        kept, filtered = app.filter_model_web_quality(
            [direct, method_only], query,
            {"search_backend": "model_web", "discovery_mode": "balanced"},
        )
        self.assertEqual(filtered, 1)
        self.assertEqual([item["title"] for item in kept], [direct["title"]])

    def test_model_web_quality_gate_allows_explicit_explore_mode(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        candidate = {"title": "Attention Network for Machine State Estimation", "relevance_score": 20}
        kept, filtered = app.filter_model_web_quality(
            [candidate], query,
            {"search_backend": "model_web", "discovery_mode": "explore"},
        )
        self.assertEqual(filtered, 0)
        self.assertEqual(kept, [candidate])

    def test_model_web_quality_gate_rejects_food_security_cross_domain_false_positive(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        biomass = {
            "title": "A Point Cloud Transformer-Based Regression Approach for Estimating Above-Ground Biomass",
            "abstract": "From the perspective of global food security, deep learning estimates crop biomass from 3D point clouds.",
            "model_relevance_label": "adjacent",
        }
        genuine = {
            "title": "Food Volume Estimation from Images Using Attention",
            "abstract": "We estimate food volume and portion size from meal images for dietary assessment.",
            "model_relevance_label": "adjacent",
        }
        for item in (biomass, genuine):
            triage = app.score_relevance(item, query, [], [])
            item["relevance_score"] = triage["score"]
            item["relevance_reasons"] = triage["reasons"]
        kept, filtered = app.filter_model_web_quality([biomass, genuine], query, {"search_backend": "model_web", "discovery_mode": "balanced"})
        self.assertEqual(filtered, 1)
        self.assertEqual([item["title"] for item in kept], [genuine["title"]])

    def test_model_web_quality_gate_keeps_real_backfill_as_uncertain(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        backfill = {
            "title": "Food Portion Volume Estimation from Meal Images with Attention",
            "abstract": "We estimate food volume and portion size from meal images using attention.",
            "model_relevance_label": "uncertain",
            "selection_backfilled": True,
            "relevance_score": 18,
            "relevance_reasons": ["命中对象：food", "命中任务：volume estimation"],
        }
        kept, filtered = app.filter_model_web_quality(
            [backfill], query,
            {"search_backend": "model_web", "discovery_mode": "balanced"},
        )
        self.assertEqual(filtered, 0)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["model_relevance_label"], "uncertain")
        self.assertTrue(any("补位" in reason for reason in kept[0]["relevance_reasons"]))

    def test_model_web_quality_gate_keeps_previously_accepted_history(self):
        """A repeat search must not discard an accepted history fill."""
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        history = {
            "title": "Food Portion Measurement with a Visual Attention Model",
            # This older record intentionally has sparse metadata.  It was
            # accepted in the earlier run and should remain available as a
            # transparent history fallback instead of shrinking the page.
            "abstract": "",
            "model_relevance_label": "uncertain",
            "history_quality_accepted": True,
            "selection_backfilled": True,
            "relevance_score": 0,
            "relevance_reasons": [],
        }
        kept, filtered = app.filter_model_web_quality(
            [history], query,
            {"search_backend": "model_web", "discovery_mode": "balanced"},
        )
        self.assertEqual(filtered, 0)
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["history_quality_accepted"])
        self.assertTrue(any("历史候选" in reason for reason in kept[0]["relevance_reasons"]))

    def test_model_search_history_fill_reuses_accepted_rows_to_reach_requested_limit(self):
        """Durable same-intent candidates fill a repeated model search page."""
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        options = {"search_backend": "model_web", "avoid_repeats": True, "suppress_seen": False}
        intent_key = app.discovery_intent_key(query, options)
        run_id = "history-fill-source-run"
        started = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)",
                (run_id, query, "success", started, '["model_web"]'),
            )
            for index in range(1, 4):
                record = {
                    "id": f"candidate:history-{index}",
                    "title": f"Food Volume Estimation History {index}",
                    "abstract": "Food volume estimation from meal images using deep learning.",
                    "url": f"https://example.test/history-{index}",
                    "source": "model_web",
                    "external_id": f"history-{index}",
                    "intent_key": intent_key,
                    "candidate_key": f"title:food-volume-estimation-history-{index}",
                    "history_quality_accepted": True,
                    "model_relevance_label": "adjacent",
                    "relevance_score": 55,
                    "relevance_reasons": ["命中对象：food", "命中任务：volume estimation"],
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], run_id, record["candidate_key"], json.dumps(record, ensure_ascii=False), started),
                )
        existing = [{
            "title": "Food Volume Estimation New Result",
            "abstract": "We estimate food volume and portion size from meal images using deep learning.",
            "url": "https://example.test/new",
            "external_id": "new",
            "model_relevance_label": "direct",
            "relevance_score": 60,
            "relevance_reasons": ["命中对象：food", "命中任务：volume estimation"],
        }]
        filled = app._model_search_history_fill(intent_key, query, existing, 4, options)
        self.assertEqual(len(filled), 3)
        self.assertTrue(all(item.get("_history_fill") for item in filled))
        self.assertTrue(all(item.get("history_quality_accepted") for item in filled))
        kept, filtered = app.filter_model_web_quality(
            [*existing, *filled], query,
            {"search_backend": "model_web", "discovery_mode": "balanced"},
        )
        self.assertEqual(filtered, 0)
        self.assertEqual(len(kept), 4)

    def test_model_search_history_fill_counts_unique_existing_works(self):
        """Connector aliases must not consume the requested unique page size."""
        query = "food volume estimation with attention"
        options = {"search_backend": "connectors", "avoid_repeats": True, "suppress_seen": False}
        intent_key = app.discovery_intent_key(query, options)
        run_id = "history-fill-duplicate-existing"
        started = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)",
                (run_id, query, "success", started, '["arxiv"]'),
            )
            for index in range(1, 4):
                record = {
                    "id": f"candidate:duplicate-history-{index}",
                    "title": f"Food Volume History Candidate {index}",
                    "abstract": "Food volume estimation from meal images.",
                    "url": f"https://example.test/duplicate-history-{index}",
                    "source": "arxiv",
                    "external_id": f"duplicate-history-{index}",
                    "intent_key": intent_key,
                    "candidate_key": f"external:duplicate-history-{index}",
                    "history_quality_accepted": True,
                    "model_relevance_label": "direct",
                }
                db.execute(
                    "INSERT INTO discovery_candidates(id,run_id,candidate_key,record_json,created_at) VALUES(?,?,?,?,?)",
                    (record["id"], run_id, record["candidate_key"], json.dumps(record, ensure_ascii=False), started),
                )
        duplicate_rows = [{
            "title": "Current connector result",
            "abstract": "Food volume estimation from meal images.",
            "url": "https://example.test/current",
            "external_id": "current",
        }] * 4
        filled = app._model_search_history_fill(intent_key, query, duplicate_rows, 4, options)
        self.assertEqual(len(filled), 3)
        self.assertEqual({item["external_id"] for item in filled}, {
            "duplicate-history-1", "duplicate-history-2", "duplicate-history-3",
        })

    def test_model_search_history_fill_reads_persisted_topic_runs(self):
        """Auto-imported topic runs remain available as repeat-page fill."""
        query = "food volume estimation with attention"
        options = {"search_backend": "model_web", "avoid_repeats": True, "suppress_seen": False}
        intent_key = app.discovery_intent_key(query, options)
        paper_id, _ = app.upsert_paper({
            "title": "Food Volume Estimation with Attention",
            "abstract": "Food volume estimation from meal images using deep learning.",
            "url": "https://example.test/persisted-history",
            "year": 2026,
            "arxiv_id": "2601.12345",
        }, source="arxiv", external_id="2601.12345")
        run_id = "persisted-history-source-run"
        started = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)",
                (run_id, query, "success", started, '["arxiv"]'),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, query, "{}", json.dumps({"intent_key": intent_key}), started),
            )
            db.execute(
                "INSERT INTO run_papers(run_id,paper_id,search_rank,metadata_json) VALUES(?,?,?,?)",
                (run_id, paper_id, 1, json.dumps({"history_quality_accepted": True, "model_relevance_label": "direct"})),
            )
        filled = app._model_search_history_fill(intent_key, query, [], 1, options)
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["title"], "Food Volume Estimation with Attention")
        self.assertTrue(filled[0]["history_quality_accepted"])

    def test_model_web_quality_gate_removes_rejected_candidates_from_review_queue(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        payload = {
            "results": [{
                "title": "Attention Augmented Convolutional Neural Network for Machine State Estimation",
                "year": 2026,
                "abstract": "",
                "doi": "10.1000/unrelated-machine-state",
                "url": "https://doi.org/10.1000/unrelated-machine-state",
                "provider_sources": [{"source": "crossref", "external_id": "10.1000/unrelated-machine-state"}],
                "model_relevance_label": "adjacent",
            }],
            "searches": [], "gaps": [], "next_queries": [], "tool_calls": 1, "attempts": 1,
        }
        with patch.object(app, "model_web_search", return_value=payload):
            result = app.run_query(
                query, [], limit=5,
                options={"search_backend": "model_web", "ai_screen": False, "avoid_repeats": True},
                request_id=str(app.uuid.uuid4()),
            )
        self.assertEqual(result["papers"], [])
        self.assertEqual(result["summary"]["model_web_quality_filtered"], 1)

    def test_agent_quality_gate_removes_cross_domain_connector_hits(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        papers = []
        for title, abstract in (
            ("Food Volume Estimation from Meal Images", "We estimate food volume and portion size from meal images."),
            ("Attention CNN for Poultry Disease Diagnosis", "A medical imaging model detects poultry disease with attention."),
            ("Deep Learning for Financial Risk Estimation", "We estimate market risk using convolution and attention."),
        ):
            item = {"id": title, "title": title, "abstract": abstract}
            triage = app.score_relevance(item, query, [], [])
            item["relevance_score"] = triage["score"]
            item["relevance_reasons"] = triage["reasons"]
            papers.append(item)
        kept, summary = app.filter_agent_candidate_quality(papers, query)
        self.assertEqual([item["title"] for item in kept], ["Food Volume Estimation from Meal Images"])
        self.assertEqual(summary["filtered_count"], 2)
        self.assertEqual(summary["unrelated"], 2)
        self.assertEqual(kept[0]["agent_relevance_class"], "direct")

    def test_agent_quality_gate_returns_structured_gap_when_all_candidates_are_unrelated(self):
        query = "用深度学习模型卷积注意力等机制进行食物体积的估计"
        kept, summary = app.filter_agent_candidate_quality([
            {"id": "x", "title": "Attention Network for Financial Forecasting", "abstract": "Market risk estimation."},
        ], query)
        self.assertEqual(kept, [])
        self.assertEqual(summary["filtered_count"], 1)
        self.assertTrue(summary["gaps"])

    def test_agent_search_sources_exposes_quality_summary_without_auto_import(self):
        original = app.run_query
        try:
            app.run_query = lambda *args, **kwargs: {
                "id": "agent-quality-run", "status": "success", "summary": {}, "papers": [
                    {"id": "good", "title": "Food Volume Estimation from Meal Images", "abstract": "food volume estimation from meal images", "relevance_score": 70},
                    {"id": "bad", "title": "Attention Network for Financial Forecasting", "abstract": "market risk estimation", "relevance_score": 20},
                ],
            }
            goal = {"objective": "food volume", "core_question": "用深度学习模型卷积注意力等机制进行食物体积的估计", "sources": ["arxiv"]}
            result = app.call_agent_tool("search_sources", {"goal": goal, "limit": 10})
        finally:
            app.run_query = original
        self.assertEqual([item["id"] for item in result["papers"]], ["good"])
        self.assertEqual(result["summary"]["agent_quality"]["filtered_count"], 1)
        self.assertTrue(result["candidate_mode"])

    def test_auto_workflow_selection_routes_by_research_intent_and_explains_choice(self):
        frontier = app.create_research_goal({
            "objective": "持续追踪多模态推理前沿的新论文，并区分新增工作和重复结果",
            "workflow": "auto",
            "sources": ["arxiv"],
        })
        self.assertEqual(frontier["workflow"], "follow_up")
        self.assertEqual(frontier["scope"]["workflow_selection"]["mode"], "auto")
        self.assertIn("前沿", frontier["scope"]["workflow_selection"]["signals"])
        self.assertIn("长期研究记忆", frontier["scope"]["workflow_selection"]["reason"])
        self.assertEqual(app.build_agent_plan(frontier)["workflow_selection"], frontier["scope"]["workflow_selection"])

        evidence = app.create_research_goal({
            "objective": "逐篇精读本地 PDF 全文，核验实验结果和页码证据",
            "workflow": "auto",
            "sources": ["arxiv"],
        })
        self.assertEqual(evidence["workflow"], "evidence_review")
        self.assertIn("本地 PDF", evidence["scope"]["workflow_selection"]["reason"])

        broad = app.create_research_goal({
            "objective": "梳理食物体积估计的深度学习方法和常见数据集",
            "workflow": "auto",
            "sources": ["arxiv"],
        })
        self.assertEqual(broad["workflow"], "research_map")

    def test_auto_frontier_schedule_has_a_future_due_time_and_stays_bounded(self):
        goal = app.create_research_goal({
            "objective": "长期关注多模态推理前沿和新增论文",
            "workflow": "auto",
            "auto_run": True,
            "cadence_hours": 168,
            "sources": ["arxiv"],
        })
        self.assertEqual(goal["workflow"], "follow_up")
        self.assertEqual(goal["auto_run"], 1)
        self.assertEqual(goal["cadence_hours"], 168)
        self.assertTrue(goal["next_run_at"])
        self.assertIn("定期自动运行", goal["scope"]["workflow_selection"]["signals"])
        with self.assertRaisesRegex(ValueError, "仅支持前沿追踪"):
            app.create_research_goal({
                "objective": "定期自动精读文献并核验全文证据",
                "workflow": "evidence_review",
                "auto_run": True,
                "sources": ["arxiv"],
            })

    def test_startup_marks_queued_search_as_interrupted(self):
        run_id = "queued-search-before-restart"
        started = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)",
                (run_id, "food volume estimation", "queued", started, '["arxiv"]'),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,run_id,status,stage,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (run_id, run_id, "queued", "queued", 0, started, started),
            )

        app.init_db()

        with app.DB_LOCK, app.db_session() as db:
            run = db.execute("SELECT status,completed_at,error FROM runs WHERE id=?", (run_id,)).fetchone()
            job = db.execute("SELECT status,stage,last_error FROM sync_jobs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(run["status"], "interrupted")
        self.assertTrue(run["completed_at"])
        self.assertEqual(run["error"], "服务重启时任务尚未开始")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["stage"], "interrupted")
        self.assertEqual(job["last_error"], "服务重启时任务尚未开始")

    def test_startup_requeues_interrupted_search_with_saved_plan_without_duplicate_worker(self):
        """A restart resumes the same durable run and restores its search contract."""
        run_id = "search-recovery-with-plan"
        historical_id = "historical-interrupted-search"
        started = app.now_iso()
        plan = {
            "original_query": "食物体积估计的深度学习方法",
            "query": "食物体积估计的深度学习方法",
            "query_variants": [{"id": "primary", "query": "food volume estimation deep learning", "lane": "primary"}],
            "search_options": {"expand_concepts": False, "avoid_repeats": True, "suppress_seen": False},
            "result_limit": 7,
        }
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json) VALUES(?,?,?,?,?)",
                (run_id, "食物体积估计的深度学习方法", "queued", started, '["arxiv","openalex"]'),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,query_id,run_id,status,stage,attempts,created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, "saved-topic", run_id, "queued", "queued", 0, started, started, ""),
            )
            db.execute(
                "INSERT INTO run_snapshots(run_id,query,plan_json,summary_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, "食物体积估计的深度学习方法", json.dumps(plan, ensure_ascii=False), "{}", started),
            )
            db.execute(
                "INSERT INTO runs(id,query_name,status,started_at,sources_json,error) VALUES(?,?,?,?,?,?)",
                (historical_id, "旧的中断搜索", "interrupted", started, '["arxiv"]', "用户此前停止"),
            )
            db.execute(
                "INSERT INTO sync_jobs(id,run_id,status,stage,attempts,created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?)",
                (historical_id, historical_id, "interrupted", "interrupted", 1, started, started, "用户此前停止"),
            )

        app.init_db()
        captured = []
        with patch.object(app, "launch_query_job", side_effect=lambda *args, **kwargs: captured.append((args, kwargs)) or {"id": run_id, "status": "queued"}):
            resumed = app.resume_interrupted_sync_jobs()
            resumed_again = app.resume_interrupted_sync_jobs()

        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["resumed_run_id"], run_id)
        self.assertEqual(resumed_again, [])
        self.assertEqual(len(captured), 1)
        args, kwargs = captured[0]
        self.assertEqual(args[:3], ("食物体积估计的深度学习方法", ["arxiv", "openalex"], 7))
        self.assertEqual(kwargs["request_id"], run_id)
        self.assertEqual(kwargs["plan_override"]["query_variants"][0]["query"], "food volume estimation deep learning")
        self.assertFalse(kwargs["options"]["suppress_seen"])
        # A historical interruption that was not created by this startup is
        # deliberately left alone and needs an explicit user retry.
        with app.DB_LOCK, app.db_session() as db:
            historical = db.execute("SELECT status FROM sync_jobs WHERE id=?", (historical_id,)).fetchone()
        self.assertEqual(historical["status"], "interrupted")

    def test_startup_requeues_ai_job_that_was_queued_before_restart(self):
        paper_id, _ = app.upsert_paper({"title": "Queued AI recovery fixture", "year": 2026, "doi": "10.1000/queued-ai-recovery"}, source="manual")
        job_id = "queued-ai-before-restart"
        started = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, paper_id, "reading_card", "queued", "queued", "已加入任务队列", json.dumps({"template": "fulltext_reading_card", "language": "zh-CN"}), started, started),
            )

        # init_db is the same startup reconciliation used by app.main().
        app.init_db()
        with app.DB_LOCK, app.db_session() as db:
            row = db.execute("SELECT status,error FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(tuple(row), ("interrupted", "服务重启时任务尚未开始"))

        with patch.object(app, "submit_ai_job", return_value={"job": {"id": "resumed-ai-job"}, "deduplicated": False}) as submit:
            resumed = app.resume_interrupted_ai_jobs()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["interrupted_job_id"], job_id)
        self.assertEqual(resumed[0]["resumed_job_id"], "resumed-ai-job")
        submit.assert_called_once()

    def test_plan_persists_executable_step_contracts(self):
        goal = app.create_research_goal({"objective": "Study multimodal reasoning evidence", "core_question": "multimodal reasoning", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        self.assertEqual(run["plan"]["version"], "agent-plan-v2")
        step = next(item for item in run["steps"] if item["step_key"] == "critique_claims")
        self.assertIn("input_schema", step)
        self.assertIn("output_schema", step)
        self.assertIn("completion", step)
        self.assertIn("failure_policy", step)
        self.assertIn("budget", step)
        self.assertEqual(step["agent_type"], "CriticAgent")
        self.assertEqual(
            [item["step_key"] for item in run["steps"][-3:]],
            ["synthesize_map", "check_citation_completeness", "evaluate_stop_conditions"],
        )
        self.assertIn("compare_candidates", [item["step_key"] for item in run["steps"]])
        self.assertIn("reading_route", [item["step_key"] for item in run["steps"]])
        self.assertIn("paper_analysis_overview", [item["step_key"] for item in run["steps"]])
        self.assertIn("search_local_context", [item["step_key"] for item in run["steps"]])
        self.assertIn("resolve_evidence_gaps", [item["step_key"] for item in run["steps"]])
        overview_step = next(item for item in run["steps"] if item["step_key"] == "paper_analysis_overview")
        synthesis_step = next(item for item in run["steps"] if item["step_key"] == "synthesize_map")
        self.assertIn("paper_analysis_overview", synthesis_step["dependencies"])
        evidence_goal = app.create_research_goal({"objective": "Extract evidence from full text", "core_question": "page evidence", "workflow": "evidence_review", "sources": ["arxiv"]})
        evidence_run = app.create_agent_run(evidence_goal["id"])
        self.assertIn("ocr_pages", [item["step_key"] for item in evidence_run["steps"]])

    def test_plan_has_explicit_identity_citation_and_follow_up_nodes(self):
        goal = app.create_research_goal({"objective": "Track multimodal reasoning frontier", "core_question": "multimodal reasoning", "workflow": "follow_up", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        keys = [item["step_key"] for item in run["steps"]]
        self.assertLess(keys.index("search_candidates"), keys.index("resolve_identity"))
        self.assertLess(keys.index("resolve_identity"), keys.index("merge_versions"))
        self.assertLess(keys.index("merge_versions"), keys.index("triage_candidates"))
        self.assertLess(keys.index("expand_search"), keys.index("expand_citations"))
        self.assertEqual(next(item for item in run["steps"] if item["step_key"] == "follow_up_map")["tool_name"], "track_follow_up")
        self.assertEqual(next(item for item in run["steps"] if item["step_key"] == "follow_up_map")["agent_type"], "FollowUpAgent")

    def test_identity_resolution_is_batch_read_only_and_marks_match_type(self):
        paper_id, _ = app.upsert_paper({"title": "Identity fixture", "year": 2026, "doi": "10.1000/identity"}, source="manual")
        result = app.call_agent_tool("resolve_paper_identity", {"papers": [
            {"id": "candidate:known", "title": "Identity fixture", "year": 2026, "doi": "10.1000/identity"},
            {"id": "candidate:new", "title": "Unseen work", "year": 2026, "arxiv_id": "2601.98765v1"},
        ]})
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["unresolved_count"], 1)
        by_title = {item["title"]: item for item in result["papers"]}
        self.assertEqual(by_title["Identity fixture"]["identity_status"], "doi")
        self.assertEqual(by_title["Identity fixture"]["local_paper_id"], paper_id)
        self.assertEqual(by_title["Unseen work"]["identity_status"], "unresolved")

    def test_agent_citation_expansion_returns_snapshot_without_importing_candidates(self):
        paper_id, _ = app.upsert_paper({"title": "Citation seed", "year": 2026, "doi": "10.1000/seed"}, source="manual")
        original = app.expand_openalex_relations
        try:
            app.expand_openalex_relations = lambda seed, limit, persist=True: {"paper_id": seed, "candidates": [{"title": "Related candidate", "year": 2026, "doi": "10.1000/related", "relation": "被引用"}], "count": 1}
            result = app.call_agent_tool("expand_citations", {"goal": {"id": "goal-missing"}, "papers": [{"local_paper_id": paper_id}], "run_id": "run-citation", "limit": 10})
        finally:
            app.expand_openalex_relations = original
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["papers"]), 1)
        self.assertTrue(result["papers"][0]["id"].startswith("candidate:citation:"))
        with app.db_session() as db:
            self.assertIsNone(db.execute("SELECT id FROM papers WHERE doi=?", ("10.1000/related",)).fetchone())

    def test_agent_citation_expansion_deduplicates_local_and_doi_aliases(self):
        """A local ID and a candidate DOI for one work must make one request."""
        paper_id, _ = app.upsert_paper({"title": "Citation alias seed", "year": 2026, "doi": "10.1000/alias-seed"}, source="manual")
        original = app.expand_openalex_relations
        calls = []

        def fake_expand(seed, limit, persist=True, **_kwargs):
            calls.append(seed)
            return {"paper_id": seed, "candidates": [], "count": 0}

        app.expand_openalex_relations = fake_expand
        try:
            result = app.call_agent_tool("expand_citations", {
                "papers": [
                    {"local_paper_id": paper_id, "doi": "10.1000/alias-seed", "title": "Citation alias seed"},
                    {"id": "candidate:alias", "doi": "10.1000/alias-seed", "title": "Citation alias seed"},
                ],
                "limit": 6,
            })
        finally:
            app.expand_openalex_relations = original
        self.assertEqual(result["seed_count"], 1)
        self.assertEqual(calls, [paper_id])

    def test_agent_citation_expansion_accepts_external_openalex_seed_without_local_import(self):
        original = app._expand_openalex_relation_work
        try:
            app._expand_openalex_relation_work = lambda url, limit, persist=False, paper_id="": {
                "candidates": [{"title": "External seed relation", "year": 2026, "openalex_id": "https://openalex.org/W2", "relation": "相似"}],
            }
            result = app.call_agent_tool("expand_citations", {"papers": [{"openalex_id": "https://openalex.org/W1", "title": "External seed"}], "limit": 6})
        finally:
            app._expand_openalex_relation_work = original
        self.assertEqual(result["seed_count"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["papers"][0]["id"].startswith("candidate:citation:"))

    def test_agent_citation_expansion_uses_goal_history_as_seed(self):
        goal = app.create_research_goal({"objective": "Track a citation neighborhood", "core_question": "citation graph", "sources": ["openalex"]})
        original = app._expand_openalex_relation_work
        captured = []
        try:
            def fake_expand(url, limit, persist=False, paper_id=""):
                captured.append(url)
                return {"candidates": [{"title": "Historical relation", "year": 2026, "doi": "10.1000/history-related", "relation": "被引用"}]}
            app._expand_openalex_relation_work = fake_expand
            with app.db_session() as db:
                app._record_goal_paper_history(db, goal["id"], "seed-run", [{"title": "Imported seed", "doi": "10.1000/historical-seed", "openalex_id": "https://openalex.org/W123"}])
            result = app.call_agent_tool("expand_citations", {"goal": goal, "papers": [], "run_id": "follow-up-run", "limit": 6})
        finally:
            app._expand_openalex_relation_work = original
        self.assertEqual(result["seed_count"], 1)
        self.assertEqual(len(captured), 1)
        self.assertIn("W123", captured[0])

    def test_agent_run_snapshot_exposes_persisted_progress(self):
        goal = app.create_research_goal({"objective": "Inspect run progress", "core_question": "progress", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        snapshot = app.agent_run_snapshot(run["id"])
        self.assertEqual(snapshot["progress"]["total_steps"], len(snapshot["steps"]))
        self.assertEqual(snapshot["progress"]["completed_steps"], 0)
        self.assertEqual(snapshot["progress"]["percent"], 0)

    def _replace_run_steps(self, run, specs, max_tool_calls=10):
        now = app.now_iso()
        budget = dict(run["budget"])
        budget["max_tool_calls"] = max_tool_calls
        with app.DB_LOCK, app.db_session() as db:
            db.execute("DELETE FROM agent_steps WHERE run_id=?", (run["id"],))
            db.execute("UPDATE agent_runs SET status='queued',budget_json=?,error='',completed_at='' WHERE id=?", (json.dumps(budget), run["id"]))
            for ordinal, spec in enumerate(specs):
                db.execute(
                    """INSERT INTO agent_steps(
                        id,run_id,step_key,agent_type,title,tool_name,tool_class,ordinal,
                        dependencies_json,completion_json,status,requires_confirmation,max_attempts,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        spec["id"], run["id"], spec["key"], "TestAgent", spec["key"],
                        spec.get("tool", "search_local_library"), spec.get("tool_class", "Read"), ordinal,
                        json.dumps(spec.get("dependencies", [])), json.dumps(spec.get("completion", {})),
                        spec.get("status", "queued"), int(spec.get("requires_confirmation", False)),
                        spec.get("max_attempts", 1), now, now,
                    ),
                )
        return app.agent_run_snapshot(run["id"])

    def test_agent_workflow_runs_independent_ready_steps_concurrently(self):
        goal = app.create_research_goal({"objective": "Verify independent workflow concurrency", "core_question": "parallel DAG", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        self._replace_run_steps(run, [{"id": "parallel-a", "key": "parallel_a"}, {"id": "parallel-b", "key": "parallel_b"}])
        active = 0
        peak = 0
        lock = __import__("threading").Lock()
        barrier = __import__("threading").Barrier(2)

        def fake_tool(_name, _payload):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=0.5)
                return {}
            finally:
                with lock:
                    active -= 1

        with patch.object(app, "call_agent_tool", side_effect=fake_tool), patch.object(app, "AGENT_STEP_CONCURRENCY", 2):
            app.run_agent_workflow(run["id"])

        result = app.agent_run_snapshot(run["id"])
        self.assertEqual(peak, 2)
        self.assertEqual(result["status"], "success")
        self.assertEqual([step["status"] for step in result["steps"]], ["success", "success"])

    def test_agent_workflow_waits_for_confirmation_instead_of_reporting_success(self):
        goal = app.create_research_goal({"objective": "Verify confirmation remains pending", "core_question": "approval state", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        self._replace_run_steps(run, [{"id": "approval-step", "key": "approval", "status": "awaiting_confirmation", "tool_class": "External", "requires_confirmation": True}])

        app.run_agent_workflow(run["id"])

        result = app.agent_run_snapshot(run["id"])
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["steps"][0]["status"], "awaiting_confirmation")

    def test_external_step_requires_consent_then_runs_after_approval(self):
        step = {"tool_class": "External", "requires_confirmation": True, "tool_name": "download_fulltext"}
        self.assertTrue(app._agent_step_needs_confirmation(step, {}, {"budget": {"consent_external": False}}, {}))
        self.assertFalse(app._agent_step_needs_confirmation(step, {}, {"budget": {"consent_external": True}}, {}))

    def test_agent_workflow_retries_failed_step_and_persists_attempts(self):
        goal = app.create_research_goal({"objective": "Verify bounded workflow retry", "core_question": "retry state", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        self._replace_run_steps(run, [{"id": "retry-step", "key": "retry", "max_attempts": 2}])
        calls = 0

        def flaky_tool(_name, _payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary provider failure")
            return {}

        with patch.object(app, "call_agent_tool", side_effect=flaky_tool):
            app.run_agent_workflow(run["id"])

        result = app.agent_run_snapshot(run["id"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["steps"][0]["status"], "success")
        self.assertEqual(result["steps"][0]["attempts"], 2)
        self.assertEqual(len(result["tool_calls"]), 2)

    def test_agent_workflow_stops_cleanly_when_tool_budget_is_exhausted(self):
        goal = app.create_research_goal({"objective": "Verify tool budget enforcement", "core_question": "budget state", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        self._replace_run_steps(run, [{"id": "budget-step", "key": "budget"}], max_tool_calls=1)
        now = app.now_iso()
        with app.DB_LOCK, app.db_session() as db:
            db.execute(
                "INSERT INTO agent_tool_calls(id,run_id,step_id,tool_name,tool_class,status,started_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                ("budget-call", run["id"], "budget-step", "search_local_library", "Read", "success", now, now),
            )

        with patch.object(app, "call_agent_tool") as call_tool:
            app.run_agent_workflow(run["id"])

        result = app.agent_run_snapshot(run["id"])
        call_tool.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["steps"][0]["status"], "skipped")

    def test_cancelling_terminal_agent_run_does_not_rewrite_its_status(self):
        goal = app.create_research_goal({"objective": "Inspect terminal cancellation", "core_question": "terminal state", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        app._agent_update_run(run["id"], status="success", completed_at=app.now_iso())
        result = app.cancel_agent_run(run["id"])
        self.assertEqual(result["status"], "success")

    def test_follow_up_reports_only_stable_history_buckets(self):
        result = app.call_agent_tool("track_follow_up", {"previous": {
            "merge_versions": {"history": {"new": [{"paper_key": "doi:a", "title": "A"}], "changed": [], "repeated": [{"paper_key": "doi:b", "title": "B"}]}},
            "expand_search": {"history": {"new": [{"paper_key": "doi:a", "title": "A"}], "changed": [{"paper_key": "doi:c", "title": "C"}], "repeated": []}},
            "expand_citations": {"history": {"new": [], "changed": [{"paper_key": "doi:c", "title": "C"}], "repeated": [{"paper_key": "doi:d", "title": "D"}]}},
        }})
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertEqual(result["repeated_count"], 2)
        self.assertEqual(result["new_papers"][0]["paper_key"], "doi:a")

    def test_evidence_review_has_strict_fulltext_and_final_comparison_order(self):
        goal = app.create_research_goal({"objective": "Check full text evidence order", "core_question": "page evidence", "workflow": "evidence_review", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        by_key = {item["step_key"]: item for item in run["steps"]}
        self.assertEqual(by_key["extract_evidence"]["dependencies"], ["prepare_fulltext"])
        self.assertEqual(by_key["extract_claims"]["dependencies"], ["ocr_pages"])
        self.assertEqual(by_key["resolve_evidence_gaps"]["dependencies"], ["critique_claims"])
        self.assertEqual(by_key["refresh_comparison"]["dependencies"], ["resolve_comparison_gaps"])
        self.assertEqual(by_key["verify_comparison_evidence"]["dependencies"], ["refresh_comparison"])
        self.assertEqual(by_key["critique_claims_final"]["dependencies"], ["verify_comparison_evidence"])
        self.assertIn("critique_claims_final", by_key["draft_review"]["dependencies"])

    def test_research_brief_prefers_rebuilt_comparison_and_final_review(self):
        goal = app.create_research_goal({"objective": "Compare final evidence", "core_question": "comparison", "sources": ["arxiv"]})
        result = app.call_agent_tool("generate_research_brief", {
            "goal": goal,
            "triage": {"candidates": [{"id": "candidate:p1", "title": "Paper", "year": 2026}]},
            "previous": {
                "compare_candidates": {"rows": [{"paper_id": "candidate:p1", "method": []}], "row_count": 1},
                "refresh_comparison": {"rows": [{"paper_id": "candidate:p1", "method": ["page evidence"]}], "row_count": 1},
                "extract_claims": {"claims": []},
                "resolve_comparison_gaps": {"claims": []},
                "reading_route": {"route": []},
            },
            "verification": {"claims_checked": 1},
            "critique": {"status": "no_obvious_issue"},
        })
        self.assertEqual(result["comparison"]["rows"][0]["method"], ["page evidence"])
        self.assertEqual(result["critique"]["status"], "no_obvious_issue")

    def test_research_brief_filters_unsupported_model_themes_and_attaches_local_evidence(self):
        paper_id, _ = app.upsert_paper({"title": "Evidence-scoped synthesis fixture", "year": 2026, "abstract": "We propose a local evidence method."}, source="manual")
        now = app.now_iso()
        page = "Methods: We use a two-stage feature extraction and classifier pipeline for the benchmark."
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("synthesis-attachment", paper_id, "fixture.pdf", "fixture.pdf", "synthesis-hash", 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("synthesis-attachment", paper_id, 4, page, len(page), "good", "fixture", "synthesis-page-hash", now))
        candidate = {"id": "candidate:synthesis", "local_paper_id": paper_id, "title": "Evidence-scoped synthesis fixture", "abstract": "We propose a local evidence method."}
        overview = app.call_agent_tool("build_paper_analysis_overview", {"papers": [candidate], "claims": [], "verification": {}, "critique": {}})
        goal = app.create_research_goal({"objective": "Synthesize evidence for this method", "core_question": "method evidence", "sources": ["arxiv"]})
        fake_draft = {
            "synthesis": "The candidate proposes a two-stage pipeline.",
            "themes": [
                {"text": "Two-stage methods", "candidate_ids": ["candidate:synthesis", "invented-paper"]},
                {"text": "Unsupported trend", "candidate_ids": ["invented-paper"]},
            ],
            "gaps": [{"text": "Need assess generalization", "candidate_ids": ["candidate:synthesis"]}],
            "cited_candidate_ids": ["candidate:synthesis", "invented-paper"],
        }
        run = {"budget": {"consent_external": True}}
        with patch.object(app, "model_settings", return_value={"enabled": True, "model": "fixture-model", "base_url": "https://example.test/v1"}), patch.object(app, "model_api_key", return_value="fixture-key"), patch.object(app, "model_json_request", return_value=fake_draft):
            result = app.call_agent_tool("generate_research_brief", {
                "goal": goal,
                "confirm": True,
                "triage": {"candidates": [candidate]},
                "previous": {"paper_analysis_overview": overview, "reading_route": {"route": []}, "refresh_comparison": {"rows": [], "gaps": []}},
                "run": run,
            })
        draft = result["model_draft"]
        self.assertEqual(draft["themes"], ["Two-stage methods"])
        self.assertEqual(draft["unsupported_theme_count"], 1)
        self.assertEqual(draft["cited_candidate_ids"], ["candidate:synthesis"])
        self.assertTrue(draft["synthesis_evidence"])
        self.assertTrue(all(item["scope"] == "fulltext_page" and item["page"] == 4 for item in draft["synthesis_evidence"]))

    def test_comparison_route_and_empty_reading_card_are_structured(self):
        comparison = app.call_agent_tool("compare_papers", {
            "papers": [{"id": "p1", "title": "Method A", "year": 2026, "relevance_label": "高相关"}],
            "claims": [{"id": "c1", "paper_id": "p1", "claim_type": "method", "claim_text": "原文摘录：A method"}],
        })
        self.assertEqual(comparison["row_count"], 1)
        self.assertIn("datasets", comparison["rows"][0]["missing_fields"])
        route = app.call_agent_tool("generate_reading_route", {"papers": [{"id": "p1", "title": "Method A", "year": 2026}]})
        self.assertEqual(route["route_count"], 1)
        self.assertEqual(route["route"][0]["stage"], "核心方法")
        empty = app.call_agent_tool("draft_reading_card", {"paper": {}})
        self.assertEqual(empty["status"], "empty")
        fulltext = app.call_agent_tool("prepare_fulltext", {"papers": [{"id": "candidate:p1", "title": "Method A", "pdf_url": "https://example.org/paper.pdf"}]})
        self.assertEqual(fulltext["downloadable_count"], 1)
        self.assertEqual(fulltext["papers"][0]["state"], "downloadable")
        duplicate_fulltext = app.call_agent_tool("prepare_fulltext", {"papers": [
            {"id": "candidate:p1", "title": "Method A", "arxiv_id": "2601.00001", "pdf_url": ""},
            {"id": "candidate:p2", "title": "Method A", "arxiv_id": "2601.00001v1", "pdf_url": ""},
        ]})
        self.assertEqual(duplicate_fulltext["missing_count"], 1)

    def test_paper_analysis_overview_preserves_page_evidence_and_reproducibility_gaps(self):
        paper_id, _ = app.upsert_paper({"title": "Professional analysis fixture", "year": 2026, "abstract": "We study a benchmark and report results."}, source="manual")
        now = app.now_iso()
        page = "Methods and Dataset. We use the FoodBench dataset and compare against a baseline. Results report accuracy improvement. Limitations include a small sample. Figure 2 shows an ablation."
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("overview-attachment", paper_id, "fixture.pdf", "fixture.pdf", "overview-hash", 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("overview-attachment", paper_id, 4, page, len(page), "good", "fixture", "overview-page-hash", now))
        result = app.call_agent_tool("build_paper_analysis_overview", {"papers": [{"id": "candidate:overview", "local_paper_id": paper_id, "title": "Professional analysis fixture", "abstract": "We study a benchmark and report results."}], "claims": [], "verification": {}, "critique": {}})
        self.assertEqual(result["status"], "completed")
        overview = result["paper_overviews"][0]
        self.assertEqual(overview["source_scope"], "fulltext_page")
        self.assertTrue(any(ref["page"] == 4 for ref in overview["analysis"]["method"]["evidence_refs"]))
        self.assertTrue(any(item["status"] == "not_located" for item in overview["reproducibility"]))
        self.assertGreater(overview["evidence_coverage"]["evidence_count"], 0)

    def test_paper_analysis_overview_marks_abstract_only_without_fulltext(self):
        result = app.call_agent_tool("build_paper_analysis_overview", {"papers": [{"id": "candidate:abstract", "title": "Abstract only", "abstract": "We propose a method and evaluate it on a benchmark."}], "claims": [], "verification": {}, "critique": {}})
        overview = result["paper_overviews"][0]
        self.assertEqual(overview["source_scope"], "abstract_only")
        self.assertEqual(overview["analysis"]["method"]["status"], "abstract_only")
        self.assertIsNone(overview["analysis"]["method"]["evidence_refs"][0]["page"])
        self.assertTrue(any("当前只有摘要" in item for item in overview["open_questions"]))

    def test_agent_reading_card_uses_persistent_multimodal_job(self):
        paper_id, _ = app.upsert_paper({"title": "Multimodal fixture", "year": 2026}, source="manual")
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-agent-card", paper_id, "fixture.pdf", "fixture.pdf", "fixture-hash", 10, app.now_iso()))
        submitted = {}
        original_submit, original_get = app.submit_ai_job, app.get_ai_job
        def fake_submit(pid, job_type, data):
            submitted.update({"paper_id": pid, "job_type": job_type, "data": data})
            return {"deduplicated": False, "job": {"id": "job-agent-card", "status": "queued"}}
        def fake_get(job_id, include_result=False):
            return {"id": job_id, "status": "success", "artifact_id": "artifact-agent-card", "result": {"reading_card": {"method": "Page 2 method", "results": "Page 4 result"}}, "meta": {"visual_pages": 2}}
        try:
            app.submit_ai_job, app.get_ai_job = fake_submit, fake_get
            result = app.call_agent_tool("draft_reading_card", {"paper": {"id": paper_id, "local_paper_id": paper_id, "title": "Multimodal fixture"}, "confirm": True})
        finally:
            app.submit_ai_job, app.get_ai_job = original_submit, original_get
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_scope"], "fulltext_multimodal")
        self.assertEqual(result["reading_card"]["method"], "Page 2 method")
        self.assertFalse(submitted["data"]["apply"])

    def test_agent_completion_does_not_accept_queued_async_job(self):
        step = {"completion": {"required": ["status"], "allowed_values": {"status": ["completed", "unavailable"]}}}
        self.assertEqual(app._agent_completion_ok(step, {"status": "queued"})[0], False)
        self.assertEqual(app._agent_completion_ok(step, {"status": "unavailable", "paper_id": "candidate:x"})[0], True)

    def test_agent_completion_validates_nested_output_types_and_array_bounds(self):
        step = {
            "output_schema": {
                "status": "string",
                "summary": {"type": "object", "properties": {"count": "integer", "ready": "boolean"}, "required": ["count"]},
                "papers": {"type": "array", "min_items": 1, "items": {"type": "object", "properties": {"title": "string"}, "required": ["title"]}},
            },
            "completion": {"required": ["status", "summary.count"], "min_items_by_field": {"papers": 1}},
        }
        valid, error = app._agent_completion_ok(step, {"status": "completed", "summary": {"count": 1, "ready": True}, "papers": [{"title": "A"}]})
        self.assertTrue(valid, error)
        invalid, error = app._agent_completion_ok(step, {"status": "completed", "summary": {"count": "1"}, "papers": []})
        self.assertFalse(invalid)
        self.assertIn("summary.count", error)

    def test_agent_completion_reports_missing_nested_required_field(self):
        step = {
            "output_schema": {"result": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}},
            "completion": {"required": ["result"]},
        }
        valid, error = app._agent_completion_ok(step, {"result": {}})
        self.assertFalse(valid)
        self.assertIn("result.items", error)

    def test_direct_agent_tool_entrypoint_validates_input_contract(self):
        with self.assertRaisesRegex(ValueError, "输入结构校验失败.*paper_id"):
            app.call_agent_tool("search_document", {"paper_id": 42, "query": "method"})

    def test_fulltext_agent_step_requires_consent_and_completed_artifact_is_reused(self):
        goal = app.create_research_goal({"objective": "Check consent and reuse", "core_question": "full text", "workflow": "evidence_review", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        draft_step = next(item for item in run["steps"] if item["step_key"] == "draft_reading_card")
        self.assertTrue(draft_step["requires_confirmation"])
        self.assertIn("prereading_guide", draft_step["output_schema"])
        paper_id, _ = app.upsert_paper({"title": "Reuse fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-reuse", paper_id, "fixture.pdf", "fixture.pdf", "reuse-file-hash", 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment-reuse", paper_id, 1, "A stable page for reuse.", 23, "good", "fixture", "reuse-page-hash", now))
        source_fingerprint = app.fulltext_source_fingerprint(paper_id)
        original_settings = app.model_settings
        try:
            app.model_settings = lambda: {"enabled": True, "api_key_set": True, "model": "reuse-model", "protocol": "chat_completions", "api_url": "https://model.example/v1/chat/completions", "base_url": "https://model.example/v1"}
            with app.db_session() as db:
                answer = {"analysis": {"reading_card": {"method": "cached method", "results": "cached result"}}, "meta": {"source_fingerprint": source_fingerprint, "template": "fulltext_reading_card", "language": "zh-CN", "model_identity": {"model": "reuse-model", "protocol": "chat_completions", "endpoint": "https://model.example/v1/chat/completions"}, "pipeline_version": app.READING_CARD_PIPELINE_VERSION, "chunk_count": 1}}
                db.execute("INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("reuse-artifact", paper_id, "reading_card_draft", "fixture", "reuse-model", "fixture", "fixture", json.dumps(answer, ensure_ascii=False), now))
            result = app.submit_ai_job(paper_id, "reading_card", {"template": "fulltext_reading_card", "language": "zh-CN", "apply": False})
        finally:
            app.model_settings = original_settings
        self.assertTrue(result["reused"])
        self.assertEqual(result["job"]["status"], "success")
        self.assertEqual(result["artifact_id"], "reuse-artifact")

    def test_interrupted_ai_job_can_be_retried_without_duplicate_active_work(self):
        paper_id, _ = app.upsert_paper({"title": "Retry fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("old-ai-job", paper_id, "reading_card", "interrupted", "interrupted", "service restart", '{"template":"fulltext_reading_card"}', now, now, now))
        original = app.submit_ai_job
        try:
            app.submit_ai_job = lambda pid, job_type, options: {"job": {"id": "new-ai-job", "paper_id": pid, "status": "queued"}, "deduplicated": False}
            retried = app.retry_ai_job("old-ai-job")
        finally:
            app.submit_ai_job = original
        self.assertEqual(retried["job"]["id"], "new-ai-job")
        self.assertEqual(retried["retried_from"], "old-ai-job")

    def test_startup_resumes_interrupted_ai_jobs_and_is_idempotent(self):
        paper_id, _ = app.upsert_paper({"title": "Automatic resume fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,error,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("restart-reading-card", paper_id, "reading_card", "interrupted", "interrupted", "服务重启时任务未完成", "服务重启时任务未完成", '{"template":"fulltext_reading_card","apply":false}', now, now, now),
            )
        submitted = []
        original_submit = app.submit_ai_job
        try:
            app.submit_ai_job = lambda target, kind, options: submitted.append((target, kind, options)) or {"job": {"id": "resumed-reading-card", "status": "queued"}, "deduplicated": False}
            first = app.resume_interrupted_ai_jobs()
            second = app.resume_interrupted_ai_jobs()
        finally:
            app.submit_ai_job = original_submit
        self.assertEqual(first[0]["interrupted_job_id"], "restart-reading-card")
        self.assertEqual(first[0]["resumed_job_id"], "resumed-reading-card")
        self.assertEqual(second, [])
        self.assertEqual(submitted[0][0:2], (paper_id, "reading_card"))
        self.assertFalse(submitted[0][2]["apply"])
        with app.db_session() as db:
            old = db.execute("SELECT error,message FROM ai_jobs WHERE id=?", ("restart-reading-card",)).fetchone()
            audit = db.execute("SELECT 1 FROM audit_events WHERE event_type='ai.job.resumed' AND entity_id=?", (paper_id,)).fetchone()
        self.assertEqual(old["error"], "")
        self.assertIn("resumed-reading-card", old["message"])
        self.assertIsNotNone(audit)

    def test_startup_resumes_partial_deep_read_with_synthesis_only_and_skips_cancelled(self):
        paper_id, _ = app.upsert_paper({"title": "Partial resume fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        partial_artifact = "partial-resume-artifact"
        answer = json.dumps({
            "analysis": {"analysis_status": "partial", "partial_findings": [{"category": "method", "finding": "saved evidence", "evidence_spans": []}]},
            "meta": {"analysis_status": "partial"},
        }, ensure_ascii=False)
        with app.db_session() as db:
            db.execute(
                "INSERT INTO ai_artifacts(id,paper_id,artifact_type,provider,model,prompt_version,input_sha256,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (partial_artifact, paper_id, "deep_read", "fixture", "fixture", "v1", "r" * 64, answer, now),
            )
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,error,artifact_id,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("restart-deep-read", paper_id, "deep_read", "interrupted", "interrupted", "服务重启时任务未完成", "服务重启时任务未完成", partial_artifact, '{"template":"graduate_deep_read"}', now, now, now),
            )
            db.execute(
                "INSERT INTO ai_jobs(id,paper_id,job_type,status,stage,message,error,cancel_requested,options_json,created_at,updated_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("cancelled-deep-read", paper_id, "deep_read", "interrupted", "interrupted", "服务重启时任务未完成", "服务重启时任务未完成", 1, '{"template":"graduate_deep_read"}', now, now, now),
            )
        submitted = []
        original_submit = app.submit_ai_job
        try:
            app.submit_ai_job = lambda target, kind, options: submitted.append((target, kind, options)) or {"job": {"id": "resumed-deep-read", "status": "queued"}, "deduplicated": False}
            resumed = app.resume_interrupted_ai_jobs()
        finally:
            app.submit_ai_job = original_submit
        self.assertEqual(len(resumed), 1)
        self.assertEqual(submitted[0][1], "deep_read")
        self.assertEqual(submitted[0][2]["synthesis_only_artifact_id"], partial_artifact)
        self.assertFalse(submitted[0][2]["reuse"])
        with app.db_session() as db:
            cancelled = db.execute("SELECT error FROM ai_jobs WHERE id=?", ("cancelled-deep-read",)).fetchone()
        self.assertEqual(cancelled["error"], "服务重启时任务未完成")

    def test_citation_completeness_compares_claim_ids_and_stop_is_blocking_aware(self):
        goal = app.create_research_goal({"objective": "Study evidence traceability", "core_question": "evidence", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        claims = [{"id": "claim-1", "paper_id": "paper-1", "claim_text": "A supported claim"}]
        complete = app.call_agent_tool("check_citation_completeness", {
            "previous": {"extract_claims": {"claims": claims}, "synthesize_map": {"claims": claims}},
        })
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["claims_cited"], 1)
        blocked = app.call_agent_tool("evaluate_stop_conditions", {
            "previous": {"evaluate_coverage": {"coverage": {"candidate_count": 3}}, "verify_evidence": {"needs_fulltext": 1}, "critique_claims": {"open_issues": []}},
            "run": run,
        })
        self.assertFalse(blocked["stop"])
        self.assertGreaterEqual(len(blocked["blocking_reasons"]), 2)
        duplicate = app.call_agent_tool("check_citation_completeness", {
            "previous": {
                "extract_claims": {"claims": claims},
                "synthesize_map": {"claims": [
                    {"id": "claim-1", "claim_text": "A supported claim with enough detail"},
                    {"id": "claim-2", "claim_text": "A supported claim with enough detail"},
                ]},
            },
        })
        self.assertEqual(duplicate["duplicate_claim_count"], 2)
        self.assertFalse(duplicate["complete"])

    def test_tool_contracts_mark_external_data_and_enforce_deadline(self):
        self.assertTrue(app.AGENT_TOOL_SPECS["search_sources"]["external_data"])
        self.assertTrue(app.AGENT_TOOL_SPECS["generate_research_brief"]["requires_confirmation"])
        self.assertEqual(app.AGENT_TOOL_SPECS["expand_citations"]["input_schema"]["papers"], "array")
        self.assertEqual(app.AGENT_TOOL_SPECS["expand_citations"]["output_schema"]["candidate_ids"], "array")
        goal = app.create_research_goal({"objective": "Test bounded agent tools", "core_question": "bounded tools", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        step = next(item for item in run["steps"] if item["step_key"] == "interpret_goal")
        original_call = app.call_agent_tool
        original_timeout = app.AGENT_TOOL_SPECS["build_search_plan"]["timeout_seconds"]
        def slow_call(_tool_name, _payload):
            time.sleep(1.1)
            return {"query": "late", "query_variants": []}
        try:
            app.call_agent_tool = slow_call
            app.AGENT_TOOL_SPECS["build_search_plan"]["timeout_seconds"] = 1
            with self.assertRaises(TimeoutError):
                app._agent_execute_tool(run["id"], step["id"], "build_search_plan", "Draft", {"goal": goal})
        finally:
            app.call_agent_tool = original_call
            app.AGENT_TOOL_SPECS["build_search_plan"]["timeout_seconds"] = original_timeout

    def test_apply_and_external_tools_require_confirmation_at_tool_boundary(self):
        with self.assertRaises(PermissionError):
            app.call_agent_tool("export_report", {"content": "draft"})
        with self.assertRaises(PermissionError):
            app.call_agent_tool("save_reading_card", {"paper_id": "missing", "reading_card": {}})

    def test_focus_selection_prefers_local_fulltext_and_explains_choice(self):
        selected = app._agent_focus_papers([
            {"id": "remote", "title": "Remote", "relevance_score": 0.99, "year": 2026},
            {"id": "local", "title": "Local", "relevance_score": 0.70, "year": 2025, "has_local_pdf": True},
        ], limit=1)
        self.assertEqual(selected[0]["id"], "local")
        self.assertTrue(selected[0]["focus_reason"])

    def test_public_pdf_downloader_validates_and_deduplicates(self):
        paper_id, _ = app.upsert_paper({"title": "Download fixture", "year": 2026, "pdf_url": "https://example.org/paper.pdf"}, source="manual")

        class Response:
            def __init__(self):
                self.parts = [b"%PDF-1.7\nfixture", b"\n%%EOF"]
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _size):
                return self.parts.pop(0) if self.parts else b""
            def geturl(self):
                return "https://example.org/paper.pdf"

        original = app.urlopen
        try:
            app.urlopen = lambda _request, timeout=60: Response()
            first = app.download_public_pdf(paper_id)
            second = app.download_public_pdf(paper_id)
        finally:
            app.urlopen = original
        self.assertTrue(first["downloaded"])
        self.assertTrue(second["deduplicated"])
        with app.db_session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments WHERE paper_id=?", (paper_id,)).fetchone()[0], 1)

    def test_search_agent_executes_reviewed_planner_query_plan(self):
        captured = {}
        original = app.run_query
        def fixture_run_query(query, sources, limit=20, query_id="", scheduled=False, name="", options=None, request_id="", plan_override=None):
            captured.update({"query": query, "sources": sources, "limit": limit, "options": options, "plan_override": plan_override})
            return {"id": "planned-search", "status": "success", "summary": {"variant": "reviewed"}, "papers": [{"id": "paper-1", "title": "Planned result"}]}
        try:
            app.run_query = fixture_run_query
            plan = {"original_query": "reviewed research question", "query_variants": [{"id": "primary", "query": "reviewed research question"}, {"id": "frontier", "query": "reviewed research question recent advances"}], "sources": [{"id": "arxiv", "role": "latest"}], "filters": {"search_depth": "deep", "expand_concepts": False}}
            result = app.call_agent_tool("search_sources", {"goal": {"objective": "ignored fallback", "sources": ["crossref"]}, "query_plan": plan, "limit": 10})
            self.assertEqual(captured["query"], "reviewed research question")
            self.assertEqual(captured["sources"], ["arxiv"])
            self.assertEqual(captured["plan_override"], plan)
            self.assertEqual(captured["options"]["search_depth"], "deep")
            self.assertEqual(result["run_id"], "planned-search")
        finally:
            app.run_query = original

    def test_ocr_tool_skips_good_pages_without_extra_work(self):
        paper_id, _ = app.upsert_paper({"title": "OCR bounded fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-ocr-good", paper_id, "fixture.pdf", "fixture.pdf", "ocr-hash", 10, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment-ocr-good", paper_id, 1, "A sufficiently long extracted page with normal text." * 5, 240, "good", "fixture", "ocr-page-hash", now))
        result = app.call_agent_tool("ocr_pdf_pages", {"paper_ids": [paper_id], "max_pages": 24})
        self.assertIn(result["status"], {"not_needed", "unavailable"})
        if result["status"] == "not_needed":
            self.assertEqual(result["attempted"], 0)
            self.assertEqual(result["updated"], 0)

    def test_agent_pdf_downloads_are_bounded_concurrent_and_ordered(self):
        paper_ids = []
        for index in range(3):
            paper_id, _ = app.upsert_paper({"title": f"Download fixture {index}", "year": 2026}, source="manual")
            paper_ids.append(paper_id)
        active = 0
        peak = 0
        lock = __import__("threading").Lock()
        original = app.download_public_pdf

        def fake_download(paper_id, url=""):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return {"id": f"attachment-{paper_id}", "filename": "fixture.pdf", "deduplicated": False}

        try:
            app.download_public_pdf = fake_download
            result = app.call_agent_tool("download_fulltext", {"paper_ids": paper_ids, "papers": [], "confirm": True})
        finally:
            app.download_public_pdf = original
        self.assertGreaterEqual(peak, 2)
        self.assertEqual([item["paper_id"] for item in result["downloaded"]], paper_ids)
        self.assertEqual(result["failed_count"], 0)

    def test_agent_ocr_pages_run_concurrently_and_persist_in_page_order(self):
        paper_id, _ = app.upsert_paper({"title": "OCR concurrent fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        attachment_id = "attachment-ocr-concurrent"
        stored_name = "ocr-concurrent.pdf"
        app.FILES_DIR.mkdir(parents=True, exist_ok=True)
        (app.FILES_DIR / stored_name).write_bytes(b"%PDF-1.4 fixture")
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", (attachment_id, paper_id, stored_name, stored_name, "ocr-concurrent-hash", 16, now))
            for page in (1, 2, 3):
                db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id, paper_id, page, "", 0, "low", "fixture", "", now))

        class FakePixmap:
            width = 1
            height = 1
            samples = b"\x00\x00\x00"

        class FakePage:
            def get_pixmap(self, **_kwargs):
                return FakePixmap()

        class FakeDocument:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __len__(self):
                return 3

            def __getitem__(self, _index):
                return FakePage()

        fitz_module = types.ModuleType("fitz")
        fitz_module.open = lambda _path: FakeDocument()
        fitz_module.Matrix = lambda *_args: object()
        pytesseract_module = types.ModuleType("pytesseract")
        pytesseract_module.image_to_string = lambda _image, lang="eng": (time.sleep(0.02), "OCR page text " + lang)[1]
        pil_module = types.ModuleType("PIL")
        image_module = types.ModuleType("PIL.Image")
        image_module.frombytes = lambda *_args: object()
        pil_module.Image = image_module
        with patch.dict(sys.modules, {"fitz": fitz_module, "pytesseract": pytesseract_module, "PIL": pil_module, "PIL.Image": image_module}):
            result = app.call_agent_tool("ocr_pdf_pages", {"paper_ids": [paper_id], "max_pages": 24})
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["updated"], 3)
        self.assertEqual([item["page"] for item in result["pages"]], [1, 2, 3])
        with app.db_session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_pages WHERE paper_id=? AND extractor='pytesseract'", (paper_id,)).fetchone()[0], 3)

    def test_version_history_and_critique_are_persistent(self):
        goal = app.create_research_goal({"objective": "Study multimodal reasoning evidence", "core_question": "multimodal reasoning", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        papers = [{"id": "candidate:one", "title": "A multimodal method", "year": 2026, "arxiv_id": "2601.00001v1", "abstract": "Our method improves accuracy on a dataset."}]
        merged = app.call_agent_tool("merge_paper_versions", {"goal": goal, "run_id": run["id"], "papers": papers})
        self.assertEqual(merged["group_count"], 1)
        self.assertEqual(merged["history"]["new_count"], 1)
        with app.db_session() as db:
            row = db.execute("SELECT seen_count FROM research_goal_papers WHERE goal_id=?", (goal["id"],)).fetchone()
            self.assertEqual(row["seen_count"], 1)

        editions = app.call_agent_tool("merge_paper_versions", {"goal": goal, "run_id": run["id"], "papers": [
            {"id": "candidate:arxiv", "title": "A multimodal method", "year": 2025, "arxiv_id": "2501.00001", "authors": "Alice"},
            {"id": "candidate:doi", "title": "A multimodal method", "year": 2026, "doi": "10.1000/example", "authors": "Alice"},
        ]})
        self.assertEqual(editions["group_count"], 1)

    def test_local_agent_run_reaches_artifact_with_critique(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "Multimodal reasoning fixture",
            "abstract": "We propose a method and report accuracy on a benchmark dataset. Limitations include failure cases.",
            "year": 2026,
            "arxiv_id": "2601.00002v1",
            "source": "arxiv",
        }]
        try:
            goal = app.create_research_goal({"objective": "Study multimodal reasoning evidence", "core_question": "multimodal reasoning", "sources": ["arxiv"]})
            run = app.create_agent_run(goal["id"])
            app.approve_agent_run(run["id"])
            for _ in range(300):
                snapshot = app.agent_run_snapshot(run["id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(snapshot["status"], "success")
            self.assertEqual(next(item for item in snapshot["steps"] if item["step_key"] == "critique_claims")["status"], "success")
            self.assertTrue(any(item["artifact_type"] == "critique" for item in snapshot["artifacts"]))
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

    def test_evidence_review_without_local_pdf_finishes_with_explicit_gap(self):
        original = app.SOURCE_SEARCH["arxiv"]
        app.SOURCE_SEARCH["arxiv"] = lambda _query, _limit, offset=0: [] if offset else [{
            "title": "Evidence review fixture",
            "abstract": "We report a method and benchmark result with limitations.",
            "year": 2026,
            "arxiv_id": "2601.00003v1",
            "source": "arxiv",
        }]
        try:
            goal = app.create_research_goal({"objective": "Review evidence without a downloaded PDF", "core_question": "evidence review", "workflow": "evidence_review", "sources": ["arxiv"]})
            run = app.create_agent_run(goal["id"])
            app.approve_agent_run(run["id"])
            for _ in range(160):
                snapshot = app.agent_run_snapshot(run["id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
            self.assertEqual(snapshot["status"], "success")
            prepare = next(item for item in snapshot["steps"] if item["step_key"] == "prepare_fulltext")
            self.assertEqual(prepare["output"]["missing_count"], 1)
            self.assertEqual(next(item for item in snapshot["steps"] if item["step_key"] == "resolve_evidence_gaps")["output"]["status"], "no_local_fulltext")
        finally:
            app.SOURCE_SEARCH["arxiv"] = original

    def test_evidence_review_reaches_final_artifact_with_mocked_fulltext_ai(self):
        paper_id, _ = app.upsert_paper({"title": "Agent fulltext fixture", "year": 2026, "doi": "10.1000/agent-fixture"}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-agent-e2e", paper_id, "fixture.pdf", "fixture.pdf", "fixture-e2e-hash", 10, now))
            page = "Methods: we use the AgentBench dataset and compare against two baselines. Results: accuracy improves to 91 percent. Limitations: small sample."
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment-agent-e2e", paper_id, 2, page, len(page), "good", "fixture", "fixture-page-hash", now))
        original_query, original_submit, original_get = app.run_query, app.submit_ai_job, app.get_ai_job
        original_model_settings, original_model_api_key = app.model_settings, app.model_api_key
        def fake_query(*_args, **_kwargs):
            return {"id": "agent-search-fixture", "status": "success", "summary": {}, "papers": [{"id": "candidate:agent-fixture", "title": "Agent fulltext fixture", "year": 2026, "doi": "10.1000/agent-fixture", "abstract": "We report a method and dataset result."}]}
        def fake_submit(pid, job_type, data):
            return {"deduplicated": False, "job": {"id": "job-agent-e2e", "status": "queued"}}
        def fake_get(job_id, include_result=False):
            return {"id": job_id, "status": "success", "artifact_id": "artifact-agent-e2e", "result": {"reading_card": {"method": "Fulltext method", "results": "Fulltext result"}}, "meta": {"visual_pages": 1}}
        try:
            app.run_query, app.submit_ai_job, app.get_ai_job = fake_query, fake_submit, fake_get
            app.model_settings = lambda: {"enabled": True, "api_key_set": True, "model": "fixture-model", "api_url": "https://model.example/v1", "base_url": "https://model.example/v1"}
            app.model_api_key = lambda: "fixture-key"
            goal = app.create_research_goal({"objective": "Review fixture evidence", "core_question": "Agent fulltext", "workflow": "evidence_review", "sources": ["arxiv"]})
            run = app.create_agent_run(goal["id"])
            app.approve_agent_run(run["id"], consent_external=True)
            for _ in range(300):
                snapshot = app.agent_run_snapshot(run["id"])
                if snapshot["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.02)
        finally:
            app.run_query, app.submit_ai_job, app.get_ai_job = original_query, original_submit, original_get
            app.model_settings, app.model_api_key = original_model_settings, original_model_api_key
        self.assertEqual(snapshot["status"], "success")
        draft = next(item for item in snapshot["steps"] if item["step_key"] == "draft_reading_card")
        self.assertEqual(draft["output"]["status"], "completed")
        self.assertEqual(draft["output"]["reading_card"]["method"], "Fulltext method")
        self.assertTrue(any(item["artifact_type"] == "evidence_brief" for item in snapshot["artifacts"]))

    def test_evidence_verification_reads_page_number(self):
        goal = app.create_research_goal({"objective": "Verify page evidence in a paper", "core_question": "evidence", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        paper_id, _ = app.upsert_paper({"title": "Evidence fixture", "year": 2026}, source="manual")
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment", paper_id, "fixture.pdf", "fixture.pdf", "hash", 10, app.now_iso()))
            db.execute("INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("claim", goal["id"], run["id"], paper_id, "原文摘录：method result", "method", "needs_verification", "medium", "fulltext_page", app.now_iso(), app.now_iso()))
            db.execute("INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("evidence", "claim", paper_id, 3, "PDF 页面文本", "method result", "page_text", "unverified", "hash", app.now_iso()))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment", paper_id, 3, "The method result is reproducible.", 34, "good", "fixture", "hash", app.now_iso()))
        result = app.call_agent_tool("verify_evidence", {"goal": goal, "run_id": run["id"]})
        self.assertEqual(result["claims_checked"], 1)
        self.assertEqual(result["evidence_not_found"], 0)

    def test_evidence_gap_loop_adds_page_evidence_and_rechecks_claim(self):
        goal = app.create_research_goal({"objective": "Close evidence gaps", "core_question": "method evidence", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        paper_id, _ = app.upsert_paper({"title": "Gap fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-gap", paper_id, "fixture.pdf", "fixture.pdf", "hash-gap", 10, now))
            db.execute("INSERT INTO research_claims(id,goal_id,run_id,paper_id,claim_text,claim_type,status,confidence,evidence_scope,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("claim-gap", goal["id"], run["id"], paper_id, "摘要摘录：method details", "method", "abstract_only", "medium", "abstract", now, now))
            db.execute("INSERT INTO evidence_spans(id,claim_id,paper_id,page_number,section,quote,evidence_type,verification_status,source_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("evidence-gap-abstract", "claim-gap", paper_id, None, "abstract", "method details", "abstract_text", "quote_match_abstract_only", "abstract-hash", now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment-gap", paper_id, 2, "The method details are fully described on this page.", 50, "good", "fixture", "page-hash", now))
        result = app.call_agent_tool("resolve_evidence_gaps", {"run_id": run["id"], "previous": {"extract_claims": {"claims": [{"id": "claim-gap", "paper_id": paper_id, "claim_text": "method details", "evidence_scope": "abstract"}]}, "verify_evidence": {"results": [{"claim_id": "claim-gap", "status": "abstract_only"}]}}, "round": 1})
        self.assertEqual(result["resolved_count"], 1)
        refined = app.call_agent_tool("verify_evidence", {"goal": goal, "run_id": run["id"]})
        self.assertEqual(refined["claims_checked"], 1)
        self.assertEqual(refined["needs_fulltext"], 0)
        self.assertEqual(refined["results"][0]["status"], "page_quote_matched")

    def test_comparison_gap_resolution_creates_page_claim_and_evidence(self):
        goal = app.create_research_goal({"objective": "Compare methods with page evidence", "core_question": "comparison evidence", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        paper_id, _ = app.upsert_paper({"title": "Comparison fixture", "year": 2026}, source="manual")
        now = app.now_iso()
        page_text = "3 Methods and Dataset\nWe use the MultiBench dataset and compare our method with two baselines."
        page_hash = "comparison-page-hash"
        with app.db_session() as db:
            db.execute("INSERT INTO attachments(id,paper_id,original_name,stored_name,sha256,byte_size,added_at) VALUES(?,?,?,?,?,?,?)", ("attachment-comparison", paper_id, "fixture.pdf", "fixture.pdf", page_hash, 100, now))
            db.execute("INSERT INTO document_pages(attachment_id,paper_id,page_number,text,char_count,quality,extractor,text_sha256,extracted_at) VALUES(?,?,?,?,?,?,?,?,?)", ("attachment-comparison", paper_id, 3, page_text, len(page_text), "good", "fixture", page_hash, now))
        result = app.call_agent_tool("resolve_comparison_gaps", {
            "goal": goal,
            "run_id": run["id"],
            "papers": [{"id": "candidate:comparison", "local_paper_id": paper_id, "title": "Comparison fixture"}],
            "claims": [],
            "comparison": {"gaps": [{"paper_id": "candidate:comparison", "fields": ["datasets"]}]},
        })
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["claims"][0]["page"], 3)
        self.assertEqual(result["claims"][0]["source_sha256"], page_hash)
        refined = app.call_agent_tool("verify_evidence", {"goal": goal, "run_id": run["id"]})
        self.assertEqual(refined["claims_checked"], 1)
        self.assertEqual(refined["results"][0]["status"], "page_quote_matched")

    def test_comparison_gap_resolution_reports_missing_local_fulltext(self):
        goal = app.create_research_goal({"objective": "Compare papers without local files", "core_question": "comparison evidence", "sources": ["arxiv"]})
        run = app.create_agent_run(goal["id"])
        result = app.call_agent_tool("resolve_comparison_gaps", {
            "goal": goal,
            "run_id": run["id"],
            "papers": [{"id": "candidate:missing", "title": "Missing fixture"}],
            "claims": [],
            "comparison": {"gaps": [{"paper_id": "candidate:missing", "fields": ["method", "results"]}]},
        })
        self.assertEqual(result["status"], "no_local_fulltext")
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["remaining_count"], 2)

    def test_browse_web_rejects_private_and_local_addresses(self):
        for value in ("http://127.0.0.1:8000/", "http://localhost/", "https://service.local/page", "file:///tmp/a"):
            with self.assertRaises(ValueError):
                app._safe_public_web_url(value)

    def test_browse_web_uses_tavily_extract_and_bounds_evidence(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"results": [{"url": "https://example.org/paper", "title": "A paper", "raw_content": "x" * 2000}], "failed_results": []}).encode()

        with patch.object(app, "_tavily_api_key", return_value="fixture-key"), patch.object(app, "urlopen", return_value=FakeResponse()) as request:
            result = app.call_agent_tool("browse_web", {"urls": ["https://example.org/paper"], "max_chars": 700})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider"], "Tavily Extract")
        self.assertEqual(len(result["pages"]), 1)
        self.assertLessEqual(len(result["pages"][0]["content"]), 700)
        request.assert_called_once()
        self.assertIn("api.tavily.com/extract", request.call_args.args[0].full_url)


if __name__ == "__main__":
    unittest.main()
